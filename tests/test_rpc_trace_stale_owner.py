"""Enumerated stale collector cleanup never claims another tracing session."""
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
from xml.sax.saxutils import escape

import pytest
import soarm_station.rpc_trace_collector as collector

NS='https://lttng.org/xml/ns/lttng-mi'
# Captured from the installed CLI when its daemon is absent (exit 1).
ABSENT_XML='''<?xml version="1.0" encoding="UTF-8"?>
<command xmlns="https://lttng.org/xml/ns/lttng-mi" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="https://lttng.org/xml/ns/lttng-mi https://lttng.org/xml/schemas/lttng-mi/4/lttng-mi-4.1.xsd" schemaVersion="4.1"><name>list</name><output/></command>'''
ABSENT_ERROR='Error: No session daemon is available\n'
DESTROY_WARNING="PWARN - 13:28:20 Failed to open configuration file '/home/pipi/.lttngrc': No such file or directory (in open_config() at conf.cpp:57)\nError: Failed to list sessions (No session daemon is available)\n"


def session_xml(*names):
    rows=''.join('<session><name>'+escape(name)+'</name><enabled>true</enabled><snapshot_mode>1</snapshot_mode></session>' for name in names)
    return f'<command xmlns="{NS}" schemaVersion="4.1"><name>list</name><output><sessions>{rows}</sessions></output></command>'


def result(stdout='',stderr='',code=0):return SimpleNamespace(returncode=code,stderr=stderr,stdout=stdout)
def absent(xml=ABSENT_XML):return result(xml,ABSENT_ERROR,1)


@pytest.fixture
def recorded_owner(tmp_path,monkeypatch):
    with tempfile.TemporaryDirectory(prefix='soarm-rpc-trace-test-',dir='/tmp') as name:
        directory=Path(name);token='d8daa9dfb3a74778b4c7d395785808b9';session='soarm-rpc-'+token
        marker={'session':session,'token':token,'pid':1967}
        (directory/'owner.json').write_text(json.dumps(marker));(directory/'snapshot').write_bytes(b'owned data')
        manifest=tmp_path/'runtime.rpc-trace.sock.owner.json'
        value={**marker,'directory':str(directory),'process_start':'1442'};manifest.write_text(json.dumps(value))
        unrelated=tmp_path/'unrelated-session';unrelated.mkdir();(unrelated/'keep').write_text('unrelated')
        monkeypatch.setattr(collector,'process_info',lambda pid:None)
        yield SimpleNamespace(directory=directory,manifest=manifest,value=value,session=session,unrelated=unrelated)


def commands(monkeypatch,owner,replies):
    calls=[];responses=iter(replies)
    def run(args,**options):
        calls.append(args)
        assert args in (['lttng','--mi','xml','list'],['lttng','destroy',owner.session])
        assert options=={'capture_output':True,'text':True,'timeout':3}
        response=next(responses)
        if isinstance(response,Exception):raise response
        return response
    monkeypatch.setattr(collector.subprocess,'run',run)
    return calls


def cleaned(owner):
    assert not owner.manifest.exists() and not owner.directory.exists()
    assert (owner.unrelated/'keep').read_text()=='unrelated'


@pytest.mark.parametrize('xml',['',ABSENT_XML])
def test_absent_daemon_is_verified_by_list_without_attempting_destroy(recorded_owner,monkeypatch,xml):
    o=recorded_owner;calls=commands(monkeypatch,o,[absent(xml)])
    collector.cleanup_stale_owner(o.manifest);cleaned(o)
    assert calls==[['lttng','--mi','xml','list']]


def test_live_daemon_with_only_unrelated_or_similarly_named_sessions_is_preserved(recorded_owner,monkeypatch):
    o=recorded_owner;calls=commands(monkeypatch,o,[result(session_xml('unrelated',o.session+'-suffix',' '+o.session))])
    collector.cleanup_stale_owner(o.manifest);cleaned(o)
    assert calls==[['lttng','--mi','xml','list']]


def test_owned_and_unrelated_sessions_destroy_only_exact_recorded_name(recorded_owner,monkeypatch):
    o=recorded_owner;calls=commands(monkeypatch,o,[result(session_xml('unrelated',o.session)),result('Session destroyed\n')])
    collector.cleanup_stale_owner(o.manifest);cleaned(o)
    assert calls==[['lttng','--mi','xml','list'],['lttng','destroy',o.session]]


@pytest.mark.parametrize('after',[absent(),result(session_xml('unrelated'))])
def test_destroy_warning_or_disappearing_daemon_requires_independent_absence_proof(recorded_owner,monkeypatch,after):
    o=recorded_owner;calls=commands(monkeypatch,o,[result(session_xml(o.session,'unrelated')),result('',DESTROY_WARNING,1),after])
    collector.cleanup_stale_owner(o.manifest);cleaned(o)
    assert calls==[['lttng','--mi','xml','list'],['lttng','destroy',o.session],['lttng','--mi','xml','list']]


