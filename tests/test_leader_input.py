"""No hardware/ROS/network: injected OS inventory, bus and websocket contracts."""
import asyncio
import copy
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from soarm_station.leader_input import ReadOnlyLeader, InputHostBridge, discover, load_config, select_gripper_command,bounded_leader_target,sample_until_control_ends
from soarm_station.leader_gateway import LeaderHostRegistry,native_start_state


@pytest.fixture
def host_config(tmp_path):
    calibration={name:{'id':i,'drive_mode':0,'homing_offset':0,'range_min':0,'range_max':4095}
                 for i,name in enumerate(['leader_pan','leader_lift','jaw'],1)}
    path=tmp_path/'calibration.json';path.write_text(json.dumps(calibration))
    profile={'id':'leader','label':'Test configured leader','match':{'serial':'UNIT-TEST-SERIAL'},
             'calibration':str(path),'joint_map':{'leader_pan':'pan','leader_lift':'lift'},'gripper_source':'jaw'}
    return {'host_id':'test-host','label':'Test host','leaders':[profile],'lock_dir':str(tmp_path/'locks')}


def ports():
    return [{'port':'/dev/unit-test-not-opened','serial':'UNIT-TEST-SERIAL','vid':1,'pid':2,'description':'Unit-test serial'},
            {'port':'/dev/unconfigured-not-opened','serial':'OTHER','vid':3,'pid':4,'description':'Unconfigured serial'}]


def test_discovery_only_advertises_actual_enumeration_and_configuration(host_config):
    rows=discover(host_config,ports)
    assert len(rows)==2 and rows[0]['configured'] and not rows[1]['configured']
    assert rows[0]['id']=='profile-leader' and rows[0]['joint_names']==['pan','lift']
    assert discover(host_config,lambda:[])==[]
    duplicate=copy.deepcopy(host_config);duplicate['leaders'].append(copy.deepcopy(duplicate['leaders'][0]))
    assert not discover(duplicate,ports)[0]['configured']


def test_missing_calibration_is_unconfigured_not_autocalibrated(host_config):
    host_config['leaders'][0]['calibration']='/nonexistent/unit-test-calibration.json'
    assert not discover(host_config,ports)[0]['configured']


class FakeBus:
    def __init__(self,port,calibration,profile):
        self.expected=calibration;self.events=[]
    def connect(self):self.events.append('connect')
    def read_calibration(self):self.events.append('read_calibration');return copy.deepcopy(self.expected)
    def sync_read(self,register):self.events.append(('read',register));return {'leader_pan':90.,'leader_lift':-30.,'jaw':50.}
    def disconnect(self,disable_torque=True):self.events.append(('disconnect',disable_torque))
    def write(self,*args,**kwargs):raise AssertionError('A motor write must never be called')
    def sync_write(self,*args,**kwargs):raise AssertionError('A motor write must never be called')
    def write_calibration(self,*args,**kwargs):raise AssertionError('Recalibration must never be called')
    def enable_torque(self,*args,**kwargs):raise AssertionError('Arming must never be called')
    def disable_torque(self,*args,**kwargs):raise AssertionError('Torque changes must never be called')


def test_reader_has_no_configure_calibrate_or_torque_writes(host_config):
    reader=ReadOnlyLeader(host_config['leaders'][0],ports()[0]['port'],FakeBus,host_config['lock_dir'])
    reader.connect();bus=reader.bus
    sample=reader.sample()
    assert sample['body']['positions']['pan']==pytest.approx(1.5707963267948966)
    assert sample['body']['gripper']==50 and sample['body']['gripper_unit']=='percent'
    assert sample['started_ns']<=sample['completed_ns']
    with pytest.raises(PermissionError):bus.write_calibration({})
    reader.close()
    assert bus.events==['connect','read_calibration',('read','Present_Position'),('disconnect',False)]


def test_reader_rejects_mismatched_calibration_without_writes(host_config):
    class Mismatched(FakeBus):
        def read_calibration(self):
            value=super().read_calibration();value['leader_pan']['homing_offset']=1;return value
    reader=ReadOnlyLeader(host_config['leaders'][0],ports()[0]['port'],Mismatched,host_config['lock_dir'])
    with pytest.raises(ValueError,match='Calibration mismatch'):reader.connect()
    assert reader.bus is None and reader.lock_file is None


class Request:
    def __init__(self,body,host='test-host'):self.body=body;self.match_info={'host_id':host}
    async def json(self):return self.body


class Socket:
    closed=False
    def __init__(self):self.sent=[]
    async def send_json(self,body):self.sent.append(body)


