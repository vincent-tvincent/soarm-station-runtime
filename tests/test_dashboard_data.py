import copy
import json
import math
from pathlib import Path
from soarm_station.config import load
from soarm_station.dashboard_data import health_rows,render_motors,resources,motor_snapshot,validate_motor_record
import pytest
from soarm_station.hardware import LeRobotArm,ReadOnlySO101


def config():
    c=load(Path(__file__).resolve().parents[1]/'config/mock.yaml')
    c['arms'][0]['label']='Left arm';c['arms'][0]['joints'][0]['motor_id']=42
    return c


def test_public_resources_are_named_and_exclude_private_configuration():
    c=config();c['profiles']={'selected':{'arm_id':'arm1','bundle':'/private/weights','token':'secret'},'other':{'arm_id':'another'}}
    c['arms'][0].update(urdf='/private/model.urdf',port='/dev/private')
    c['cameras'][0]['intrinsics']={'k':[0.]*9}
    original=copy.deepcopy(c);public=resources(c)
    assert public['arms'][0]['label']=='Left arm'
    assert public['arms'][0]['profile_ids']==['selected']
    assert public['arms'][0]['capabilities']['cartesian_endpoints']
    assert public['arms'][0]['tcp_frame']=='gripper_frame_link'
    assert public['arms'][0]['gripper']['unit']=='percent'
    assert '/private' not in json.dumps(public) and 'secret' not in json.dumps(public)
    assert c==original
    assert public['cameras'][0]['calibrated'] is False
    c['cameras'][0]['intrinsics']['k'][0]=100.
    assert resources(c)['cameras'][0]['calibrated'] is True


def test_missing_motor_data_is_null_and_measured_zero_is_preserved():
    c=config();empty=render_motors(c,{}, {},{},10**9)['arm1']
    assert empty['state']=='unavailable'
    assert all(m['position'] is None and m['torque_enabled'] is None for m in empty['motors'])
    assert empty['motors'][0]['id']==42 and empty['motors'][1]['id'] is None
    row={'name':'shoulder_pan','id':7,'torque_enabled':False,'temperature_c':0.,'voltage_v':0.,'goal_position':0.}
    records={'arm1':{'health_time_ns':10**9,'health_source':'measured','position_time_ns':10**9,
        'positions':{'shoulder_pan':0.},'motors':[row]}}
    actual=render_motors(c,records,{}, {},10**9+10**7)['arm1']['motors'][0]
    assert actual['id']==7 and actual['position']==0. and actual['goal_position']==0.
    assert actual['torque_enabled'] is False and actual['reference_position'] is None
    assert actual['source_state']=='measured'
    stale=render_motors(c,records,{}, {},10**9+3*10**9)['arm1']
    assert stale['state']=='stale' and stale['motors'][0]['position']==0.


def test_motor_positions_merge_newer_feedback_and_keep_goal_reference_distinct():
    c=config();name='shoulder_pan'
    record={'arm_id':'arm1','time_ns':10**9,'positions':{name:.1},'gripper':25.,
        'health_time_ns':10**9,'health':{'source':'simulated','motors':[{'name':name,'id':42,'goal_position':.3}]},
        'reference_positions':{name:.2},'reference_gripper':None}
    wire=motor_snapshot(record)
    joint={'header':{'stamp':{'sec':1,'nanosec':100}},'name':[name],'position':[.15]}
    result=render_motors(c,{'arm1':wire},{'arm1':joint},{},10**9+100)['arm1']
    row=result['motors'][0]
    assert result['state']=='simulated'
    assert (row['position'],row['goal_position'],row['reference_position'])==(.15,.3,.2)
    assert 'voltage_v' in row['unavailable_fields']
    record['positions'][name]=float('nan')
    assert motor_snapshot(record)['positions'][name] is None


