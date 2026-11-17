from concurrent.futures import Future
from types import SimpleNamespace
import pytest
from soarm_station.http_operations import OperationRequests,RequestConflict

class Goal:
    accepted=True
    def __init__(self):self.result=Future();self.cancel_calls=0;self.cancel_reply=Future()
    def get_result_async(self):return self.result
    def cancel_goal_async(self):self.cancel_calls+=1;return self.cancel_reply

def register(registry,identifier='one',principal='owner',**values):
    pending=Future();calls=[]
    def send(feedback):calls.append(feedback);return pending
    body={'request_id':identifier,'arm_id':'arm',**values}
    key=registry.register(body,principal,send,lambda wrapped:wrapped.result)
    return key,pending,calls,body

def test_pending_cancel_applies_only_to_its_late_handle_and_waits_for_real_result():
    requests=OperationRequests();key,pending,_,_=register(requests)
    _,other_pending,_,_=register(requests,'other');other=Goal();other_pending.set_result(other)
    assert requests.snapshot(key)['feedback']=={} and not requests.snapshot(key)['done']
    assert requests.cancel(key,'owner')['accepted'] and not requests.cancel(key,'owner')['done']
    goal=Goal();pending.set_result(goal)
    assert goal.cancel_calls==1 and other.cancel_calls==0
    goal.cancel_reply.set_result(SimpleNamespace(goals_canceling=[object()]))
    assert not requests.snapshot(key)['done']
    result={'result_code':'CANCELED','execution_completed':False}
    goal.result.set_result(SimpleNamespace(result=result))
    assert requests.snapshot(key)['result']==result
    assert requests.cancel(key,'owner')=={'accepted':True,'done':True,'reason':'Operation already ended'}
    assert goal.cancel_calls==1

def test_identical_requests_do_not_send_another_goal_and_conflicts_are_rejected():
    requests=OperationRequests();key,pending,_,body=register(requests)
    def no_send(_):pytest.fail('A repeated request must not execute again')
    assert requests.register(body,'owner',no_send,lambda x:x)==key
    with pytest.raises(RequestConflict):requests.register({**body,'arm_id':'different'},'owner',no_send,lambda x:x)
    with pytest.raises(RequestConflict):requests.register(body,'other-owner',no_send,lambda x:x)
    with pytest.raises(PermissionError):requests.cancel(key,'other-owner')
    assert not requests.snapshot(key)['cancel_requested']

def test_rejection_and_uncertain_admission_cannot_be_reported_as_success():
    requests=OperationRequests();key,pending,_,_=register(requests)
    pending.set_result(SimpleNamespace(accepted=False))
    status=requests.snapshot(key)
    assert status['done'] and not status['result']['execution_completed']
    assert status['result']['result_code']=='REJECTED'
    key,pending,_,_=register(requests,'uncertain')
    pending.set_exception(RuntimeError('ROS connection failed'))
    status=requests.snapshot(key)
    assert not status['done'] and status['result'] is None and 'not be confirmed' in status['reason']
    assert not requests.cancel(key,'owner')['accepted']

def test_failed_cancel_can_retry_exact_goal_without_creating_or_stopping_another():
    requests=OperationRequests();key,pending,_,_=register(requests);goal=Goal();pending.set_result(goal)
    requests.cancel(key,'owner');goal.cancel_reply.set_result(SimpleNamespace(goals_canceling=[]))
    assert not requests.snapshot(key)['done'] and 'not confirmed' in requests.snapshot(key)['reason']
    goal.cancel_reply=Future();requests.cancel(key,'owner');assert goal.cancel_calls==2
    goal.cancel_reply.set_result(SimpleNamespace(goals_canceling=[object()]))
    goal.result.set_result(SimpleNamespace(result={'result_code':'CANCELED'}))
    assert requests.cancel(key,'owner')['done']

def test_record_bound_prunes_completed_only_and_never_discards_pending_owner():
    requests=OperationRequests(capacity=2);one,future,_,_=register(requests)
    two,_,_,_=register(requests,'two')
    with pytest.raises(RuntimeError,match='unresolved'):register(requests,'three')
    future.set_result(SimpleNamespace(accepted=False))
    register(requests,'three')
    assert not requests.contains(one) and requests.contains(two) and len(requests.records)==2

@pytest.mark.parametrize('identifier',['x'*65,'../bad path',12])
def test_request_identifier_contract(identifier):
    with pytest.raises(ValueError):register(OperationRequests(),identifier)

@pytest.mark.parametrize('cancel_first',[True,False])
def test_accepted_handle_remains_cancelable_when_result_observation_fails(cancel_first):
    class UnobservedGoal(Goal):
        def get_result_async(self):raise RuntimeError('Result observer unavailable')
    requests=OperationRequests();key,pending,_,_=register(requests);goal=UnobservedGoal()
    if cancel_first:assert requests.cancel(key,'owner')['accepted']
    pending.set_result(goal)
    if not cancel_first:assert requests.cancel(key,'owner')['accepted']
    assert goal.cancel_calls==1
    status=requests.snapshot(key)
    assert not status['done'] and 'could not be monitored' in status['reason']

def test_terminal_result_wins_over_late_cancellation_error():
    requests=OperationRequests();key,pending,_,_=register(requests);goal=Goal();pending.set_result(goal)
    requests.cancel(key,'owner');goal.result.set_result(SimpleNamespace(result={'result_code':'CANCELED'}))
    goal.cancel_reply.set_exception(RuntimeError('Late response failure'))
    assert requests.snapshot(key)['done'] and not requests.snapshot(key)['reason']
    assert requests.cancel(key,'owner')['accepted']

def test_cancel_arriving_before_submit_prevents_late_execution_without_faking_completion():
    requests=OperationRequests()
    cancellation=requests.cancel('late','owner')
    assert cancellation['accepted'] and not cancellation['done']
    def forbidden(_):pytest.fail('Canceled delayed submission must not create a ROS goal')
    requests.register({'request_id':'late','arm_id':'arm'},'owner',forbidden,lambda x:x)
    status=requests.snapshot('late')
    assert status['done'] and status['result']['result_code']=='CANCELED'
    assert status['result']['task_outcome']=='NOT_EXECUTED'

def test_cancel_tombstones_are_bounded_and_do_not_cross_principals():
    requests=OperationRequests(capacity=2)
    for name in ('one','two'):requests.cancel(name,'foreign-owner')
    with pytest.raises(RuntimeError,match='unresolved pre-submission'):requests.cancel('three','foreign-owner')
    assert len(requests.canceled_before_submission)==2
    _,_,calls,_=register(requests,'three',principal='owner')
    assert len(calls)==1
    with pytest.raises(PermissionError):requests.cancel('three','foreign-owner')


def test_acknowledged_unknown_cancellation_is_not_evicted_under_pressure():
    requests=OperationRequests(capacity=1)
    assert requests.cancel('first','owner')['accepted']
    with pytest.raises(RuntimeError):requests.cancel('second','owner')
    def forbidden(_):pytest.fail('Previously canceled request was allowed to execute')
    requests.register({'request_id':'first'},'owner',forbidden,lambda x:x)
    assert requests.snapshot('first')['result']['result_code']=='CANCELED'