def registry_fixture(host_config):
    state={'arms':{'follower':{'permission':True,'feedback_valid':True,'runtime_boot_id':'boot',
                             'controller_session':'controller','operation_id':''}},
           'resources':{'arms':[{'id':'follower','joints':[{'name':'pan','min':-2.,'max':2.},{'name':'lift','min':-2.,'max':2.}]}]}}
    held=[]
    async def hold(arm,session,operator,operation):held.append((arm,session,operator,operation));return {'success':True}
    registry=LeaderHostRegistry(lambda:state,hold)
    registry.hosts['test-host']={'id':'test-host','label':'Test host','devices':discover(host_config,ports),
        'ws':Socket(),'state':'IDLE','active':None,'last_seen':time.monotonic()}
    body={'device_id':'profile-leader','arm_id':'follower','operator_session_id':'operator',
          'runtime_boot_id':'boot','controller_session':'controller','hz':100}
    return registry,state,held,body


def test_start_requires_explicit_permission_and_resource_mapping(host_config):
    async def check():
        registry,state,held,body=registry_fixture(host_config)
        state['arms']['follower']['permission']=False
        with pytest.raises(ValueError,match='permission'):await registry.start_route(Request(body))
        state['arms']['follower']['permission']=True
        response=await registry.start_route(Request(body));result=json.loads(response.text)
        assert result['accepted'] and result['state']=='STARTING' and not held
        command=registry.hosts['test-host']['ws'].sent[0]
        assert command['type']=='start' and command['joint_names']==['pan','lift']
        assert command['joint_limits']=={name:{'min_rad':-2.,'max_rad':2.} for name in ('pan','lift')}
        assert not any('permission' in message.get('type','') for message in registry.hosts['test-host']['ws'].sent)
        assert registry.snapshot()['hosts'][0]['latest_pose'] is None
    asyncio.run(check())


def test_start_uses_current_permission_even_before_ros_cache_publication(host_config):
    async def check():
        registry,cached,held,body=registry_fixture(host_config)
        current=copy.deepcopy(cached)
        cached['arms']['follower'].update(permission=False,feedback_valid=False)
        reads=[]
        async def read_current():
            await asyncio.sleep(0)
            reads.append('snapshot')
            return current
        registry.start_state_provider=read_current
        reply=json.loads((await registry.start_route(Request(body))).text)
        assert reply['accepted'] and reads==['snapshot']
        assert not cached['arms']['follower']['permission']
        assert len(registry.hosts['test-host']['ws'].sent)==1 and held==[]
    asyncio.run(check())


@pytest.mark.parametrize('field,reason',[('permission','permission'),('feedback_valid','feedback')])
def test_start_rejects_current_denial_even_when_ros_cache_says_ready(host_config,field,reason):
    async def check():
        registry,cached,held,body=registry_fixture(host_config)
        current=copy.deepcopy(cached);current['arms']['follower'][field]=False
        registry.start_state_provider=lambda:current
        with pytest.raises(ValueError,match=reason):await registry.start_route(Request(body))
        assert registry.hosts['test-host']['active'] is None
        assert registry.hosts['test-host']['ws'].sent==[] and held==[]
    asyncio.run(check())


def test_native_read_does_not_allow_two_competing_starts(host_config):
    async def check():
        registry,state,held,body=registry_fixture(host_config)
        barrier=asyncio.Event()
        async def read_current():await barrier.wait();return state
        registry.start_state_provider=read_current
        calls=[asyncio.create_task(registry.start_route(Request(body))) for _ in range(2)]
        await asyncio.sleep(0);barrier.set()
        replies=await asyncio.gather(*calls,return_exceptions=True)
        assert sum(isinstance(reply,ValueError) for reply in replies)==1
        assert sum(not isinstance(reply,Exception) for reply in replies)==1
        assert len(registry.hosts['test-host']['ws'].sent)==1 and held==[]
    asyncio.run(check())


@pytest.mark.parametrize('change',['connection','device'])
def test_start_rechecks_input_host_after_native_read(host_config,change):
    async def check():
        registry,state,held,body=registry_fixture(host_config)
        host=registry.hosts['test-host'];connection=host['ws']
        async def read_current():
            await asyncio.sleep(0)
            if change=='connection':host['ws']=Socket()
            else:host['devices']=[]
            return state
        registry.start_state_provider=read_current
        with pytest.raises(ValueError):await registry.start_route(Request(body))
        assert host['active'] is None and connection.sent==[] and held==[]
    asyncio.run(check())


