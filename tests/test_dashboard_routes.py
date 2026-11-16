import asyncio
import json
import threading
import types
import pytest

try:
    from soarm_station_system.dashboard_node import DashboardNode
    from soarm_station.dashboard_data import resources
    ROS_AVAILABLE=True
except ImportError:ROS_AVAILABLE=False
pytestmark=pytest.mark.skipif(not ROS_AVAILABLE,reason='Sourced ROS environment required')


@pytest.mark.parametrize('operation_id',[None,'owned-operation'])
def test_scoped_hold_routes_exact_identity_to_native_owned_hold(operation_id):
    calls=[]
    async def scoped(*args):calls.append(args);return {'success':False,'message':'Owner changed'}
    def global_hold(*args):raise AssertionError('Scoped cleanup must never use global Hold')
    owner=types.SimpleNamespace(leader_registry=types.SimpleNamespace(hold_callback=scoped),
        hold_client=types.SimpleNamespace(call_async=global_hold))
    class Request:
        async def json(self):return {'arm_id':'arm1','controller_session':'session',
            'operator_session_id':'operator','operation_id':operation_id}
    response=asyncio.run(DashboardNode.hold(owner,Request()))
    assert json.loads(response.text)=={'success':False,'message':'Owner changed'}
    assert calls==[('arm1','session','operator',operation_id)]


@pytest.mark.parametrize('scope',[
    {'controller_session':'session'}, {'operator_session_id':'operator'}, {'operation_id':None},
    {'controller_session':'session','operator_session_id':'operator'},
    {'controller_session':'session','operator_session_id':'','operation_id':None},
    {'controller_session':'session','operator_session_id':'operator','operation_id':''},
])
def test_partial_hold_scope_is_rejected_without_downgrading_to_global_hold(scope):
    def forbidden(*args):raise AssertionError('Invalid scope reached a hold backend')
    owner=types.SimpleNamespace(leader_registry=types.SimpleNamespace(hold_callback=forbidden),
        hold_client=types.SimpleNamespace(call_async=forbidden))
    class Request:
        async def json(self):return {'arm_id':'arm1',**scope}
    with pytest.raises(ValueError):asyncio.run(DashboardNode.hold(owner,Request()))


def test_explicit_unscoped_hold_keeps_existing_ros_route(monkeypatch):
    import soarm_station_system.dashboard_node as module
    calls=[]
    async def resolved(value):return value
    monkeypatch.setattr(module,'resolved',resolved);monkeypatch.setattr(module,'asdict',lambda value:value)
    def call(request):calls.append(request.arm_id);return {'success':True,'reason':'explicit hold'}
    owner=types.SimpleNamespace(hold_client=types.SimpleNamespace(call_async=call))
    class Request:
        async def json(self):return {'arm_id':'arm1'}
    assert json.loads(asyncio.run(DashboardNode.hold(owner,Request())).text)['success']
    assert calls==['arm1']


