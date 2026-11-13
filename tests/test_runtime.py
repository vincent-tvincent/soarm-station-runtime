import copy
import json
import time
from pathlib import Path
import pytest
from soarm.config import load
from soarm.control import ArmController,now
from soarm.worker import Runtime
from soarm.journal import Journal

ROOT=Path(__file__).resolve().parents[1]
@pytest.fixture
def arm(tmp_path):
    c=load(ROOT/'config/mock.yaml');c['lock_dir']=str(tmp_path/'locks')
    a=ArmController(c['arms'][0],c,'boot',{});a.q={j['name']:0. for j in a.config['joints']};a.gripper=50.;a.sample_time=now();a.backend.q=dict(a.q)
    return a
def grant(a,kind='DIRECT',mode='STREAM',**kw):
    a.set_permission(True,'operator',a.session)
    return a.begin({'principal':'operator','runtime_boot_id':'boot','controller_session':a.session,'operation_type':kind,'mode':mode,**kw})
def command(a,g,body,seq=1,kind='COMMAND',input_id='one',revision=0):
    return {'runtime_boot_id':g['runtime_boot_id'],'controller_session':g['controller_session'],
        'operation_id':g['operation_id'],'operation_epoch':g['operation_epoch'],'principal':'operator',
        'window_id':g['window_id'],'sequence':seq,'semantic_revision':revision,'frame_kind':kind,'input_id':input_id,'body':body}
def target(a,delta=.05):return {'positions':{n:v+delta for n,v in a.q.items()},'gripper':a.gripper,'gripper_unit':'percent'}
def test_monitoring_never_writes(tmp_path):
    c=load(ROOT/'config/multi_mock.yaml');c['lock_dir']=str(tmp_path/'locks');c['frame_dir']=str(tmp_path/'frames')
    runtime=Runtime(c);runtime.start();time.sleep(.2)
    assert all(a.backend.writes==0 for a in runtime.arms.values())
    assert all(not a.permission for a in runtime.arms.values())
    runtime.close();assert all(a.backend.writes==0 for a in runtime.arms.values())
def test_direct_latest_and_partial_rejected(arm):
    g=grant(arm);bad=target(arm);bad['positions'].pop(next(iter(bad['positions'])))
    with pytest.raises(ValueError):arm.input(command(arm,g,bad))
    arm.input(command(arm,g,target(arm)));arm._tick(now(),.01)
    assert arm.backend.writes==1
    assert max(arm.backend.q.values())<=.0201
def test_heartbeat_does_not_keep_old_pose(arm):
    g=grant(arm);arm.input(command(arm,g,target(arm)));arm.op['last_direct']=now()-.26
    arm.input(command(arm,g,{},seq=2,kind='HEARTBEAT'));arm._tick(now(),.01)
    assert arm.op is None and arm.error=='DIRECT_INPUT_EXPIRED'
def test_old_window_is_not_renewed_by_receipt(arm):
    g=grant(arm);arm.windows[g['window_id']]=now()-.001
    with pytest.raises(ValueError,match='Expired'):arm.input(command(arm,g,target(arm)))
def test_old_session_and_sequence_rejected(arm):
    g=grant(arm);f=command(arm,g,target(arm));arm.input(f)
    with pytest.raises(ValueError,match='sequence'):arm.input(f)
    arm._hold('cancel')
    with pytest.raises(ValueError,match='No active'):arm.input(command(arm,g,target(arm),2))
    assert not arm.permission
def test_pickup_and_gripper_clutch(arm):
    g=grant(arm)
    with pytest.raises(ValueError,match='pickup'):arm.input(command(arm,g,target(arm,.3)))
    t=target(arm);t['gripper']=99
    with pytest.raises(ValueError,match='Gripper pickup'):arm.input(command(arm,g,t))
def test_nan_and_unknown_units(arm):
    for v in (float('nan'),float('inf'),100):
        t=target(arm);t['positions'][next(iter(arm.q))]=v
        with pytest.raises(ValueError):arm._target(t)
    t=target(arm);t['gripper_unit']='radian'
    with pytest.raises(ValueError):arm._target(t)
def test_trajectory_timing_velocity(arm):
    with pytest.raises(ValueError):grant(arm,'TRAJECTORY','INLINE',points=[{**target(arm,1),'time_s':.001}])
    arm.permission=True
    with pytest.raises(ValueError):arm._trajectory([{**target(arm),'time_s':1},{**target(arm),'time_s':.5}])
def test_sparse_retry_cannot_reactivate_superseded_path(arm):
    g=grant(arm,'TRAJECTORY');one={'points':[{**target(arm),'time_s':1}]};two={'points':[{**target(arm,.1),'time_s':1}]}
    arm.input(command(arm,g,one,input_id='one',revision=1));arm.input(command(arm,g,two,seq=2,input_id='two',revision=2));generation=arm.generation
    with pytest.raises(ValueError,match='STALE_SEMANTIC_REVISION'):arm.input(command(arm,g,one,seq=3,input_id='one',revision=1))
    assert arm.last_input=='two' and arm.generation==generation
def test_independent_arms(arm,tmp_path):
    c=load(ROOT/'config/multi_mock.yaml');a2=ArmController(c['arms'][1],c,'boot',{})
    a2.q=dict(arm.q);a2.sample_time=now();a2.gripper=50
    g=grant(arm);g2=grant(a2);arm._hold('lost_source')
    assert a2.permission and a2.op
def test_unknown_resource(tmp_path):
    c=load(ROOT/'config/mock.yaml');r=Runtime(c)
    with pytest.raises(ValueError,match='Unknown arm'):r.call({'op':'permission','arm_id':'missing'})
def test_durable_exact_ack_and_reopen(tmp_path):
    path=tmp_path/'journal.db';j=Journal(path)
    sample={'device_id':'device','runtime_boot_id':'boot','arm_id':'arm','scheduled_ns':100,'valid':True}
    saved=j.append([sample],200);j.acknowledge([{'device_id':'wrong','runtime_boot_id':'boot','sequence':saved[0]['sequence']}])
    assert j.counts()['queued']==1
    j.close();j=Journal(path);assert j.meta('next_ns')==200 and len(j.pending())==1
    j.acknowledge(saved);assert j.counts()['remote_committed']==1;j.close()
def test_duplicate_device_config(tmp_path):
    import yaml
    c=load(ROOT/'config/mock.yaml');c['cameras']=[{'id':'one','device':'/dev/video0'},{'id':'two','device':'/dev/video0'}]
    path=tmp_path/'bad.yaml';path.write_text(yaml.safe_dump(c))
    with pytest.raises(ValueError,match='one capture owner'):load(path)


def test_inline_does_not_require_unconfigured_heartbeat(arm):
    grant(arm,'DIRECT','INLINE',duration_s=3,points=[{**target(arm),'time_s':2}])
    arm.op['last_live']=now()-1
    arm._tick(now(),.01)
    assert arm.op is not None and arm.backend.writes==1

def test_model_step_sequence_resets_generation(arm):
    arm.system['profiles']['fixture']={'backend':'fixture','arm_id':arm.id,'language_conditioned':True,'action_fps':30}
    grant(arm,'MODEL','INLINE',profile_id='fixture',duration_s=3,model_steps=[{'instruction':'one','duration_s':1},{'instruction':'two','duration_s':1}],instruction='one')
    generation=arm.generation;arm.op['step_started']=now()-1.1
    arm._tick(now(),.01)
    assert arm.op['instruction']=='two' and arm.generation==generation+1