def test_failed_native_read_does_not_fall_back_to_ready_ros_cache(host_config):
    async def check():
        registry,state,held,body=registry_fixture(host_config)
        async def unavailable():raise OSError('Native worker unavailable')
        registry.start_state_provider=unavailable
        with pytest.raises(OSError):await registry.start_route(Request(body))
        assert registry.hosts['test-host']['active'] is None
        assert registry.hosts['test-host']['ws'].sent==[] and held==[]
    asyncio.run(check())


@pytest.mark.parametrize('age,elapsed,positions,valid',[
    (5.,2.,{'pan':0.},True),(149.,0.,{'pan':0.},True),
    (149.,2.,{'pan':0.},False),(150.,0.,{'pan':0.},False),
    (1.,0.,{},False),(None,0.,{'pan':0.},False),
    (float('nan'),0.,{'pan':0.},False),(False,0.,{'pan':0.},False),
])
def test_native_admission_preserves_feedback_freshness_including_transport_age(age,elapsed,positions,valid):
    source={'arms':{'arm':{'permission':True,'age_ms':age,'positions':positions,'controller_session':'session'}}}
    result=native_start_state(source,{'arms':[{'id':'arm'}]},elapsed)
    assert result['arms']['arm']['feedback_valid'] is valid
    assert result['arms']['arm']['permission'] is True
    assert 'feedback_valid' not in source['arms']['arm']


def test_stop_and_heartbeat_cannot_take_over_unrelated_sessions(host_config):
    async def check():
        registry,state,held,body=registry_fixture(host_config);host=registry.hosts['test-host']
        assert not (await registry.stop_host(host,'missing','operator'))['accepted'] and not held
        result=json.loads((await registry.start_route(Request(body))).text);run_id=result['run_id']
        with pytest.raises(ValueError,match='session'):await registry.stop_host(host,run_id,'different-operator')
        denied=json.loads((await registry.heartbeat_route(Request({'run_id':run_id,'operator_session_id':'other'}))).text)
        assert not denied['accepted'] and host['active']['run_id']==run_id
        active=host['active'];active['operation_id']='owned-operation';state['arms']['follower']['operation_id']='unrelated-operation'
        assert not (await registry._hold_current(active))['success'] and not held
        state['arms']['follower']['operation_id']='owned-operation'
        assert (await registry._hold_current(active))['success'] and held==[('follower','controller','operator','owned-operation')]
        active['last_browser']=time.monotonic()-3600
        reply=json.loads((await registry.heartbeat_route(Request({'run_id':run_id,'operator_session_id':'operator'}))).text)
        assert reply['accepted'] and registry.snapshot()['browser_lease_ms'] is None
        host['active']=None;host['reason']='Actual native terminal reason'
        late=json.loads((await registry.heartbeat_route(Request({'run_id':run_id,'operator_session_id':'operator'}))).text)
        assert not late['accepted'] and late['reason']=='Actual native terminal reason'
    asyncio.run(check())


def test_bad_browser_shapes_raise_validation_errors(host_config):
    async def check():
        registry,state,held,body=registry_fixture(host_config)
        with pytest.raises(ValueError):await registry.start_route(Request([]))
        with pytest.raises(ValueError):await registry.start_route(Request({**body,'arm_id':[]}))
        with pytest.raises(ValueError):await registry.heartbeat_route(Request(None))
        with pytest.raises(ValueError):await registry.stop_route(Request('all'))
        assert not held
    asyncio.run(check())


