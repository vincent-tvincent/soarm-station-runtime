"""Stale collector cleanup never claims another session or ignores real errors."""
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace

import pytest

import soarm_station.rpc_trace_collector as collector


@pytest.fixture
def recorded_owner(tmp_path,monkeypatch):
    # The real allowlist deliberately only accepts its private /tmp prefix.
    with tempfile.TemporaryDirectory(prefix='soarm-rpc-trace-test-',dir='/tmp') as name:
        directory=Path(name);token='d8daa9dfb3a74778b4c7d395785808b9';session='soarm-rpc-'+token
        marker={'session':session,'token':token,'pid':1967}
        (directory/'owner.json').write_text(json.dumps(marker));(directory/'snapshot').write_bytes(b'owned data')
        manifest=tmp_path/'runtime.rpc-trace.sock.owner.json'
        value={**marker,'directory':str(directory),'process_start':'1442'}
        manifest.write_text(json.dumps(value))
        unrelated=tmp_path/'unrelated-session';unrelated.mkdir();(unrelated/'keep').write_text('unrelated')
        monkeypatch.setattr(collector,'process_info',lambda pid:None)
        yield SimpleNamespace(directory=directory,manifest=manifest,value=value,session=session,unrelated=unrelated)


def command(monkeypatch,owner,*,returncode=1,stderr='',stdout=''):
    calls=[]
    def run(args,**options):
        calls.append((args,options))
        assert args==['lttng','destroy',owner.session]
        assert options=={'capture_output':True,'text':True,'timeout':3}
        return SimpleNamespace(returncode=returncode,stderr=stderr,stdout=stdout)
    monkeypatch.setattr(collector.subprocess,'run',run)
    return calls


def test_boot_stale_manifest_with_absent_daemon_cleans_only_owned_files(recorded_owner,monkeypatch):
    o=recorded_owner;calls=command(monkeypatch,o,stderr='Error: No session daemon is available\n')
    collector.cleanup_stale_owner(o.manifest)
    assert len(calls)==1 and not o.manifest.exists() and not o.directory.exists()
    assert (o.unrelated/'keep').read_text()=='unrelated'


@pytest.mark.parametrize('message',[
    'Error: Session name not found\n',
    'Error: Session does not exist\n',
    "Error: Session '{session}' does not exist\n",
    'Error: Session {session} not found\n',
])
def test_already_missing_exact_session_allows_stale_file_cleanup(recorded_owner,monkeypatch,message):
    o=recorded_owner;command(monkeypatch,o,stderr=message.format(session=o.session))
    collector.cleanup_stale_owner(o.manifest)
    assert not o.directory.exists() and not o.manifest.exists() and o.unrelated.exists()


def test_successful_destroy_keeps_other_sessions_and_files(recorded_owner,monkeypatch):
    o=recorded_owner;command(monkeypatch,o,returncode=0,stdout='Session destroyed\n')
    collector.cleanup_stale_owner(o.manifest)
    assert not o.manifest.exists() and not o.directory.exists() and o.unrelated.exists()


@pytest.mark.parametrize('stderr,stdout',[
    ('Error: Permission denied\n',''),
    ('Error: Unable to connect to session daemon\n',''),
    ('Error: No session daemon is available\nError: Permission denied\n',''),
    ('Error: Session name not found: permission denied\n',''),
    ("Error: Session 'another-session' does not exist\n",''),
    ('Error: No session daemon is available\n','Unexpected output'),
    ('',''),
])
def test_unknown_or_permission_errors_preserve_evidence(recorded_owner,monkeypatch,stderr,stdout):
    o=recorded_owner;command(monkeypatch,o,stderr=stderr,stdout=stdout)
    with pytest.raises(RuntimeError,match='Cannot clean recorded RPC trace session:') as error:
        collector.cleanup_stale_owner(o.manifest)
    if stderr.strip():assert stderr.strip()[:240] in str(error.value)
    assert o.manifest.exists() and o.directory.exists() and o.unrelated.exists()


def test_live_collector_is_never_destroyed_even_when_daemon_is_absent(recorded_owner,monkeypatch):
    o=recorded_owner;calls=command(monkeypatch,o,stderr='Error: No session daemon is available\n')
    monkeypatch.setattr(collector,'process_info',lambda pid:(1,'1442'))
    with pytest.raises(RuntimeError,match='still alive'):collector.cleanup_stale_owner(o.manifest)
    assert calls==[] and o.manifest.exists() and o.directory.exists()


def test_reused_pid_does_not_block_cleanup_of_dead_original_owner(recorded_owner,monkeypatch):
    o=recorded_owner;command(monkeypatch,o,stderr='Error: No session daemon is available\n')
    monkeypatch.setattr(collector,'process_info',lambda pid:(1,'9999'))
    collector.cleanup_stale_owner(o.manifest)
    assert not o.manifest.exists()


def test_directory_marker_mismatch_cannot_delete_or_destroy_anything(recorded_owner,monkeypatch):
    o=recorded_owner;calls=command(monkeypatch,o,stderr='Error: No session daemon is available\n')
    (o.directory/'owner.json').write_text(json.dumps({'session':o.session,'token':'other'}))
    with pytest.raises(ValueError,match='ownership mismatch'):collector.cleanup_stale_owner(o.manifest)
    assert calls==[] and o.manifest.exists() and o.directory.exists()


def test_malformed_session_manifest_never_runs_lttng(recorded_owner,monkeypatch):
    o=recorded_owner;calls=command(monkeypatch,o,returncode=0)
    o.manifest.write_text(json.dumps({**o.value,'session':'unrelated'}))
    with pytest.raises(ValueError,match='Unrecognized'):collector.cleanup_stale_owner(o.manifest)
    assert calls==[] and o.directory.exists() and o.unrelated.exists()


def test_missing_temporary_directory_still_allows_dead_manifest_cleanup(recorded_owner,monkeypatch):
    o=recorded_owner;command(monkeypatch,o,stderr='Error: No session daemon is available\n')
    for path in o.directory.iterdir():path.unlink()
    o.directory.rmdir()
    collector.cleanup_stale_owner(o.manifest)
    assert not o.manifest.exists() and o.unrelated.exists()


def test_lttng_timeout_does_not_remove_owned_evidence(recorded_owner,monkeypatch):
    o=recorded_owner
    def fail(*args,**kwargs):raise subprocess.TimeoutExpired('lttng',3)
    monkeypatch.setattr(collector.subprocess,'run',fail)
    with pytest.raises(subprocess.TimeoutExpired):collector.cleanup_stale_owner(o.manifest)
    assert o.directory.exists() and o.manifest.exists()
