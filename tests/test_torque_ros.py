"""Typed ROS adapter checks with fake IPC; no node, worker or hardware starts."""
from types import SimpleNamespace

import pytest

try:
    import rclpy
    from rclpy.serialization import serialize_message
    from soarm_station_interfaces.srv import SetTorque
    from soarm_station_system.runtime_node import RuntimeNode
    ROS_AVAILABLE=True
except ImportError:
    ROS_AVAILABLE=False

pytestmark=pytest.mark.skipif(not ROS_AVAILABLE,
    reason='Run with sourced ROS and rebuilt SetTorque interfaces')


@pytest.mark.parametrize('enabled,result',[
    (True,{'success':True,'measured_torque':'ON','message':'Fresh readback confirms enabled'}),
    (False,{'success':True,'measured_torque':'OFF','message':'Fresh readback confirms disabled'}),
    (False,{'success':False,'measured_torque':'MIXED','message':'Some motors remain enabled'}),
])
def test_typed_torque_request_forwards_exact_identity_and_fresh_response(enabled,result):
    calls=[]
    def call(operation,**fields):
        calls.append((operation,fields));return result
    node=RuntimeNode.__new__(RuntimeNode)
    node.control_client=SimpleNamespace(call=call)
    node.snapshot={'arms':{'arm-test':{'health':{'torque':'STALE_VALUE'}}}}
    request=SetTorque.Request(arm_id='arm-test',expected_controller_session='exact-session',enabled=enabled)
    response=SetTorque.Response()
    assert RuntimeNode.set_torque(node,request,response) is response
    assert calls==[('set_torque',{'arm_id':'arm-test','expected_controller_session':'exact-session','enabled':enabled})]
    assert response.success is result['success']
    assert response.measured_torque==result['measured_torque']
    assert response.message==result['message']
    serialize_message(request);serialize_message(response)


def test_torque_ipc_failure_never_claims_cached_torque_or_previous_success():
    def fail(*args,**kwargs):raise OSError('native unavailable: '+'x'*700)
    node=RuntimeNode.__new__(RuntimeNode)
    node.control_client=SimpleNamespace(call=fail)
    node.snapshot={'arms':{'arm-test':{'health':{'torque':'ON'}}}}
    response=SetTorque.Response(success=True,measured_torque='ON',message='previous response')
    result=RuntimeNode.set_torque(node,SetTorque.Request(arm_id='arm-test',enabled=True),response)
    assert result.success is False and result.measured_torque=='UNKNOWN'
    assert result.message.startswith('native unavailable: ') and len(result.message)==512
    serialize_message(result)


def test_incomplete_native_torque_result_resets_partial_success():
    node=RuntimeNode.__new__(RuntimeNode)
    node.control_client=SimpleNamespace(call=lambda *args,**kwargs:{'success':True,'measured_torque':'ON'})
    response=RuntimeNode.set_torque(node,SetTorque.Request(),SetTorque.Response())
    assert response.success is False and response.measured_torque=='UNKNOWN'
    assert 'message' in response.message
    serialize_message(response)
