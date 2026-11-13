"""Run with the pinned Python3.12 LeRobot environment; no devices are opened."""
import time
from types import SimpleNamespace
import pytest
try:
    import torch
except ImportError:
    torch=None
pytestmark=pytest.mark.skipif(torch is None,reason='Run native model tests with the pinned Python3.12 environment')
from soarm.policy import NativePolicy,LocalPolicy,RemotePolicy,ModelOutcome
from soarm.model_runtime import ModelSupervisor

class Engine:
    def __init__(self,events):
        self.allowed=True;self.failed=False;self.events=events;self.acks=0;self.records=0;self.task=''
        self.runtime=SimpleNamespace(generation=1,current=SimpleNamespace(capture_time=time.monotonic()))
        self._hold=SimpleNamespace(started_at=1.,reason='action starvation')
        self.sequence=[]
    def dispatch_allowed(self):return self.sequence.pop(0) if self.sequence else self.allowed
    def get_action(self,obs):return torch.tensor([.02,50.])
    def acknowledge_hold(self):
        assert self.events and self.events[-1]=='hold'
        self.events.append('ack');self.acks+=1
    def record_dispatch(self):
        assert self.events and self.events[-1]=='send'
        self.events.append('record');self.records+=1
    def pause(self):self.events.append('pause')
    def reset(self):self.events.append('reset');self.runtime.generation+=1
    def resume(self):self.events.append('resume')
    def set_task(self,task):self.task=task;self.events.append('task')

def session():
    events=[];p=NativePolicy()
    p._initialize({'action_fps':10,'action_features':[{'joint':'joint','unit':'radian'},{'joint':'gripper','unit':'percent'}]})
    p.engine=Engine(events)
    return p,events

def test_native_hold_is_not_acknowledged_until_owner_hold_succeeds():
    policy,events=session();policy.engine.allowed=False
    outcome=policy.poll({},time.monotonic())
    assert outcome.state=='HOLD' and policy.engine.acks==0 and events==[]
    class Backend:
        def hold(self):events.append('hold');return {'torque':'ON'}
    owner=SimpleNamespace(backend=Backend(),health_data={},q={'joint':0.},gripper=50.,model_state='',model_reason='')
    supervisor=ModelSupervisor(owner);supervisor.moving=True
    supervisor._native_hold(outcome)
    assert events==['hold','ack'] and policy.engine.acks==1
    supervisor._native_hold(outcome)
    assert policy.engine.acks==1 and events==['hold','ack']
    assert policy.gate().state=='WAIT'

def test_failed_physical_hold_is_never_acknowledged():
    policy,events=session();policy.engine.allowed=False;outcome=policy.poll({},time.monotonic())
    class Backend:
        def hold(self):raise RuntimeError('bus unavailable')
    owner=SimpleNamespace(backend=Backend(),health_data={},q={'joint':0.},gripper=50.)
    supervisor=ModelSupervisor(owner);supervisor.moving=True
    with pytest.raises(RuntimeError):supervisor._native_hold(outcome)
    assert policy.engine.acks==0

def test_native_record_occurs_after_real_send_callback_only():
    policy,events=session();outcome=policy.poll({},time.monotonic())
    assert outcome.state=='ACTION' and policy.engine.records==0
    sent,state=policy.commit(outcome,lambda:events.append('send'),lambda:events.append('hold'))
    assert sent and events==['send','record'] and policy.engine.records==1

def test_final_native_denial_holds_and_does_not_send():
    policy,events=session();outcome=policy.poll({},time.monotonic())
    policy.engine.sequence=[True,False]
    sent,state=policy.commit(outcome,lambda:events.append('send'),lambda:events.append('hold'))
    assert not sent and state.state=='HOLD' and events==['hold','ack'] and policy.engine.records==0

def test_native_source_age_and_generation_are_not_replaced_by_receipt_ttl():
    policy,events=session();outcome=policy.poll({},time.monotonic()-.3)
    assert outcome.state=='ACTION' # Valid native source budget, not the old200ms wrapper TTL.
    stale=ModelOutcome('ACTION',outcome.target,.1,time.monotonic()-6,5.,policy.execution_generation,policy)
    assert policy.gate(stale).state=='FAULT'
    policy.engine.runtime.generation+=1
    assert policy.gate(outcome).state=='WAIT'
    assert events==[]

def test_cached_local_chunk_and_remote_actions_keep_acquisition_time():
    local=object.__new__(LocalPolicy);local.chunk_capture=None
    local.policy=SimpleNamespace(count_queued_actions=lambda:3)
    local._observe({},100.);local._observe({},103.)
    assert local._action_source(103.)==100.
    remote=object.__new__(RemotePolicy);remote.engine=SimpleNamespace(runtime=SimpleNamespace(current=SimpleNamespace(capture_time=91.)))
    assert remote._action_source(105.)==91.

def test_task_reset_reuses_engine_and_never_closes_weights():
    policy,events=session();engine=policy.engine
    policy.reset_task('',('operation',1));policy.reset_task('new task',('operation',2))
    assert policy.engine is engine and 'reset' in events and events.count('task')==1
    assert events.count('pause')==1 and 'stop' not in events


def test_closed_remote_engine_is_not_reusable_even_without_failure_text():
    import threading
    policy,events=session();policy.engine._stop_event=threading.Event();policy.engine._stop_event.set()
    assert policy.terminally_failed
