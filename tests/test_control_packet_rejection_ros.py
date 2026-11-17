"""A bad control packet never closes an otherwise owned DIRECT stream."""
import asyncio
import copy
import json
from types import SimpleNamespace
import types
import pytest
try:
    from soarm_station_system.dashboard_node import DashboardNode
    import soarm_station_system.dashboard_node as module
    from soarm_station_interfaces.msg import OperationFeedback
    AVAILABLE=True
except ImportError:AVAILABLE=False
pytestmark=pytest.mark.skipif(not AVAILABLE,reason='Sourced ROS and rebuilt interfaces required')


def fixture(monkeypatch,delayed=False):
    events=[]
    class NativeClient:
        def __init__(self,path):assert path=='mock-native-socket'
        def call(self,operation,**binding):
            assert operation=='bind_control_owner';events.append('bound')
            return {'accepted':True,**binding}
        def close(self):events.append('owner_closed')
    monkeypatch.setattr(module,'Client',NativeClient)
    owner=SimpleNamespace(states={'arm1':{}},joints={},goals={},stopping=False,
        config={'socket':'mock-native-socket'},command=SimpleNamespace(publish=lambda m:events.append(('published',m))))
    async def submit(body,principal,feedback=None,*,persistent_target=False):
        assert persistent_target and principal=='operator'
        owner.goals['request']={'future':asyncio.get_running_loop().create_future()}
        def grant():
            status=OperationFeedback(arm_id='arm1',runtime_boot_id='boot',controller_session='session',
                operation_id='owned-op',operation_epoch=1,phase='READY',stream_ready=True,
                window_id='current-window',window_valid_for_ms=250)
            feedback(SimpleNamespace(feedback=SimpleNamespace(status=status)))
        owner.grant=grant
        if not delayed:grant()
        return 'request',SimpleNamespace(cancel_goal_async=lambda:events.append('canceled'))
    owner.submit=submit;owner.control=types.MethodType(DashboardNode.control,owner)
    return owner,events


def command():
    return {'arm_id':'arm1','runtime_boot_id':'boot','controller_session':'session','operation_id':'owned-op',
        'operation_epoch':1,'window_id':'current-window','sequence':10,'input_id':'current',
        'frame_kind':'COMMAND','payload_kind':'DIRECT_JOINT_TARGET',
        'body':{'positions':{'joint':12.5},'gripper':50.,'gripper_unit':'percent'}}


async def hello(ws):
    await ws.send_json({'arm_id':'arm1','operator_session_id':'operator','runtime_boot_id':'boot',
        'controller_session':'session','operation_type':'DIRECT','mode':'STREAM','input_semantics':'persistent_target'})


async def receive_type(ws,kind):
    for _ in range(100):
        message=await ws.receive_json(timeout=1)
        if message.get('type')==kind:return message
    raise AssertionError('Expected '+kind)


def test_invalid_packets_keep_lifetime_and_next_valid_command_is_published(monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient,TestServer
    owner,events=fixture(monkeypatch)
    async def exercise():
        app=web.Application();app.router.add_get('/ws/control',owner.control)
        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect('/ws/control',protocols=['soarm_station']) as ws:
                await hello(ws);await receive_type(ws,'grant')
                invalid=['{',[],{**command(),'arm_id':'different'}, {**command(),'sequence':True},
                    {**command(),'frame_kind':'HEARTBEAT'}, {**command(),'semantic_revision':'1'}]
                for value in (True,'1',float('nan')):
                    packet=copy.deepcopy(command());packet['body']['positions']['joint']=value;invalid.append(packet)
                for packet in invalid:
                    await ws.send_str(packet if isinstance(packet,str) else json.dumps(packet))
                    rejection=await receive_type(ws,'input_rejected')
                    assert len(rejection['reason'])<=512 and not ws.closed
                    assert events==['bound']
                await ws.send_bytes(b'not JSON text');await receive_type(ws,'input_rejected')
                await ws.send_json(command())
                for _ in range(50):
                    if len(events)>1:break
                    await asyncio.sleep(.005)
                assert len(events)==2 and events[1][0]=='published'
                assert list(events[1][1].direct_target.positions_rad)==[12.5]
            for _ in range(50):
                if 'canceled' in events:break
                await asyncio.sleep(.005)
            assert events[-2:]==['owner_closed','canceled']
    asyncio.run(exercise())


def test_prebind_packet_is_dropped_then_fresh_packet_works_after_owner_binding(monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient,TestServer
    owner,events=fixture(monkeypatch,delayed=True)
    async def exercise():
        app=web.Application();app.router.add_get('/ws/control',owner.control)
        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect('/ws/control',protocols=['soarm_station']) as ws:
                await hello(ws);await receive_type(ws,'state')
                await ws.send_json(command());rejected=await receive_type(ws,'input_rejected')
                assert 'ownership is not ready' in rejected['reason'] and events==[]
                owner.grant();await receive_type(ws,'grant')
                assert events==['bound'] # The old packet was not queued/replayed.
                await ws.send_json({**command(),'sequence':11,'input_id':'fresh'})
                for _ in range(50):
                    if len(events)>1:break
                    await asyncio.sleep(.005)
                assert events[1][0]=='published' and events[1][1].sequence==11
    asyncio.run(exercise())