def test_native_per_motor_health_reuses_exactly_the_existing_four_batched_reads():
    c={'joints':[{'name':'joint'}],'gripper':{'name':'gripper'}}
    values={'Torque_Enable':{'joint':1,'gripper':0},'Present_Voltage':{'joint':120,'gripper':119},
        'Present_Temperature':{'joint':31,'gripper':34},'Goal_Position':{'joint':90.,'gripper':12.}}
    class Bus:
        def __init__(self):self.reads=[]
        def sync_read(self,register,normalize=True):self.reads.append((register,normalize));return values[register]
    arm=LeRobotArm(c);arm.bus=Bus();arm.gripper_name='gripper';arm.calibration={'joint':{'id':17},'gripper':{'id':41}}
    health=arm.health()
    assert arm.bus.reads==[('Torque_Enable',False),('Present_Voltage',False),('Present_Temperature',False),('Goal_Position',True)]
    assert health['writes']==0 and health['torque']=='MIXED'
    a,b=health['motors'];assert (a['id'],b['id'])==(17,41)
    assert a['goal_position']==math.pi/2 and b['goal_position']==12.
    assert a['voltage_v']==12. and b['voltage_v']==11.9 and b['torque_enabled'] is False


def test_readonly_health_keeps_read_count_and_has_no_goal_register_reads():
    arm=ReadOnlySO101({'joints':[{'name':'joint'}],'gripper':{'name':'gripper'}})
    arm.calibration={'joint':{'id':3},'gripper':{'id':8}};reads=[]
    def read(motor,address,size):reads.append((motor,address,size));return {40:1,62:120,63:30}[address]
    arm._read=read
    health=arm.health()
    assert len(reads)==6 and {address for _,address,_ in reads}=={40,62,63}
    assert health['writes']==0 and all(m['goal_position'] is None for m in health['motors'])


def test_gripper_retains_its_own_sample_time_when_body_feedback_is_newer():
    c=config();record={'arm_id':'arm1','health_time_ns':3*10**9,'health_source':'measured',
        'position_time_ns':10**9,'positions':{'shoulder_pan':.1},'gripper':20.,
        'motors':[{'name':'shoulder_pan'},{'name':'gripper'}]}
    body={'header':{'stamp':{'sec':3,'nanosec':0}},'name':['shoulder_pan'],'position':[.2]}
    data=render_motors(c,{'arm1':record},{'arm1':body},{},3*10**9)['arm1']
    joint,gripper=data['motors'][0],data['motors'][-1]
    assert joint['position_time_ns']==3*10**9 and joint['position_age_ms']==0
    assert gripper['position_time_ns']==10**9 and gripper['position_age_ms']==2000
    assert gripper['source_state']=='stale' and data['state']=='stale'
    assert gripper['current_a'] is None and 'current_a' in gripper['unavailable_fields']


@pytest.mark.parametrize('change',[
    {'motors':{}},{'motors':[None]},{'positions':[]},{'positions':{'unknown':1.}},
    {'positions':{'shoulder_pan':'invalid'}},{'health_time_ns':'bad'},
    {'motors':[{'name':'shoulder_pan','id':True}]},
    {'motors':[{'name':'shoulder_pan','torque_enabled':'ON'}]},
    {'motors':[{'name':'shoulder_pan','temperature_c':float('nan')}]},
    {'motors':[{'name':'shoulder_pan'},{'name':'shoulder_pan'}]},
])
def test_malformed_motor_records_cannot_enter_the_cache(change):
    c=config();record={'arm_id':'arm1','health_time_ns':1,'position_time_ns':1,'health_source':'measured'}|change
    with pytest.raises(ValueError):validate_motor_record(record,c['arms'][0])


def test_only_real_current_values_are_projected_without_load_conversion():
    c=config();record={'arm_id':'arm1','health_time_ns':1,'position_time_ns':1,'health_source':'measured',
        'motors':[{'name':'shoulder_pan','current_a':.12,'load':900}]}
    checked=validate_motor_record(record,c['arms'][0])
    data=render_motors(c,{'arm1':checked},{},{},1)['arm1']
    assert data['motors'][0]['current_a']==.12 and data['motors'][1]['current_a'] is None
    assert 'load' not in json.dumps(checked)


