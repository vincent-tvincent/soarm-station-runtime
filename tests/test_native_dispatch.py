import math
import threading
from soarm.hardware import LeRobotArm
from soarm.config import load
from soarm.control import ArmController
from soarm.worker import Runtime
from pathlib import Path
import pytest

class Bus:
    def __init__(self):
        self.events=[];self.torque={'joint':0,'gripper':0};self.raw={'joint':2000,'gripper':1555}
    def sync_read(self,register,normalize=True):
        self.events.append(('read',register,normalize))
        return dict(self.torque) if register=='Torque_Enable' else dict(self.raw)
    def sync_write(self,register,values,normalize=True):
        self.events.append(('write',register,dict(values),normalize))
    def enable_torque(self):
        self.events.append(('enable',));self.torque={n:1 for n in self.torque}

def native():
    a=LeRobotArm({});a.bus=Bus();a.gripper_name='gripper';return a

def test_first_dispatch_stages_current_raw_pose_then_enables():
    a=native();a.send({'joint':.1},20)
    events=a.bus.events
    stage=next(i for i,e in enumerate(events) if e[0]=='write')
    enable=next(i for i,e in enumerate(events) if e[0]=='enable')
    assert events[stage]==('write','Goal_Position',{'joint':2000,'gripper':1555},False)
    assert stage<enable<len(events)-1
    assert math.isclose(events[-1][2]['joint'],math.degrees(.1))

def test_hold_uses_fresh_torque_and_does_not_arm_off_motors():
    a=native();a.hold();assert not any(e[0] in ('write','enable') for e in a.bus.events)
    a.bus.torque={'joint':1,'gripper':0};a.bus.events.clear();a.hold()
    assert a.bus.events[-1]==('write','Goal_Position',{'joint':2000},False)
    assert not any(e[0]=='enable' for e in a.bus.events)

def test_torque_loss_does_not_rearm():
    a=native();a.dispatch_ready=True
    with pytest.raises(RuntimeError,match='Torque was lost'):a.send({'joint':0.},50)
    assert not any(e[0] in ('write','enable') for e in a.bus.events)

def test_controller_does_not_use_cached_torque_for_hold():
    root=Path(__file__).resolve().parents[1];c=load(root/'config/mock.yaml')
    a=ArmController(c['arms'][0],c,'boot',{})
    a.backend.torque=True;a.last_write=1.;a.health_data={'torque':'OFF'}
    a._finish_hold()
    assert a.backend.writes==1 and a.health_data['torque']=='ON' and a.hold_event.is_set()

def test_failed_hold_cannot_report_a_completed_operation():
    root=Path(__file__).resolve().parents[1];c=load(root/'config/mock.yaml');r=Runtime(c);a=r.arms['arm1']
    a.completed['operation']={'execution_completed':True,'result_code':'COMPLETED'}
    a.pending_result_id='operation';a._fault('hold failed')
    result=r.call({'op':'result','arm_id':'arm1','operation_id':'operation'})
    assert not result['execution_completed'] and result['result_code']=='FAULT'

def test_later_fault_does_not_rewrite_a_confirmed_historical_result():
    root=Path(__file__).resolve().parents[1];c=load(root/'config/mock.yaml');r=Runtime(c);a=r.arms['arm1']
    a.completed['old']={'execution_completed':True,'result_code':'COMPLETED'};a.result_ready.add('old')
    a._fault('unrelated later failure')
    result=r.call({'op':'result','arm_id':'arm1','operation_id':'old'})
    assert result['execution_completed'] and result['result_code']=='COMPLETED'

def test_reconnect_has_no_goals_and_requires_new_session(tmp_path):
    root=Path(__file__).resolve().parents[1];c=load(root/'config/mock.yaml');c['lock_dir']=str(tmp_path/'locks')
    r=Runtime(c);a=r.arms['arm1'];session=a.session;a.phase='FAULT';a.last_write=123.;a.hold_pending=True
    class Once:
        calls=0
        def wait(self,_):self.calls+=1;return self.calls>1
    r.recovery_stop=Once();r.recover()
    try:
        import time
        time.sleep(.03)
        assert a.session!=session and not a.permission and a.last_write==0. and a.backend.writes==0
    finally:a.close()

def test_command_advances_while_measured_plant_lags_then_respects_lead_cap():
    root=Path(__file__).resolve().parents[1];c=load(root/'config/mock.yaml');a=ArmController(c['arms'][0],c,'boot',{})
    from soarm.control import now
    a.q={j['name']:0. for j in a.config['joints']};a.gripper=50.;a.sample_time=now()
    a.set_permission(True,'operator',a.session)
    grant=a.begin({'principal':'operator','runtime_boot_id':'boot','controller_session':a.session,'operation_type':'DIRECT','mode':'STREAM'})
    a.input({'principal':'operator','runtime_boot_id':'boot','controller_session':a.session,'operation_id':grant['operation_id'],
             'operation_epoch':grant['operation_epoch'],'window_id':grant['window_id'],'sequence':1,'frame_kind':'COMMAND','input_id':'one',
             'body':{'positions':{n:.15 for n in a.q},'gripper':50.}})
    a._tick(now(),.01);first=a.commanded_q['elbow_flex']
    a._tick(now(),.01);second=a.commanded_q['elbow_flex']
    assert second>first and second-first<=.01500001
    for _ in range(20):a._tick(now(),.01)
    assert a.commanded_q['elbow_flex']==pytest.approx(.12)

def test_completed_path_preserves_final_servo_goal():
    root=Path(__file__).resolve().parents[1];c=load(root/'config/mock.yaml');a=ArmController(c['arms'][0],c,'boot',{})
    a.backend.torque=True;a.backend.writes=7;a.last_write=1.
    a._hold('PATH_COMPLETE',completed=True);a._finish_hold()
    assert a.backend.writes==7 and a.hold_event.is_set()

def test_joint_specific_completion_tolerance():
    root=Path(__file__).resolve().parents[1];c=load(root/'config/mock.yaml');a=ArmController(c['arms'][0],c,'boot',{})
    from soarm.control import now
    a.q={j['name']:0. for j in a.config['joints']};a.gripper=50.;a.sample_time=now()
    next(j for j in a.config['joints'] if j['name']=='elbow_flex')['goal_tolerance_rad']=.07
    a.set_permission(True,'operator',a.session)
    q=dict(a.q);q['elbow_flex']=.06
    a.begin({'principal':'operator','runtime_boot_id':'boot','controller_session':a.session,'operation_type':'TRAJECTORY','mode':'INLINE',
             'points':[{'positions':q,'gripper':50.,'time_s':.1}]})
    a.op['start']=now()-.2;a._tick(now(),.01)
    assert a.op is not None # The final goal itself must be fully dispatched first.
    for _ in range(4):a._tick(now(),.01)
    assert a.op is None and a.error=='PATH_COMPLETE'
