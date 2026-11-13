"""Manual calculation routes with local aiohttp clients; no devices or ROS."""
import asyncio
import copy
import math
from pathlib import Path
import threading

from aiohttp import web
from aiohttp.test_utils import TestClient,TestServer
import pytest

from soarm_station.manual_http import ManualRoutes


NAMES=('shoulder_pan','shoulder_lift','elbow_flex','wrist_flex','wrist_roll')
HOME=dict(zip(NAMES,map(math.radians,(0.,-50.,65.,0.,0.))))


def config():
    return {'arms':[{'id':'arm1','backend':'mock','base_frame':'arm1_base',
        'urdf':str(Path(__file__).resolve().parents[1]/'models/so101/so101.urdf'),
        'tcp_frame':'gripper_frame_link',
        'joints':[{'name':name,'min_rad':-2.8,'max_rad':2.8,'velocity_rad_s':2.} for name in NAMES],
        'gripper':{'name':'gripper','velocity_percent_s':100.},
        'manual_control':{'poses':{'home':{'positions':dict(HOME),'gripper':'preserve'}}}}]}


async def client_case(configuration,scenario,models=None):
    routes=ManualRoutes(configuration);app=web.Application();routes.mount(app)
    if models:routes.models.update(models)
    server=TestServer(app);await server.start_server()
    async with TestClient(server) as client:await scenario(client,routes)


def test_describe_and_fk_return_geometry_and_distinct_overlay_without_actuation_interfaces():
    configuration=config();before=copy.deepcopy(configuration)
    async def check(client,routes):
        async with client.get('/api/manual/arm1/describe') as response:
            assert response.status==200;description=await response.json()
        assert description['available'] and description['presets']=={'home':True,'fold':False}
        assert description['joint_names']==list(NAMES) and description['geometry_is_collision_check'] is False
        assert description['frame_id']=='arm1_base' and description['tcp_frame']=='gripper_frame_link'
        target={**HOME,'shoulder_pan':.1}
        body={'positions':HOME,'gripper':41.,'target':{'positions':target,'gripper':35.}}
        async with client.post('/api/manual/arm1/fk',json=body) as response:
            assert response.status==200;result=await response.json()
        assert result['measured']['gripper_percent']==41. and result['target']['gripper_percent']==35.
        assert result['measured']['position_m']!=result['target']['position_m']
        assert result['measured']['units']=={'position':'m','joint':'rad','gripper':'percent'}
        assert len([j for j in result['measured']['joints'] if j['role']=='joint'])==5
        assert all(len(segment['start_m'])==len(segment['end_m'])==3 for segment in result['measured']['segments'])
        assert 'permission' not in result and 'operation_id' not in result
        async with client.post('/api/manual/arm1/fk',json={'positions':HOME,'gripper':41.}) as response:
            assert (await response.json())['target'] is None
        assert routes.model('arm1') is routes.models['arm1']
    asyncio.run(client_case(configuration,check));assert configuration==before


def test_step_uses_admitted_body_and_gripper_reference_without_changing_measurements():
    configuration=config();reference={**HOME,'shoulder_pan':.01}
    body={'positions':dict(HOME),'gripper':70.,'reference_positions':reference,
          'reference_gripper':40.,'keys':['BracketRight'],'dt_s':.01,'level':1,'position_only':True}
    before=copy.deepcopy(body)
    async def check(client,routes):
        async with client.post('/api/manual/arm1/step',json=body) as response:
            assert response.status==200;result=await response.json()
        assert result['status']=='OK' and result['target']['positions']==reference
        assert result['target']['gripper']==pytest.approx(40.1)
        assert result['target']['gripper_unit']=='percent'
        assert result['geometry']['gripper_percent']==pytest.approx(40.1)
        assert result['dt_s']==.01 and result['level']==1
        no_keys={**body,'keys':[],'dt_s':.9}
        async with client.post('/api/manual/arm1/step',json=no_keys) as response:
            idle=await response.json();assert response.status==200
        assert idle['status']=='IDLE' and idle['dt_s']==.1
        assert idle['target']['positions']==reference and idle['target']['gripper']==40.
    asyncio.run(client_case(configuration,check));assert body==before