@pytest.mark.parametrize('delivery_delay',[0.,.07])
def test_native_bridge_samples_only_after_grant_and_never_enables_permission(host_config,delivery_delay):
    async def check():
        command_sent=asyncio.Event();log=[]
        class Control:
            closed=False
            def __init__(self):self.queue=asyncio.Queue();self.sent=[]
            async def __aenter__(self):return self
            async def __aexit__(self,*args):self.closed=True
            def __aiter__(self):return self
            async def __anext__(self):return await self.queue.get()
            async def send_json(self,value):
                self.sent.append(value)
                if value.get('operation_type'):
                    grant={'stream_ready':True,'operation_id':'op','window_id':'original-pi-window','window_valid_for_ms':250}
                    log.append('grant_sent')
                    for data in [{'type':'grant','grant':grant},{'type':'state','state':{'phase':'ACTIVE','operation_id':'op'}}]:
                        self.queue.put_nowait(SimpleNamespace(type=SimpleNamespace(value=1),data=json.dumps(data)))
                elif value.get('frame_kind')=='COMMAND':command_sent.set()
        control=Control()
        class Session:
            def ws_connect(self,url,**kwargs):assert url.endswith('/ws/control');return control
            def post(self,*args,**kwargs):raise AssertionError('Bridge must never enable permission')
        class Sampler:
            def __init__(self,*args):log.append('sampler_created')
            async def start(self):log.append('reader_started')
            async def sample(self):
                assert 'grant_sent' in log;log.append('sample')
                t=time.monotonic_ns()
                await asyncio.sleep(delivery_delay)
                return {'body':{'positions':{'pan':.1,'lift':-.1},'gripper':50.,'gripper_unit':'percent'},'started_ns':t,'completed_ns':t}
            async def close(self):log.append('reader_closed')
        bridge=InputHostBridge(host_config,'https://gateway.invalid',None,sampler_factory=Sampler,enumerate_ports=ports)
        bridge.session=Session();bridge.registry=Socket();bridge.advertisement();assert not log
        request={'run_id':'run','device_id':'profile-leader','arm_id':'follower','operator_session_id':'operator',
                 'runtime_boot_id':'boot','controller_session':'session','joint_names':['pan','lift'],'hz':100,
                 'joint_limits':{name:{'min_rad':-2.,'max_rad':2.} for name in ('pan','lift')}}
        bridge.active=request;bridge.task=asyncio.create_task(bridge.operate(request))
        await asyncio.wait_for(command_sent.wait(),1)
        await bridge.stop('run')
        commands=[m for m in control.sent if m.get('frame_kind')=='COMMAND']
        assert commands and commands[0]['window_id']=='original-pi-window'
        assert 'reader_closed' in log and bridge.active is None
        assert bridge.headers=={} # trusted-network bridge does not demand a token
    asyncio.run(check())


def test_gripper_reference_never_defaults_or_loses_valid_zero():
    assert select_gripper_command(80,False,31.7,5,{'gripper_percent':44})==31.7
    assert select_gripper_command(80,False,None,0,{'gripper_percent':44})==0
    assert select_gripper_command(80,False,None,float('nan'),{'gripper_percent':44})==44
    assert select_gripper_command(80,True,31.7,5,{'gripper_percent':44})==80
    with pytest.raises(ValueError,match='unavailable'):
        select_gripper_command(80,False,None,None,{'gripper_percent':None})


def test_follow_setting_is_scoped_and_changes_no_permission_or_liveness(host_config):
    async def check():
        registry,state,held,body=registry_fixture(host_config)
        response=await registry.start_route(Request({**body,'follow_gripper':False,'gripper_reference':37.5}))
        run=json.loads(response.text)['run_id'];host=registry.hosts['test-host'];active=host['active'];initial_live=active['last_browser']
        assert not active['follow_gripper'] and active['gripper_reference']==37.5
        assert host['ws'].sent[0]['follow_gripper'] is False
        settings={'run_id':run,'operator_session_id':'operator','follow_gripper':True}
        for invalid in [{**settings,'run_id':'old'},{**settings,'operator_session_id':'other'},{**settings,'follow_gripper':'true'}]:
            with pytest.raises(ValueError):await registry.settings_route(Request(invalid))
        assert len(host['ws'].sent)==1
        result=json.loads((await registry.settings_route(Request(settings))).text)
        assert result['accepted'] and active['follow_gripper'] is True
        assert host['ws'].sent[-1]=={'type':'settings',**settings}
        assert active['last_browser']==initial_live and not held and state['arms']['follower']['permission']
        assert registry.snapshot()['hosts'][0]['active']['follow_gripper'] is True
        host['state']='STOPPING'
        with pytest.raises(ValueError):await registry.settings_route(Request({**settings,'follow_gripper':False}))
    asyncio.run(check())