def test_authenticated_metadata_and_public_allowlisted_assets(tmp_path,monkeypatch):
    from aiohttp.test_utils import TestClient,TestServer
    token='t'*32
    root=tmp_path/'web';root.mkdir();(root/'index.html').write_text('Dashboard')
    (root/'app.js').write_text('export const application=true;')
    (root/'payload-model.js').write_text('export const payload=true;')
    (tmp_path/'secrets.env').write_text('private')
    monkeypatch.setenv('SOARM_STATION_ROOT',str(tmp_path))
    config={'device_id':'test','web':{'trusted_network_access':False},'arms':[{'id':'arm1','backend':'mock','joints':[{'name':'joint','min_rad':-1.,'max_rad':1.,'velocity_rad_s':1.}]}],'cameras':[],'profiles':{}}
    async def unused(request):raise AssertionError('Actuation route was not requested')
    owner=types.SimpleNamespace(config=config,public_resources=resources(config),motor_records={},motor_record_errors={},states={},inference={},joints={},images={},upload={},events=[],workflows={},decisions=[])
    for name in ('permission','hold','stream_input','operate','operation_status','start_workflow','history','image','control','camera_stream'):setattr(owner,name,unused)
    for name in ('index','asset','api_resources','graph_status','graph_sample','graph_watch','resource_view','state','status','camera_stream'):
        setattr(owner,name,types.MethodType(getattr(DashboardNode,name),owner))
    owner.images={'forward':(b'\xff\xd8camera',1)};owner.stopping=False
    watch_calls=[];request_thread=threading.get_ident()
    def watch(body):
        watch_calls.append((dict(body),threading.get_ident()))
        if body.get('operation') not in ('acquire','renew','release'):raise ValueError('Unknown watch operation')
        return {'schema_version':1,'accepted':True,'lease_id':'test-lease','expires_in_ms':12000}
    owner.graph_observer=types.SimpleNamespace(snapshot=lambda:{'schema_version':1,'nodes':[],'topics':[],'services':[],'actions':[],'edges':[]},
        sample=lambda body:{'schema_version':1,'accepted':True,'topic':body['topic'],'state':'sampling','sample_id':'test'},watch=watch)
    async def exercise():
        async with TestClient(TestServer(DashboardNode.create_app(owner,token))) as client:
            for path in ('/api/status','/api/resources','/api/graph','/api/v1/ros/graph'):
                async with client.get(path) as response:assert response.status==401
            for name in ('app.js','payload-model.js'):
                async with client.get('/assets/'+name) as response:assert response.status==200 and 'javascript' in response.content_type
            for path in ('/assets/secrets.env','/assets/unknown.js','/assets/%2e%2e%2fsecrets.env'):
                async with client.get(path) as response:assert response.status==404
            headers={'Authorization':'Bearer '+token}
            async with client.get('/api/resources',headers=headers) as response:
                data=await response.json();assert data['schema_version']==1 and data['arms'][0]['id']=='arm1'
            async with client.get('/api/status',headers=headers) as response:
                data=await response.json();assert data['motor_status']['arm1']['motors'][0]['position'] is None
            for path in ('/api/graph','/api/v1/ros/graph'):
                async with client.get(path,headers=headers) as response:assert (await response.json())['schema_version']==1
            async with client.ws_connect('/ws/camera/forward',protocols=['soarm_station',token]) as ws:
                assert ws.protocol=='soarm_station'
                assert (await ws.receive(timeout=2)).data.startswith(b'\xff\xd8')
                owner.stopping=True
            for path in ('/api/graph/sample','/api/v1/ros/sample'):
                async with client.post(path,json={'topic':'/arbitrary','duration_s':5},headers=headers) as response:assert (await response.json())['accepted']
                async with client.post(path,json={'topic':'/arbitrary'}) as response:assert response.status==401
            watch_path='/api/v1/ros/watch'
            async with client.post(watch_path,json={'operation':'acquire'}) as response:assert response.status==401
            assert watch_calls==[]
            requests=[{'operation':'acquire','client_id':'81255bed-f260-4430-b00c-5913871ff6e2'},
                {'operation':'renew','lease_id':'test-lease'},{'operation':'release','lease_id':'test-lease'}]
            for body in requests:
                async with client.post(watch_path,json=body,headers=headers) as response:
                    result=await response.json()
                    assert response.status==200 and result['accepted'] and result['expires_in_ms']==12000
                    assert response.headers['Cache-Control']=='no-store'
            assert [body for body,thread in watch_calls]==requests
            assert all(thread!=request_thread for body,thread in watch_calls)
            for body in ([],None,'text'):
                async with client.post(watch_path,json=body,headers=headers) as response:
                    result=await response.json()
                    assert response.status==400 and not result['accepted'] and result['schema_version']==1
            assert len(watch_calls)==3
            async with client.post(watch_path,json={'operation':'invalid'},headers=headers) as response:
                assert response.status==400 and (await response.json())['reason']=='Unknown watch operation'
    asyncio.run(exercise())


def test_bad_diagnostics_keep_last_valid_motor_cache_with_visible_error():
    from diagnostic_msgs.msg import DiagnosticArray,DiagnosticStatus,KeyValue
    arm={'id':'arm1','backend':'mock','joints':[{'name':'joint','min_rad':-1.,'max_rad':1.,'velocity_rad_s':1.}]}
    owner=types.SimpleNamespace(config={'arms':[arm]},motor_records={},motor_record_errors={})
    def send(value):
        message=DiagnosticArray(status=[DiagnosticStatus(name='arm1/motors',values=[KeyValue(key='status',value=json.dumps(value))])])
        DashboardNode.motor_health(owner,message)
    valid={'arm_id':'arm1','health_time_ns':1,'position_time_ns':1,'health_source':'measured','motors':[{'name':'joint','id':8}]}
    send(valid);saved=owner.motor_records['arm1']
    for invalid in ([],None,'text',{'arm_id':'arm1','motors':{}},{'arm_id':'arm1','positions':[]}):
        send(invalid)
        assert owner.motor_records['arm1'] is saved and owner.motor_record_errors['arm1']
    send(valid);assert owner.motor_record_errors=={}