@pytest.mark.parametrize('bad',[{'motor_id':True},{'motor_id':254},{'label':42}])
def test_bad_motor_metadata_rejects_at_configuration_load(tmp_path,bad):
    import yaml
    c=config();c['arms'][0]['joints'][0].update(bad)
    path=tmp_path/'config.yaml';path.write_text(yaml.safe_dump(c))
    with pytest.raises(ValueError):load(path)


def issue_snapshot(c,*,sample_ns=10_000_000_000,health_ns=9_900_000_000):
    arm=c['arms'][0];names=[j['name'] for j in arm['joints']]+['gripper']
    rows=health_rows(arm,{},dict.fromkeys(names,1),dict.fromkeys(names,12.),dict.fromkeys(names,30.),source='simulated')
    q={j['name']:0. for j in arm['joints']}
    return {'arm_id':'arm1','phase':'ACTIVE','error':'','age_ms':5.,'sequence':10,
        'time_ns':sample_ns,'health_time_ns':health_ns,'positions':q,'reference_positions':dict(q),
        'gripper':40.,'reference_gripper':40.,'health':{'source':'simulated','motors':rows},
        'stats':{'tracking_state':'FOLLOWING','limited_actuators':[]},'hold_context':{}}


def test_motor_issue_projection_survives_validated_wire_and_rendering_without_blanket_faults():
    c=config();snapshot=issue_snapshot(c);snapshot.update(phase='HOLD',error='TRACKING_STALLED: shoulder_pan')
    wire=motor_snapshot(snapshot,c['arms'][0]);checked=validate_motor_record(wire,c['arms'][0])
    result=render_motors(c,{'arm1':checked},{},{},10_050_000_000)['arm1']
    marked={m['name']:m['motion_issue'] for m in result['motors']}
    assert marked['shoulder_pan']['state']=='stall' and marked['shoulder_pan']['active'] is True
    assert not any(v['active'] for k,v in marked.items() if k!='shoulder_pan')
    recovered=validate_motor_record(motor_snapshot(issue_snapshot(c),c['arms'][0]),c['arms'][0])
    row=render_motors(c,{'arm1':recovered},{},{},10_050_000_000)['arm1']['motors'][0]
    assert row['motion_issue']['state']=='clear' and set(row['motion_issue']['clear_kinds'])=={'stall','fault'}


def test_new_joint_state_cannot_refresh_an_old_motor_issue_clear_record():
    c=config();wire=validate_motor_record(motor_snapshot(issue_snapshot(c),c['arms'][0]),c['arms'][0])
    joint={'header':{'stamp':{'sec':12,'nanosec':0}},'name':['shoulder_pan'],'position':[.2]}
    row=render_motors(c,{'arm1':wire},{'arm1':joint},{},12_000_000_000)['arm1']['motors'][0]
    assert row['position']==.2 and row['position_age_ms']==0
    assert row['motion_issue']['state']=='unknown' and row['motion_issue']['clear_kinds']==[]


@pytest.mark.parametrize('current_state,wall_ns,health_ns',[
    ({},10_200_000_000,7_600_000_000),
    ({'arm1':{'phase':'FAULT'}},10_050_000_000,9_900_000_000),
])
def test_stale_health_or_current_fault_suppresses_cached_clear_even_with_fresh_positions(current_state,wall_ns,health_ns):
    c=config();native=issue_snapshot(c,health_ns=health_ns)
    wire=validate_motor_record(motor_snapshot(native,c['arms'][0]),c['arms'][0])
    assert 'fault' in wire['motion_issues']['by_name']['shoulder_pan']['clear_kinds']
    row=render_motors(c,{'arm1':wire},{},current_state,wall_ns)['arm1']['motors'][0]
    assert row['position_age_ms']<=500 and row['source_state']=='stale'
    assert row['motion_issue']['clear_kinds']==[] and row['motion_issue']['state']=='unknown'


def test_invalid_motor_issue_projection_is_rejected_by_wire_validation():
    c=config();wire=motor_snapshot(issue_snapshot(c),c['arms'][0])
    wire['motion_issues']['by_name']['shoulder_pan']['feedback_fresh']=False
    with pytest.raises(ValueError,match='Stale motor issue cannot clear'):
        validate_motor_record(wire,c['arms'][0])
