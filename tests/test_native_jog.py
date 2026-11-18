"""Manual rate packets through the native owner, with a simulated clock and plant."""
from pathlib import Path

import pytest

from test_continuity import begin, frame, setup, tick

ROOT = Path(__file__).resolve().parents[1]


def jog_body(y=0., jaw=None):
    return {'jog': {'translation':[0.,y,0.], 'level':2, 'elevation':0., 'roll':0.,
                    'position_only':False, 'gripper_rate':0., 'gripper_target':jaw}}


def test_bursty_arrivals_integrate_once_per_control_tick_and_release_holds(setup):
    arm,clock=setup
    arm.config['urdf']=str(ROOT/'models/so101/so101.urdf')
    arm.prepare_jog()
    arm.q.update(shoulder_lift=-.87,elbow_flex=1.13)
    arm.backend.q=dict(arm.q)
    begin(arm,persistent_target=True)
    def y():return arm.jog_model.fk(arm.commanded_q,arm.commanded_gripper)['position_m'][1]
    previous=y()
    # The browser delivers held-input packets irregularly. The motor tick is local.
    for i in range(20):
        if i in (0,3,4,11,15,17):
            arm.input(frame(arm,jog_body(1.),identifier=str(i)))
        tick(arm,clock)
        current=y()
        assert current-previous==pytest.approx(.0004,abs=.000025)
        previous=current
    arm.input(frame(arm,jog_body(-1.)))
    tick(arm,clock);assert y()<previous-.00035
    frozen=dict(arm.commanded_q)
    arm.input(frame(arm,jog_body()));tick(arm,clock);assert arm.commanded_q==frozen

    arm.input(frame(arm,jog_body(1.,70.)));tick(arm,clock)
    assert arm.commanded_gripper>50.
    # Losing packets freezes at the admitted position without retiring the operation.
    tick(arm,clock, .3);held=dict(arm.commanded_q);jaw=arm.commanded_gripper
    tick(arm,clock)
    assert arm.commanded_q==held and arm.commanded_gripper==jaw and arm.permission
    assert arm.feedback()['jog_status']=='WAITING_FOR_INPUT'
    arm.input(frame(arm,{'positions':frozen,'gripper':jaw}));tick(arm,clock)
    assert 'jog' not in arm.op and 'jog_status' not in arm.feedback()


def test_outside_historical_angle_range_allows_jog_and_invalid_input_cannot_renew_a_rate(setup):
    arm,clock=setup
    arm.config['urdf']=str(ROOT/'models/so101/so101.urdf')
    arm.prepare_jog()
    arm.q.update(shoulder_lift=-1.87345,elbow_flex=1.67782,wrist_flex=1.31878)
    arm.backend.q=dict(arm.q);begin(arm,persistent_target=True)
    previous=dict(arm.commanded_q)
    arm.input(frame(arm,jog_body(1.)));tick(arm,clock)
    assert arm.commanded_q!=previous
    expiry=arm.op['jog_until']
    bad=frame(arm,jog_body(float('nan')))
    with pytest.raises(ValueError):arm.input(bad)
    assert arm.op['jog_until']==expiry
    arm._hold('test stop')
    with pytest.raises(ValueError):arm.input(bad)
    assert not arm.permission


def test_jog_rejected_on_external_live_sample_operation(setup):
    arm,clock=setup
    begin(arm)
    with pytest.raises(ValueError,match='owned DIRECT'):arm.input(frame(arm,jog_body(1.)))
    assert arm.target is None


@pytest.mark.parametrize('hardware_flag,reason', [(True,'SERVO_OVERLOAD'), (False,'NO_JAW_PROGRESS')])
def test_gamepad_contact_holds_jaw_through_release_while_body_keeps_jogging(setup,hardware_flag,reason):
    arm,clock=setup
    arm.config['urdf']=str(ROOT/'models/so101/so101.urdf')
    arm.q.update(shoulder_lift=-.87,elbow_flex=1.13)
    arm.backend.q=dict(arm.q)
    arm.prepare_jog()
    begin(arm,persistent_target=True)
    op_id=arm.op['operation_id']
    original=arm.backend.send
    arm.backend.send=lambda q,g:original(q,max(40.,g))  # Object at 40% opening.
    reads=[]
    def overload():
        reads.append(arm.gripper)
        return hardware_flag and arm.gripper<=40.
    arm.backend.gripper_overload=overload
    def run(attempt,jaw,count):
        for i in range(count):
            if i%5==0:
                body=jog_body(.2,jaw)
                if attempt:body['jog']['grasp_attempt']=attempt
                arm.input(frame(arm,body,identifier=f'fresh-{arm.last_sequence+1}'))
            tick(arm,clock)
    run(8,0.,60)
    fb=arm.feedback()
    assert fb['frozen_grasp_attempt']==8 and fb['grasp_freeze_reason']==reason
    held=fb['jog_gripper_percent']
    assert 38<=held<40 and reads
    joint_reference=dict(arm.commanded_q)
    run(8,95.,12)  # Release before the browser receives its first lock report.
    run(0,None,12)  # Browser has now entered its existing X/Square lock state.
    assert arm.commanded_gripper==held and arm.commanded_q!=joint_reference
    assert arm.permission and arm.op['operation_id']==op_id
    run(9,90.,12)  # Explicit unlock and the next trigger pickup.
    assert arm.commanded_gripper>held+5 and not arm.feedback().get('frozen_grasp_attempt')
    # Other producers using an absolute jaw goal do not opt into auto-lock.
    reads.clear()
    run(0,0.,65)
    assert not arm.feedback().get('frozen_grasp_attempt') and reads==[]
