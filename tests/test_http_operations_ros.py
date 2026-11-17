import asyncio
from concurrent.futures import Future
from types import SimpleNamespace
import types
import pytest
try:
    from soarm_station_system.dashboard_node import DashboardNode
    from soarm_station.http_operations import OperationRequests
    from soarm_station_system.conversion import goal_from_dict,stream_from_dict
    from rclpy.serialization import serialize_message
    AVAILABLE=True
except ImportError:AVAILABLE=False
pytestmark=pytest.mark.skipif(not AVAILABLE,reason='Sourced ROS required')

def body():
    return {'request_id':'request-one','arm_id':'arm1','runtime_boot_id':'boot','controller_session':'session',
        'operator_session_id':'operator','operation_type':'DIRECT','mode':'INLINE',
        'points':[{'positions':{'joint':0},'gripper':50,'time_s':1.}]}

def test_integer_json_joint_targets_are_normalized_to_ros_floats_without_angle_gates():
    for kind in ('DIRECT','TRAJECTORY'):
        request=body();request['operation_type']=kind;request['points'][0]['positions']['joint']=9
        goal=goal_from_dict(request,'operator');serialize_message(goal)
        positions=goal.input.inline_sequence.direct_steps[0].positions_rad if kind=='DIRECT' else goal.input.inline_sequence.joint_trajectory.points[0].positions
        assert list(positions)==[9.]
    for invalid in (True,'9',float('nan')):
        request=body();request['points'][0]['positions']['joint']=invalid
        with pytest.raises(ValueError):goal_from_dict(request,'operator')

def test_http_pending_id_deduplicates_and_cancels_exact_late_goal():
    from aiohttp import web
    from aiohttp.test_utils import TestClient,TestServer
    from soarm_station_interfaces.action import ExecuteOperation
    calls=[];pending=Future();result=Future();cancels=[]
    def send(goal,**kwargs):serialize_message(goal);calls.append(goal);return pending
    def cancel():
        cancels.append('only-owned-goal');future=Future();future.set_result(SimpleNamespace(goals_canceling=[1]));return future
    handle=SimpleNamespace(accepted=True,get_result_async=lambda:result,cancel_goal_async=cancel)
    owner=SimpleNamespace(operation=SimpleNamespace(send_goal_async=send),operation_requests=OperationRequests(),goals={})
    async def exercise():
        app=web.Application()
        app.router.add_post('/api/operate',types.MethodType(DashboardNode.operate,owner))
        app.router.add_get('/api/operations/{id}',types.MethodType(DashboardNode.operation_status,owner))
        app.router.add_post('/api/operations/{id}/cancel',types.MethodType(DashboardNode.cancel_operation,owner))
        async with TestClient(TestServer(app)) as client:
            for _ in range(2):
                response=await client.post('/api/operate',json=body())
                assert response.status==200 and await response.json()=={'request_id':'request-one'}
            assert len(calls)==1
            state=await (await client.get('/api/operations/request-one')).json()
            assert not state['done'] and state['feedback']=={}
            response=await client.post('/api/operations/request-one/cancel',json={'operator_session_id':'other'})
            assert response.status==403 and not cancels
            response=await client.post('/api/operations/request-one/cancel',json={'operator_session_id':'operator'})
            assert (await response.json())['accepted'] and not cancels
            pending.set_result(handle);assert cancels==['only-owned-goal']
            assert not (await (await client.get('/api/operations/request-one')).json())['done']
            result.set_result(SimpleNamespace(result=ExecuteOperation.Result(result_code='CANCELED',execution_completed=False)))
            response=await client.post('/api/operations/request-one/cancel',json={'operator_session_id':'operator'})
            assert (await response.json())['done'] and len(cancels)==1
    asyncio.run(exercise())


@pytest.mark.parametrize('changes',[{'request_id':'x'*65},{'request_id':False},{'request_id':0},{'operator_session_id':'x'*65},{'operator_session_id':12}])
def test_invalid_request_identity_is_rejected_before_ros_conversion_or_goal_send(changes):
    def forbidden(*args,**kwargs):pytest.fail('Invalid identity sent a goal')
    owner=SimpleNamespace(operation=SimpleNamespace(send_goal_async=forbidden),operation_requests=OperationRequests())
    class Request:
        async def json(self):return {**body(),**changes}
    with pytest.raises(ValueError):asyncio.run(DashboardNode.operate(owner,Request()))
