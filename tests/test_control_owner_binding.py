"""Persistent manual ownership follows real Unix EOF, not an idle clock."""
import json
import os
import select
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from soarm_station.control import ArmController
from soarm_station.ipc import Client
from soarm_station.worker import Runtime,RuntimeIPCServer,RuntimeRequestHandler


@pytest.fixture
def owner_server(monkeypatch):
    monkeypatch.setattr('soarm_station.control.now',time.monotonic)
    config={'id':'arm','backend':'mock','joints':[{'name':'joint','min_rad':-2.,'max_rad':2.,'velocity_rad_s':1.}],
            'gripper':{'name':'gripper'}}
    arm=ArmController(config,{'control_hz':100,'profiles':{}},'boot',{})
    arm.q={'joint':0.};arm.gripper=50.;arm.sample_time=time.monotonic()
    monkeypatch.setattr(arm.hold_event,'wait',lambda timeout:True)
    runtime=Runtime.__new__(Runtime);runtime.arms={'arm':arm}
    attempts=[];released=threading.Event();original=arm.hold_owned
    def held(*args,**kwargs):
        attempts.append((args,kwargs))
        result=original(*args,**kwargs);released.set();return result
    monkeypatch.setattr(arm,'hold_owned',held)
    timeouts=[]
    class RecordingHandler(RuntimeRequestHandler):
        def handle(self):
            timeouts.append(self.request.gettimeout())
            super().handle()
    with tempfile.TemporaryDirectory(prefix='control-owner-',dir='/tmp') as directory:
        path=str(Path(directory)/'runtime.sock')
        server=RuntimeIPCServer(path,runtime);server.RequestHandlerClass=RecordingHandler
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
        clients=[]
        def client():
            result=Client(path);clients.append(result);return result
        def begin(persistent=True,principal='operator'):
            arm.sample_time=time.monotonic()
            arm.set_permission(True,principal,arm.session)
            arm.begin({'runtime_boot_id':'boot','controller_session':arm.session,'principal':principal,
                       'operation_type':'DIRECT','mode':'STREAM','persistent_target':persistent})
            return {'arm_id':arm.id,'controller_session':arm.session,'principal':principal,'operation_id':arm.op['operation_id']}
        yield SimpleNamespace(arm=arm,runtime=runtime,client=client,begin=begin,path=path,
                              attempts=attempts,released=released,timeouts=timeouts)
        for entry in clients:entry.close()
        server.shutdown();server.server_close();thread.join(timeout=1)
        arm.pool.shutdown(wait=False,cancel_futures=True)


def test_dedicated_connection_has_no_idle_deadline_and_eof_holds_exact_operation(owner_server):
    value=owner_server;binding=value.begin();client=value.client()
    response=client.call('bind_control_owner',**binding)
    assert response=={'accepted':True,'arm_id':'arm','controller_session':binding['controller_session'],
                     'operation_id':binding['operation_id']}
    assert value.timeouts==[None]
    assert not value.released.wait(.05)
    assert value.arm.permission and value.arm.op['operation_id']==binding['operation_id']
    client.close()
    assert value.released.wait(1)
    assert value.arm.op is None and not value.arm.permission
    assert value.arm.error=='CONTROL_OWNER_DISCONNECTED'
    assert value.attempts==[((binding['controller_session'],binding['principal'],binding['operation_id']),
                            {'reason':'CONTROL_OWNER_DISCONNECTED'})]
    assert value.arm.backend.writes==0  # No device thread or physical backend exists in this test.