def test_destroy_timeout_is_accepted_only_after_verified_session_absence(recorded_owner,monkeypatch):
    o=recorded_owner;commands(monkeypatch,o,[result(session_xml(o.session)),subprocess.TimeoutExpired('lttng',3),result(session_xml())])
    collector.cleanup_stale_owner(o.manifest);cleaned(o)


@pytest.mark.parametrize('error',[DESTROY_WARNING,'Error: Permission denied\n','Error: Session name not found\n'])
def test_destroy_error_cannot_delete_files_while_owned_session_still_exists(recorded_owner,monkeypatch,error):
    o=recorded_owner;commands(monkeypatch,o,[result(session_xml(o.session)),result('',error,1),result(session_xml(o.session))])
    with pytest.raises(RuntimeError,match='Cannot clean recorded RPC trace session:'):
        collector.cleanup_stale_owner(o.manifest)
    assert o.manifest.exists() and o.directory.exists() and o.unrelated.exists()


def test_failed_requery_preserves_files_after_destroy_failure(recorded_owner,monkeypatch):
    o=recorded_owner;commands(monkeypatch,o,[result(session_xml(o.session)),result('',DESTROY_WARNING,1),result('','Error: Permission denied',1)])
    with pytest.raises(RuntimeError,match='Cannot enumerate'):collector.cleanup_stale_owner(o.manifest)
    assert o.manifest.exists() and o.directory.exists()


@pytest.mark.parametrize('reply',[
    result('','Error: Permission denied\n',1),
    result('',DESTROY_WARNING,1),
    result('',ABSENT_ERROR+'Error: Permission denied\n',1),
    result(session_xml(),'Unexpected warning',0),
    result(''),
    result('not XML'),
    result(session_xml().replace(NS,'https://wrong.example/schema')),
    result(session_xml().replace('schemaVersion="4.1"','schemaVersion="99"')),
    result(session_xml().replace('<name>list</name>','<name>destroy</name>')),
    result(ABSENT_XML), # Success must enumerate sessions, not just empty output.
    result(session_xml().replace('<sessions>','<domains>').replace('</sessions>','</domains>')),
    result(session_xml('one','one')),
    result(session_xml('one').replace('<name>one</name>','')),
    result(session_xml('one').replace('<name>one</name>','<name>one</name><name>two</name>')),
    result(session_xml().replace('</command>','<success>false</success></command>')),
    absent('not XML'),
    absent(session_xml('unexpected-session')),
])
def test_unexpected_list_errors_or_schema_preserve_everything(recorded_owner,monkeypatch,reply):
    o=recorded_owner;calls=commands(monkeypatch,o,[reply])
    with pytest.raises(RuntimeError,match='Cannot enumerate'):collector.cleanup_stale_owner(o.manifest)
    assert calls==[['lttng','--mi','xml','list']]
    assert o.manifest.exists() and o.directory.exists() and o.unrelated.exists()


def test_live_collector_is_checked_before_any_lttng_command(recorded_owner,monkeypatch):
    o=recorded_owner;calls=commands(monkeypatch,o,[])
    monkeypatch.setattr(collector,'process_info',lambda pid:(1,'1442'))
    with pytest.raises(RuntimeError,match='still alive'):collector.cleanup_stale_owner(o.manifest)
    assert calls==[] and o.manifest.exists() and o.directory.exists()


def test_reused_pid_does_not_block_verified_cleanup_of_original_dead_owner(recorded_owner,monkeypatch):
    o=recorded_owner;commands(monkeypatch,o,[absent()])
    monkeypatch.setattr(collector,'process_info',lambda pid:(1,'9999'))
    collector.cleanup_stale_owner(o.manifest);cleaned(o)


@pytest.mark.parametrize('mismatch',['marker','manifest'])
def test_ownership_mismatch_never_enumerates_or_destroys(recorded_owner,monkeypatch,mismatch):
    o=recorded_owner;calls=commands(monkeypatch,o,[])
    if mismatch=='marker':(o.directory/'owner.json').write_text(json.dumps({'session':o.session,'token':'other'}))
    else:o.manifest.write_text(json.dumps({**o.value,'session':'unrelated'}))
    with pytest.raises(ValueError):collector.cleanup_stale_owner(o.manifest)
    assert calls==[] and o.manifest.exists() and o.directory.exists()


def test_missing_temporary_directory_still_requires_verified_session_absence(recorded_owner,monkeypatch):
    o=recorded_owner;commands(monkeypatch,o,[result(session_xml())])
    for path in o.directory.iterdir():path.unlink()
    o.directory.rmdir();collector.cleanup_stale_owner(o.manifest);cleaned(o)


def test_list_timeout_does_not_remove_owned_evidence(recorded_owner,monkeypatch):
    o=recorded_owner;commands(monkeypatch,o,[subprocess.TimeoutExpired('lttng',3)])
    with pytest.raises(subprocess.TimeoutExpired):collector.cleanup_stale_owner(o.manifest)
    assert o.directory.exists() and o.manifest.exists()