def test_prepared_resources_preserve_static_profile_ids_with_managed_looking_prefix():
    original={'profiles':[{'id':'pm_custom','arm_id':'arm1'},{'id':'legacy','arm_id':'arm1'}],
              'arms':[{'id':'arm1','profile_ids':['pm_custom','legacy']}]}
    prepared=[{'id':'pm_prepared_revision','arm_id':'arm1'}]
    owner=types.SimpleNamespace(public_resources=original,
        policy_manager=types.SimpleNamespace(public_runtime_profiles=lambda:prepared))
    result=DashboardNode.resource_view(owner)
    assert [p['id'] for p in result['profiles']]==['pm_custom','legacy','pm_prepared_revision']
    assert result['arms'][0]['profile_ids']==['pm_custom','legacy','pm_prepared_revision']
    assert original['arms'][0]['profile_ids']==['pm_custom','legacy']


def test_leader_cleanup_uses_native_snapshot_without_policy_ipc_and_precedes_policy_store_cleanup(monkeypatch):
    from aiohttp.test_utils import TestClient,TestServer
    calls=[]
    async def unused(request):raise AssertionError('Route was not requested')
    def expanded_state():raise AssertionError('Leader cleanup must not query policy IPC')
    manager=types.SimpleNamespace(close=lambda:calls.append('policy'))
    native={'arms':{'arm1':{'controller_session':'controller','operation_id':'','permission':False,'hold_pending':False,'phase':'HOLD'}}}
    class NativeClient:
        def __init__(self,path):assert path=='/mock/native.sock'
        def call(self,operation):assert operation=='snapshot';calls.append('native');return native
        def close(self):pass
    monkeypatch.setattr('soarm_station_system.dashboard_node.Client',NativeClient)
    owner=types.SimpleNamespace(config={'web':{},'arms':[],'socket':'/mock/native.sock'},public_resources={'arms':[]},
        states={'arm1':{'controller_session':'controller','operation_id':'owned','permission':True}},
        state=expanded_state,policy_manager=manager)
    for name in ('index','status','history','permission','hold','stream_input','operate','operation_status',
                 'start_workflow','image','control','camera_stream','asset','api_resources','graph_status','graph_sample','graph_watch'):
        setattr(owner,name,unused)
    app=DashboardNode.create_app(owner)
    original_close=owner.leader_registry.close
    async def close_leader():
        calls.append('leader')
        await original_close()
    owner.leader_registry.close=close_leader
    async def exercise():
        async with TestClient(TestServer(app)):
            result=await owner.leader_registry._hold_current({'arm_id':'arm1','controller_session':'controller',
                'operation_id':'owned','operator_session_id':'operator'})
            assert result['success']
            assert calls==['native']
        assert calls==['native','leader','policy']
    asyncio.run(exercise())