def test_actual_client_process_sigkill_closes_owner_connection_without_python_cleanup(owner_server):
    value=owner_server;binding=value.begin()
    script='''
import json,sys
from soarm_station.ipc import Client
client=Client(sys.argv[1])
client.call('bind_control_owner',**json.loads(sys.argv[2]))
print('bound',flush=True)
sys.stdin.buffer.read(1)
'''
    env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]/'python'))
    process=subprocess.Popen([sys.executable,'-c',script,value.path,json.dumps(binding)],
                             stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
    try:
        ready,_,_=select.select([process.stdout],[],[],3)
        assert ready and process.stdout.readline()==b'bound\n'
        assert value.arm.op and value.arm.permission
        process.kill();process.wait(timeout=3)
        assert value.released.wait(1)
        assert value.arm.op is None and not value.arm.permission
        assert value.arm.error=='CONTROL_OWNER_DISCONNECTED'
    finally:
        if process.poll() is None:process.kill();process.wait(timeout=3)
        for stream in (process.stdin,process.stdout,process.stderr):stream.close()


def test_unbound_normal_ipc_client_close_does_not_change_operation_lifetime(owner_server):
    value=owner_server;binding=value.begin();client=value.client()
    client.call('feedback',arm_id='arm')
    client.close()
    assert not value.released.wait(.05)
    assert value.arm.permission and value.arm.op['operation_id']==binding['operation_id']


@pytest.mark.parametrize('field,replacement',[('arm_id','other'),('controller_session','old'),
                                            ('principal','other'),('operation_id','old'),('principal','')])
def test_invalid_binding_never_installs_cleanup_for_another_owner(owner_server,field,replacement):
    value=owner_server;binding=value.begin();client=value.client()
    with pytest.raises(ValueError):client.call('bind_control_owner',**{**binding,field:replacement})
    client.close()
    assert not value.released.wait(.02)
    assert value.arm.permission and value.arm.op['operation_id']==binding['operation_id']


@pytest.mark.parametrize('mutation',[{'persistent_target':False},{'persistent_target':'true'},
                                    {'operation_type':'MODEL'},{'operation_type':'TRAJECTORY'},{'mode':'INLINE'}])
def test_binding_only_applies_to_explicit_persistent_direct_stream(owner_server,mutation):
    value=owner_server;binding=value.begin();value.arm.op.update(mutation);client=value.client()
    with pytest.raises(ValueError,match='persistent DIRECT STREAM'):
        client.call('bind_control_owner',**binding)
    client.close()
    assert not value.released.wait(.02)
    assert value.arm.permission and value.arm.op


def test_browser_sampled_source_keeps_live_sample_semantics_and_retires_on_eof(owner_server):
    station=owner_server;binding=station.begin(persistent=False);link=station.client()
    assert link.call('bind_control_owner',**binding,live_sample_owner=True)['accepted']
    assert station.arm.op['persistent_target'] is False
    link.close()
    assert station.released.wait(1)
    assert not station.arm.permission and not station.arm.op


def test_same_binding_retry_is_idempotent_but_rebind_and_late_eof_cannot_stop_replacement(owner_server):
    value=owner_server;binding=value.begin();client=value.client()
    first=client.call('bind_control_owner',**binding)
    assert client.call('bind_control_owner',**binding)==first
    with value.arm.lock:
        value.arm._hold('EXPLICIT_REPLACE');value.arm._finish_hold()
        replacement=value.begin()
    with pytest.raises(ValueError,match='already bound'):
        client.call('bind_control_owner',**replacement)
    client.close()
    assert value.released.wait(1)
    assert value.arm.permission and value.arm.op['operation_id']==replacement['operation_id']
    assert len(value.attempts)==1


@pytest.mark.parametrize('change',['session','principal'])
def test_late_eof_cannot_stop_changed_controller_session_or_principal(owner_server,change):
    value=owner_server;binding=value.begin();client=value.client()
    client.call('bind_control_owner',**binding)
    if change=='session':value.arm.session='new-session'
    else:value.arm.principal='new-operator'
    client.close()
    assert value.released.wait(1)
    assert value.arm.permission and value.arm.op['operation_id']==binding['operation_id']


def test_broken_bound_protocol_also_retires_its_exact_operation(owner_server):
    value=owner_server;binding=value.begin();client=value.client()
    client.call('bind_control_owner',**binding)
    client.sock.sendall(b'\xff\xff\xff\xff')  # Oversized frame closes this connection.
    assert value.released.wait(1)
    assert value.arm.op is None and not value.arm.permission
