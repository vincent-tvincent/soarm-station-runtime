"""Connection-owned teleoperation: simulated clocks/plant only, no devices."""
import copy
import math

import pytest

from test_continuity import setup,begin,frame,tick


def test_persistent_teleop_has_no_inherited_input_liveness_total_or_tracking_expiry(setup):
    arm,clock=setup;arm.limits['max_stream_duration_s']=.5
    for joint in arm.config['joints']:joint['tracking_timeout_s']=.01
    arm.config.setdefault('gripper',{})['tracking_timeout_s']=.01
    begin(arm,persistent_target=True,require_liveness=True)
    assert arm.op['deadline'] is None and arm.op['requires_liveness'] is False
    target={'positions':{name:.5 for name in arm.q},'gripper':100.}
    old=frame(arm,target,identifier='one-intent');arm.input(old)
    identity=(arm.session,arm.op['operation_id'],arm.epoch)
    for _ in range(8):tick(arm,clock,60.,follow=False)
    assert (arm.session,arm.op['operation_id'],arm.epoch)==identity
    assert arm.permission and arm.phase=='ACTIVE' and not arm.hold_pending
    assert arm.stats['lagging_actuators'] and not arm.stats['limited_actuators'] and arm.backend.writes==8
    with pytest.raises(ValueError,match='Expired or unknown input window'):
        arm.input({**old,'sequence':2})
    assert arm.op['operation_id']==identity[1] and arm.permission
    arm.input(frame(arm,target,identifier='fresh-window'))
    assert arm.last_input=='fresh-window'


def test_stale_feedback_pauses_only_writes_and_resumes_latest_target_without_reference_jump(setup):
    arm,clock=setup;begin(arm,persistent_target=True)
    arm.input(frame(arm,{'positions':{name:.5 for name in arm.q},'gripper':55.}))
    tick(arm,clock,.01,follow=False)
    identity=(arm.session,arm.op['operation_id'],arm.epoch)
    previous=copy.deepcopy(arm.commanded_q);jaw=arm.commanded_gripper;writes=arm.backend.writes
    clock.advance(30.)
    arm._tick(clock(),.03)
    assert arm.backend.writes==writes and arm.commanded_q==previous and arm.commanded_gripper==jaw
    assert arm.permission and not arm.hold_pending and arm.stats['dispatch_paused']
    assert arm.stats['pause_reason']=='FEEDBACK_STALE'
    arm.input(frame(arm,{'positions':{name:-.2 for name in arm.q},'gripper':40.},identifier='latest'))
    arm._tick(clock(),.03);assert arm.backend.writes==writes
    tick(arm,clock,.01,follow=False)
    assert (arm.session,arm.op['operation_id'],arm.epoch)==identity and arm.permission
    assert not arm.stats['dispatch_paused'] and arm.stats['pause_reason']==''
    assert arm.backend.writes==writes+1 and arm.last_applied=='latest'
    for joint in arm.config['joints']:
        assert previous[joint['name']]-joint['velocity_rad_s']*.01-1e-9<=arm.commanded_q[joint['name']]<=previous[joint['name']]


@pytest.mark.parametrize('sign',[-1,1])
def test_lagging_joints_keep_advancing_and_reverse_by_slew_without_software_envelope(setup,sign):
    arm,clock=setup;begin(arm,persistent_target=True)
    lag='shoulder_lift';healthy='shoulder_pan'
    arm.commanded_q[lag]=sign*.2
    previous=dict(arm.commanded_q)
    arm.input(frame(arm,{'positions':{**arm.q,lag:sign*.8,healthy:.5},'gripper':50.}))
    for _ in range(100):
        previous=dict(arm.commanded_q)
        arm.q[healthy]=arm.backend.q[healthy]
        tick(arm,clock,.01,follow=False)
        assert sign*(arm.commanded_q[lag]-previous[lag])>=0
        assert abs(arm.commanded_q[lag]-previous[lag])<=.015+1e-9
        assert arm.op is not None and arm.permission
    assert arm.commanded_q[healthy]==pytest.approx(.5)
    assert arm.commanded_q[lag]==pytest.approx(sign*.8)
    assert arm.stats['tracking_state']=='TRACKING_LAG'
    assert arm.stats['lagging_actuators']==[lag] and not arm.stats['limited_actuators']
    reverse={**arm.q,lag:-sign*.2,healthy:.5}
    arm.input(frame(arm,{'positions':reverse,'gripper':50.},identifier='reverse'))
    speed=next(j['velocity_rad_s'] for j in arm.config['joints'] if j['name']==lag)
    recovery_ticks=math.ceil(.6/(speed*.01))+2
    for _ in range(recovery_ticks):
        before=arm.commanded_q[lag];tick(arm,clock,.01,follow=False)
        assert abs(arm.commanded_q[lag]-before)<=speed*.01+1e-9
        assert sign*(arm.commanded_q[lag]-before)<=0
        assert arm.commanded_q[healthy]==pytest.approx(.5)
    assert abs(arm.commanded_q[lag])<.2 and arm.op is not None
    assert arm.stats['tracking_state']=='TRACKING_LAG'


def test_persistent_admission_keeps_existing_valid_reference_instead_of_reanchoring(setup):
    arm,clock=setup;arm.commanded_q={**arm.q,'shoulder_lift':.2};arm.commanded_gripper=50.
    assert arm._reference()[0]['shoulder_lift']==.2
    begin(arm,persistent_target=True)
    assert arm.commanded_q['shoulder_lift']==.2 and arm.q['shoulder_lift']==0.


def test_explicit_duration_remains_intentional_but_default_profile_ceiling_is_not_a_teleop_deadline(setup):
    arm,clock=setup;arm.limits['max_stream_duration_s']=.1
    begin(arm,persistent_target=True,duration_s=2.)
    tick(arm,clock,1.)
    assert arm.op is not None
    tick(arm,clock,1.1)
    assert arm.op is None and not arm.permission


def finite_loop(arm,clock,tmp_path):
    arm.system={**arm.system,'lock_dir':str(tmp_path/'locks')}
    class Stop:
        count=0
        def is_set(self):return self.count>=2
        def wait(self,_):self.count+=1;clock.advance(.5)
        def set(self):self.count=2
    arm.stop=Stop()


def test_real_control_loop_gap_keeps_persistent_operation_and_caps_slew_dt(setup,tmp_path):
    arm,clock=setup;begin(arm,persistent_target=True)
    arm.input(frame(arm,{'positions':{name:.5 for name in arm.q},'gripper':55.}))
    finite_loop(arm,clock,tmp_path);sent=[];original=arm.backend.send
    def send(q,g):sent.append((dict(q),g));original(q,g)
    arm.backend.send=send
    arm.run()
    assert arm.op is not None and arm.permission and len(sent)==2
    for joint in arm.config['joints']:
        name=joint['name'];assert abs(sent[1][0][name]-sent[0][0][name])<=joint['velocity_rad_s']*.03+1e-9


def test_hardware_exception_still_faults_persistent_teleoperation(setup,tmp_path):
    arm,clock=setup;begin(arm,persistent_target=True)
    arm.input(frame(arm,{'positions':{name:.1 for name in arm.q},'gripper':50.}))
    finite_loop(arm,clock,tmp_path)
    def broken(*_):raise OSError('SIMULATED_IO_FAILURE')
    arm.backend.send=broken;arm.run()
    assert arm.phase=='FAULT' and arm.op is None and not arm.permission
    assert arm.error=='SIMULATED_IO_FAILURE'
    assert arm.snapshot()['hold_pending'] is True