def test_home_and_roll_presets_return_named_targets_and_explicit_unavailable_errors():
    current={**HOME,'shoulder_pan':.2,'wrist_roll':.3}
    async def check(client,routes):
        async with client.post('/api/manual/arm1/preset',json={'name':'home','positions':current,'gripper':37.,'reference_gripper':23.,'preserve_wrist':True}) as response:
            assert response.status==200;result=await response.json()
        assert result['name']=='home' and result['target']['positions']=={**HOME,'wrist_roll':.3}
        assert result['target']['gripper']==23.
        async with client.post('/api/manual/arm1/roll',json={'name':'plus_90','positions':current,'gripper':37.,'reference_gripper':23.}) as response:
            assert response.status==200;rolled=await response.json()
        assert rolled['target']['positions']['wrist_roll']==pytest.approx(math.pi/2)
        assert rolled['target']['positions']['shoulder_pan']==current['shoulder_pan']
        assert rolled['target']['gripper']==23.
        for endpoint,payload in [('preset',{'name':'fold'}),('roll',{'name':'unknown'})]:
            async with client.post('/api/manual/arm1/'+endpoint,json={**payload,'positions':current,'gripper':37.}) as response:
                assert response.status==400 and (await response.json())['message']
    asyncio.run(client_case(config(),check))


def test_malformed_inputs_unknown_arms_and_missing_models_fail_as_data_not_server_errors():
    configuration=config();configuration['arms'].append({'id':'no_model','joints':copy.deepcopy(configuration['arms'][0]['joints'])})
    async def check(client,routes):
        for identifier in ('missing','no_model'):
            async with client.get('/api/manual/'+identifier+'/describe') as response:
                body=await response.json();assert response.status==200 and body['available'] is False and body['reason']
        for endpoint,payload in [('fk',None),('fk',{}),('fk',{'positions':HOME,'gripper':True}),
                                 ('step',{'positions':HOME,'gripper':50,'keys':'KeyK'}),
                                 ('step',{'positions':HOME,'gripper':50,'level':True}),
                                 ('preset',{'positions':HOME,'gripper':50,'name':'home','preserve_wrist':'yes'})]:
            async with client.post('/api/manual/arm1/'+endpoint,json=payload) as response:
                assert response.status==400 and (await response.json())['message']
        async with client.post('/api/manual/no_model/fk',json={'positions':HOME,'gripper':50}) as response:
            assert response.status==400
        async with client.get('/api/manual/arm1/fk') as response:assert response.status==405
    asyncio.run(client_case(configuration,check))


def test_busy_step_returns_409_without_blocking_describe_or_an_independent_arm():
    entered=threading.Event();release=threading.Event();calls=[]
    class Model:
        def __init__(self,slow=False):self.slow=slow
        def describe(self):return {'available':True,'presets':{}}
        def step(self,positions,gripper,**kwargs):
            calls.append((self.slow,copy.deepcopy(kwargs)))
            if self.slow:entered.set();release.wait(2)
            return {'status':'OK','target':{'positions':positions,'gripper':kwargs['reference_gripper']}}
    configuration=config();configuration['arms'].append({**copy.deepcopy(configuration['arms'][0]),'id':'arm2'})
    async def check(client,routes):
        payload={'positions':HOME,'gripper':70.,'reference_gripper':40.,'reference_positions':HOME,'keys':[]}
        async def post(arm):
            async with client.post('/api/manual/'+arm+'/step',json=payload) as response:return response.status,await response.json()
        first=asyncio.create_task(post('arm1'))
        try:
            assert await asyncio.to_thread(entered.wait,1)
            status,body=await post('arm1');assert status==409 and 'still running' in body['message']
            async with client.get('/api/manual/arm1/describe') as response:assert response.status==200
            status,body=await post('arm2');assert status==200 and body['target']['gripper']==40.
        finally:release.set()
        status,body=await first;assert status==200 and body['target']['gripper']==40.
        assert len(calls)==2
    asyncio.run(client_case(configuration,check,{'arm1':Model(True),'arm2':Model()}))
