from types import SimpleNamespace

import pytest

from soarm_station.gripper_contact import GraspAttempt
from soarm_station.hardware import LeRobotArm


def test_real_register_and_jaw_only_overload_mask():
    calls = []
    def read(register, motor, *, normalize):
        calls.append((register, motor, normalize))
        return next(values)

    values = iter([0, 0x08, 0x20, 0x24])
    arm = LeRobotArm({})
    arm.gripper_name = 'custom_jaw'
    arm.bus = SimpleNamespace(read=read)
    assert [arm.gripper_overload() for _ in range(4)] == [False, False, True, True]
    assert calls == [('Status', 'custom_jaw', False)] * 4
    def offline(*args, **kwargs):raise ConnectionError('optional status read missed')
    arm.bus.read = offline
    assert arm.gripper_overload() is None


@pytest.mark.parametrize('read_flag,reason', [(lambda: True, 'SERVO_OVERLOAD'),
                                             (lambda: False, 'NO_JAW_PROGRESS'),
                                             (lambda: None, 'NO_JAW_PROGRESS')])
def test_blocked_jaw_freezes_once_and_keeps_only_existing_closing_bias(read_flag, reason):
    attempt = GraspAttempt(7)
    polls = []
    def read():
        polls.append(i)
        return read_flag()
    for i in range(50):
        held = attempt.update(i/100, 0, 17, 40, read)
        if held is not None:break
    assert held == 38 and attempt.reason == reason
    if reason == 'SERVO_OVERLOAD':
        assert i == 0
    else:
        assert i >= 30
    assert len(polls) <= 4
    # A trigger release in flight must not open the jaw before the UI sees the lock.
    assert attempt.update(1, 95, held, 41, lambda:pytest.fail('latched lock repolled')) == held


def test_motion_partial_goal_reversal_and_missing_samples_do_not_mimic_contact():
    attempt = GraspAttempt(1)
    for i in range(150):
        assert attempt.update(i/100, 10, 15, 60-i*.15, lambda:False) is None
    # The operator eases the trigger while there is still a large tracking error.
    for i in range(150, 201):
        assert attempt.update(i/100, 20, 15, 38, lambda:pytest.fail('opening repolled')) is None
    attempt = GraspAttempt(2)
    for i in range(100):
        assert attempt.update(i/100, 35, 35, 35.2, lambda:pytest.fail('reached target repolled')) is None
    for stamp in (2., 2.5, 3., 3.5):
        assert attempt.update(stamp, 0, 25, 35, lambda:False) is None
    assert attempt.reason == ''
    assert attempt.update(3.51, 0, .5, .7, lambda:False) is None
    assert attempt.update(3.52, 0, 0, .2, lambda:False) == 0
    assert attempt.reason == 'FULLY_CLOSED'