def test_bridge_toggle_keeps_body_live_and_holds_last_sent_jaw(host_config):
    async def check():
        done=asyncio.Event();samples=[];bridge=None
        class Control:
            closed=False
            def __init__(self):self.queue=asyncio.Queue();self.commands=[]
            async def __aenter__(self):return self
            async def __aexit__(self,*args):self.closed=True
            def __aiter__(self):return self
            async def __anext__(self):return await self.queue.get()
            async def send_json(self,value):
                if value.get('operation_type'):
                    grant={'stream_ready':True,'operation_id':'op','window_id':'pi-window','window_valid_for_ms':250}
                    for payload in [{'type':'grant','grant':grant},{'type':'state','state':{'phase':'ACTIVE','operation_id':'op','gripper_percent':61.2}}]:
                        self.queue.put_nowait(SimpleNamespace(type=SimpleNamespace(value=1),data=json.dumps(payload)))
                elif value.get('frame_kind')=='COMMAND':
                    self.commands.append(copy.deepcopy(value))
                    if len(self.commands)==1:
                        assert bridge.apply_settings({'run_id':'run','operator_session_id':'operator','follow_gripper':True})
                    elif len(self.commands)==2:
                        assert bridge.apply_settings({'run_id':'run','operator_session_id':'operator','follow_gripper':False})
                    elif len(self.commands)==3:done.set()
        control=Control()
        class Session:
            def ws_connect(self,url,**kwargs):return control
        class Sampler:
            def __init__(self,*args):pass
            async def start(self):pass
            async def sample(self):
                index=len(samples);body={'positions':{'pan':.1+index*.01,'lift':-.1},'gripper':[80.,70.,60.,50.][min(index,3)],'gripper_unit':'percent'}
                samples.append(copy.deepcopy(body));stamp=time.monotonic_ns()
                return {'body':body,'started_ns':stamp,'completed_ns':stamp}
            async def close(self):pass
        bridge=InputHostBridge(host_config,'https://gateway.invalid',None,sampler_factory=Sampler,enumerate_ports=ports)
        bridge.session=Session();bridge.registry=Socket()
        request={'run_id':'run','device_id':'profile-leader','arm_id':'follower','operator_session_id':'operator',
                 'runtime_boot_id':'boot','controller_session':'session','joint_names':['pan','lift'],'hz':100,
                 'joint_limits':{name:{'min_rad':-2.,'max_rad':2.} for name in ('pan','lift')},
                 'follow_gripper':False,'gripper_reference':37.5}
        bridge.active=request
        assert not bridge.apply_settings({'run_id':'old','operator_session_id':'operator','follow_gripper':True})
        assert not bridge.apply_settings({'run_id':'run','operator_session_id':'other','follow_gripper':True})
        bridge.task=asyncio.create_task(bridge.operate(request))
        await asyncio.wait_for(done.wait(),1);await bridge.stop('run')
        outgoing=[m['body'] for m in control.commands[:3]]
        assert [b['gripper'] for b in outgoing]==[37.5,70.,70.]
        assert outgoing[0]['positions']['pan']<outgoing[1]['positions']['pan']<outgoing[2]['positions']['pan']
        assert [s['gripper'] for s in samples[:3]]==[80.,70.,60.] # actual overlay samples were not rewritten
    asyncio.run(check())


def test_leader_targets_clip_existing_follower_limits_without_mutating_observation():
    measured={'positions':{'pan':3.,'lift':-.3},'gripper':-2.,'gripper_unit':'percent'}
    limits={'pan':{'min_rad':-1.,'max_rad':1.},'lift':{'min_rad':-.5,'max_rad':.5}}
    result=bounded_leader_target(measured,limits)
    assert result=={'positions':{'pan':1.,'lift':-.3},'gripper':0.,'gripper_unit':'percent'}
    assert measured['positions']['pan']==3. and measured['gripper']==-2.
    with pytest.raises(ValueError):bounded_leader_target(measured,None)
    with pytest.raises(ValueError):bounded_leader_target({**measured,'positions':{'pan':float('nan'),'lift':0.}},limits)


def test_control_end_cancels_a_pending_leader_read_and_preserves_actual_reason():
    async def check():
        started=asyncio.Event();cancelled=[]
        class Sampler:
            async def sample(self):
                started.set()
                try:await asyncio.Event().wait()
                finally:cancelled.append(True)
        async def source_end():
            await started.wait();raise RuntimeError('Actual native terminal reason')
        receiver=asyncio.create_task(source_end())
        with pytest.raises(RuntimeError,match='Actual native terminal reason'):
            await sample_until_control_ends(Sampler(),receiver)
        assert cancelled==[True]
    asyncio.run(check())


def test_stop_cancels_the_reader_without_canceling_unrelated_receiver_ownership():
    async def check():
        started=asyncio.Event();cancelled=[]
        class Sampler:
            async def sample(self):
                started.set()
                try:await asyncio.Event().wait()
                finally:cancelled.append(True)
        receiver=asyncio.create_task(asyncio.Event().wait())
        run=asyncio.create_task(sample_until_control_ends(Sampler(),receiver))
        await started.wait();run.cancel()
        with pytest.raises(asyncio.CancelledError):await run
        assert cancelled==[True] and not receiver.done()
        receiver.cancel()
        try:await receiver
        except asyncio.CancelledError:pass
    asyncio.run(check())
