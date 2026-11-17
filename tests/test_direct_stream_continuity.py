"""Finite named targets, continuous same-owner sessions, and no old-sample replay."""
import copy
import math
import pytest
from test_continuity import setup,begin,frame,tick,point
from test_persistent_teleop import finite_loop


def test_absolute_angle_metadata_is_not_command_authority(setup):
    arm,clock=setup
    for joint in arm.config['joints']:
        joint.pop('min_rad',None);joint.pop('max_rad',None)
    begin(arm,persistent_target=True)
    desired={'positions':{name:4*math.pi for name in arm.q},'gripper':50.}
    arm.input(frame(arm,desired,identifier='beyond-former-yaml-range'))
    for _ in range(500):tick(arm,clock,.01,follow=False)
    assert arm.op and arm.permission and arm.error==''
    assert all(value>3.14 for value in arm.commanded_q.values())
    assert arm.target[0]==desired['positions']
    assert arm.stats['tracking_state']=='TRACKING_LAG' and not arm.stats['limited_actuators']


@pytest.mark.parametrize('bad',[float('nan'),float('inf'),True,'0.5',None])
def test_bad_sample_is_rejected_without_ending_or_changing_previous_intent(setup,bad):
    arm,clock=setup;begin(arm,persistent_target=True)
    desired={'positions':dict(arm.q),'gripper':50.}
    arm.input(frame(arm,desired,identifier='valid'))
    identity=(arm.op['operation_id'],arm.epoch,arm.last_sequence);target=copy.deepcopy(arm.target)
    broken=copy.deepcopy(desired);broken['positions'][next(iter(arm.q))]=bad
    with pytest.raises((ValueError,TypeError)):arm.input(frame(arm,broken,identifier='invalid'))
    assert arm.permission and (arm.op['operation_id'],arm.epoch,arm.last_sequence)==identity
    assert arm.target==target and arm.rejected_frames==1
    arm.input(frame(arm,desired,identifier='next-valid'))
    assert arm.last_input=='next-valid' and arm.last_input_error==''


@pytest.mark.parametrize('field,value',[('principal','other'),('controller_session','old'),('operation_id','old')])
def test_stale_identity_packet_does_not_cancel_current_owner(setup,field,value):
    arm,_=setup;begin(arm,persistent_target=True)
    valid=frame(arm,{'positions':dict(arm.q),'gripper':50.},identifier='valid')
    with pytest.raises(ValueError,match='identity mismatch'):arm.input({**valid,field:value})
    assert arm.permission and arm.op is not None and arm.last_sequence==-1
    arm.input(valid);assert arm.last_input=='valid'


@pytest.mark.parametrize('gap',[.3,1.,120.])
def test_live_sample_gap_pauses_without_replaying_old_target_and_fresh_sample_resumes(setup,gap):
    arm,clock=setup;arm.limits['max_stream_duration_s']=.1
    begin(arm);assert arm.op['deadline'] is None
    arm.input(frame(arm,{'positions':{n:.8 for n in arm.q},'gripper':80.},identifier='old'))
    tick(arm,clock,.01,follow=False)
    identity=(arm.session,arm.epoch,arm.op['operation_id']);reference=copy.deepcopy(arm.commanded_q);writes=arm.backend.writes
    tick(arm,clock,gap,follow=False)
    assert arm.phase=='PAUSED' and arm.op and arm.permission and arm.target is None
    assert arm.commanded_q==reference and arm.backend.writes==writes
    # Fresh heartbeats cannot revalidate an old sampled pose.
    arm.input(frame(arm,kind='HEARTBEAT'));tick(arm,clock,.01,follow=False)
    assert arm.backend.writes==writes and arm.feedback()['stream_ready']
    assert arm.feedback()['phase']=='PAUSED'
    arm.input(frame(arm,{'positions':{n:-.8 for n in arm.q},'gripper':30.},identifier='fresh'))
    tick(arm,clock,.01,follow=False)
    assert identity==(arm.session,arm.epoch,arm.op['operation_id']) and arm.phase=='ACTIVE'
    assert arm.backend.writes==writes+1 and arm.last_applied=='fresh'
    for joint in arm.config['joints']:
        name=joint['name'];assert 0<reference[name]-arm.commanded_q[name]<=joint['velocity_rad_s']*.01+1e-9


def test_live_sample_feedback_pause_drops_sample_until_new_command(setup):
    arm,clock=setup;begin(arm)
    arm.input(frame(arm,{'positions':{n:.8 for n in arm.q},'gripper':50.}))
    tick(arm,clock,.01,follow=False);writes=arm.backend.writes;operation=arm.op['operation_id']
    clock.advance(.2);arm._tick(clock(),.01)
    assert arm.phase=='PAUSED' and arm.target is None and arm.stats['pause_reason']=='FEEDBACK_STALE'
    tick(arm,clock,.01,follow=False)
    assert arm.backend.writes==writes and arm.op['operation_id']==operation and arm.permission
    arm.input(frame(arm,{'positions':dict(arm.q),'gripper':50.},identifier='fresh'))
    tick(arm,clock,.01);assert arm.backend.writes==writes+1 and arm.phase=='ACTIVE'


def test_real_control_gap_does_not_revoke_live_sample_owner_or_replay_old_sample(setup,tmp_path):
    arm,clock=setup;begin(arm)
    arm.input(frame(arm,{'positions':{n:.8 for n in arm.q},'gripper':50.}))
    finite_loop(arm,clock,tmp_path);arm.run()
    assert arm.op and arm.permission and arm.phase=='PAUSED' and arm.target is None
    assert arm.backend.writes==1 and arm.stats['pause_reason']=='AWAITING_FRESH_INPUT'


def test_tracking_timeouts_and_old_lead_settings_are_ignored_even_for_model_limiter(setup):
    arm,clock=setup;begin(arm)
    arm.op['operation_type']='MODEL'
    arm.config['max_target_lead_rad']=float('nan')
    for joint in arm.config['joints']:joint['tracking_timeout_s']=.001
    for _ in range(50):
        q,g=arm._limit({n:1. for n in arm.q},100.,clock(),.01)
        arm.commanded_q=q;arm.commanded_gripper=g;clock.advance(.1)
    assert arm.op and arm.permission and arm.commanded_gripper==100.
    assert all(value>=.75-1e-9 for value in arm.commanded_q.values())


def test_autonomous_liveness_and_finite_completion_deadlines_remain(setup):
    arm,clock=setup;begin(arm,'TRAJECTORY')
    tick(arm,clock,.7)
    assert arm.op is None and arm.error=='OPERATION_OR_LIVENESS_EXPIRED'
    arm._finish_hold();begin(arm,'TRAJECTORY','INLINE',points=[point(arm,.2,1.)])
    for _ in range(100):tick(arm,clock,.03,follow=False)
    tick(arm,clock,.1,follow=False)
    assert arm.op is None and arm.error=='TRACKING_TIMEOUT'
