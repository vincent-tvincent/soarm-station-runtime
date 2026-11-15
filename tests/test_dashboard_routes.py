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


def test_leader_cleanup_uses_ros_cache_and_precedes_policy_store_cleanup():
    from aiohttp.test_utils import TestClient,TestServer
    calls=[]
    async def unused(request):raise AssertionError('Route was not requested')
    def expanded_state():raise AssertionError('Leader cleanup must not query policy IPC')
    manager=types.SimpleNamespace(close=lambda:calls.append('policy'))
    owner=types.SimpleNamespace(config={'web':{},'arms':[]},public_resources={'arms':[]},
        states={'arm1':{'controller_session':'controller','operation_id':'','permission':False}},
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
            assert calls==[]
        assert calls==['leader','policy']
    asyncio.run(exercise())