def test_leader_start_reads_native_permission_off_http_thread_before_ros_cache_updates(monkeypatch):
    import soarm_station_system.dashboard_node as module
    calls=[];http_thread=threading.get_ident()
    class NativeClient:
        def __init__(self,path):assert path=='test-native-socket'
        def call(self,operation):
            assert operation=='snapshot'
            calls.append(('read',threading.get_ident()))
            return {'arms':{'arm1':{'runtime_boot_id':'boot','controller_session':'controller',
                'operation_id':'','permission':True,'positions':{'pan':0.},'age_ms':1.}}}
        def close(self):calls.append(('close',threading.get_ident()))
    monkeypatch.setattr(module,'Client',NativeClient)
    async def unused(request):raise AssertionError('Unrelated route was requested')
    owner=types.SimpleNamespace(config={'web':{},'arms':[],'socket':'test-native-socket'},
        public_resources=resources({'arms':[{'id':'arm1','backend':'mock','joints':[{'name':'pan','min_rad':-2.,'max_rad':2.,'velocity_rad_s':1.}]}]}),
        states={'arm1':{'permission':False,'feedback_valid':False}})
    for name in ('index','status','history','permission','hold','stream_input','operate','operation_status',
                 'start_workflow','image','control','camera_stream','asset','api_resources','graph_status','graph_sample','graph_watch'):
        setattr(owner,name,unused)
    DashboardNode.create_app(owner)
    class Socket:
        closed=False
        def __init__(self):self.sent=[]
        async def send_json(self,value):self.sent.append(value)
    socket=Socket()
    owner.leader_registry.hosts['mac']={'id':'mac','label':'Mac','ws':socket,'active':None,
        'devices':[{'id':'leader','configured':True,'joint_names':['pan']}],'state':'IDLE'}
    class Request:
        match_info={'host_id':'mac'}
        async def json(self):return {'device_id':'leader','arm_id':'arm1','runtime_boot_id':'boot',
            'controller_session':'controller','operator_session_id':'operator'}
    async def check():
        response=await owner.leader_registry.start_route(Request())
        assert json.loads(response.text)['accepted']
    asyncio.run(check())
    assert len(socket.sent)==1 and socket.sent[0]['type']=='start'
    assert [name for name,_ in calls]==['read','close']
    assert all(thread!=http_thread for _,thread in calls)
    assert owner.states['arm1']['permission'] is False


@pytest.mark.parametrize('semantics,persistent,cancel_failure',[
    (None,False,False),('live_sample',False,False),('persistent_target',True,False),('persistent_target',True,True)])
def test_control_socket_selects_explicit_intent_and_cancels_only_its_action(semantics,persistent,cancel_failure,monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient,TestServer
    from soarm_station_interfaces.msg import OperationFeedback
    import soarm_station_system.dashboard_node as module
    submitted=[];canceled=[];lifetime=[]
    class NativeClient:
        def __init__(self,path):assert path=='test-native-socket'
        def call(self,operation,**binding):
            assert operation=='bind_control_owner'
            assert binding=={'arm_id':'arm1','controller_session':'controller','principal':'browser-owner','operation_id':'owned-action'}
            lifetime.append('bound')
            return {'accepted':True,**binding}
        def close(self):lifetime.append('closed')
    monkeypatch.setattr(module,'Client',NativeClient)
    owner=types.SimpleNamespace(states={'arm1':{}},joints={},goals={},stopping=False,
        config={'socket':'test-native-socket'},command=types.SimpleNamespace(publish=lambda message:lifetime.append('command')))
    def cancel():
        canceled.append('owned-action')
        if cancel_failure:raise RuntimeError('ROS cancellation unavailable')
    async def submit(body,principal,feedback=None,*,persistent_target=False):
        submitted.append((dict(body),principal,persistent_target))
        future=asyncio.get_running_loop().create_future()
        owner.goals['gateway-request']={'future':future}
        status=OperationFeedback(arm_id='arm1',runtime_boot_id='boot',controller_session='controller',
            operation_id='owned-action',operation_epoch=1,phase='READY',stream_ready=True,
            window_id='window',window_valid_for_ms=250)
        feedback(types.SimpleNamespace(feedback=types.SimpleNamespace(status=status)))
        return 'gateway-request',types.SimpleNamespace(cancel_goal_async=cancel)
    owner.submit=submit
    owner.control=types.MethodType(DashboardNode.control,owner)
    async def exercise():
        app=web.Application();app.router.add_get('/ws/control',owner.control)
        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect('/ws/control',protocols=['soarm_station']) as ws:
                goal={'arm_id':'arm1','operator_session_id':'browser-owner',
                    'operation_type':'DIRECT','mode':'STREAM','runtime_boot_id':'boot','controller_session':'controller'}
                if semantics is not None:goal['input_semantics']=semantics
                await ws.send_json(goal)
                message=await ws.receive_json(timeout=1)
                assert message['type']=='grant'
                assert submitted[0][1:] == ('browser-owner',persistent)
                assert lifetime==(['bound'] if persistent else [])
                await ws.send_json({'arm_id':'arm1','runtime_boot_id':'boot','controller_session':'controller',
                    'operation_id':'owned-action','operation_epoch':1,'window_id':'window','sequence':1,
                    'input_id':'current','frame_kind':'COMMAND','payload_kind':'DIRECT_JOINT_TARGET',
                    'body':{'positions':{'joint':0.},'gripper':50.,'gripper_unit':'percent'}})
                for _ in range(20):
                    if 'command' in lifetime:break
                    await asyncio.sleep(.005)
                assert 'command' in lifetime
                if persistent:assert lifetime.index('bound')<lifetime.index('command')
            for _ in range(20):
                if canceled:break
                await asyncio.sleep(.005)
            assert canceled==['owned-action']
            assert ('closed' in lifetime)==persistent
    asyncio.run(exercise())


def test_control_socket_rejects_unknown_intent_before_submitting_an_action():
    from aiohttp import web
    from aiohttp.test_utils import TestClient,TestServer
    async def submit(*args,**kwargs):raise AssertionError('Invalid semantics submitted an action')
    owner=types.SimpleNamespace(states={'arm1':{}},joints={},goals={},stopping=False,submit=submit)
    owner.control=types.MethodType(DashboardNode.control,owner)
    async def exercise():
        app=web.Application();app.router.add_get('/ws/control',owner.control)
        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect('/ws/control',protocols=['soarm_station']) as ws:
                await ws.send_json({'arm_id':'arm1','operator_session_id':'browser-owner',
                    'operation_type':'DIRECT','mode':'STREAM','input_semantics':'unknown'})
                result=await ws.receive_json(timeout=1)
                assert result['type']=='error' and result['reason']=='Unknown DIRECT input semantics'
    asyncio.run(exercise())


def test_late_persistent_admission_is_canceled_after_gateway_wait_fails(monkeypatch):
    import soarm_station_system.dashboard_node as module
    canceled=[]
    async def exercise():
        future=asyncio.get_running_loop().create_future()
        owner=types.SimpleNamespace(operation=types.SimpleNamespace(send_goal_async=lambda *a,**kw:future))
        async def interrupted_wait(pending,timeout=5):raise TimeoutError('response delayed')
        monkeypatch.setattr(module,'resolved',interrupted_wait)
        body={'arm_id':'arm1','runtime_boot_id':'boot','controller_session':'controller',
            'operation_type':'DIRECT','mode':'STREAM'}
        with pytest.raises(TimeoutError):
            await DashboardNode.submit(owner,body,'owner',persistent_target=True)
        future.set_result(types.SimpleNamespace(accepted=True,cancel_goal_async=lambda:canceled.append('late-owned-goal')))
        await asyncio.sleep(0)
        assert canceled==['late-owned-goal']
    asyncio.run(exercise())


def test_canceled_native_owner_bind_closes_even_a_late_worker_connection(monkeypatch):
    import soarm_station_system.dashboard_node as module
    started=threading.Event();release=threading.Event();events=[]
    binding={'arm_id':'arm1','controller_session':'controller','principal':'owner','operation_id':'owned'}
    class NativeClient:
        def __init__(self,path):pass
        def call(self,operation,**arguments):
            assert operation=='bind_control_owner' and arguments==binding
            started.set();assert release.wait(2)
            events.append('bound-late');return {'accepted':True,**arguments}
        def close(self):events.append('closed')
    monkeypatch.setattr(module,'Client',NativeClient)
    async def exercise():
        task=asyncio.create_task(module.bind_control_owner('socket',binding))
        assert await asyncio.to_thread(started.wait,1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert events==['closed']
        release.set()
        for _ in range(100):
            if events.count('closed')>=2:break
            await asyncio.sleep(.005)
        assert events[-2:]==['bound-late','closed']
    try:asyncio.run(exercise())
    finally:release.set()


def test_native_owner_binding_rejection_closes_the_lifetime_client(monkeypatch):
    import soarm_station_system.dashboard_node as module
    closed=[]
    class NativeClient:
        def __init__(self,path):pass
        def call(self,*args,**kwargs):raise ValueError('operation no longer owned')
        def close(self):closed.append(True)
    monkeypatch.setattr(module,'Client',NativeClient)
    async def exercise():
        with pytest.raises(ValueError,match='operation no longer owned'):
            await module.bind_control_owner('socket',{'arm_id':'arm1','controller_session':'controller',
                'principal':'owner','operation_id':'owned'})
        assert closed
    asyncio.run(exercise())
