"""Captured +Y stall: deterministic geometry checks, no devices or live state."""
import itertools
from pathlib import Path
import time
import unittest
from unittest.mock import patch

import numpy as np

from soarm_station.manual_control import ManualControl

NAMES=['shoulder_pan','shoulder_lift','elbow_flex','wrist_flex','wrist_roll']
# Read-only Pi snapshot 922495, baseline67e8fcf, November15. These are fixtures,
# not a command to restore the physical pose or a claim of current hardware state.
REFERENCE=dict(zip(NAMES,[-.06178812909299237,1.49576805973028,-1.2662957333374099,-.13204595344529957,-1.5182446548117707]))
MEASURED=dict(zip(NAMES,[-.05677114929563973,1.5205461878913236,-1.203701800606199,-.1373248070799934,-1.5228477209708766]))
GRIPPER=29.766803840877916


def model(legacy_budget=40.):
    cfg={'urdf':str(Path(__file__).resolve().parents[1]/'models/so101/so101.urdf'),
         'joints':[{'name':n,'min_rad':lo,'max_rad':hi,'velocity_rad_s':v} for n,lo,hi,v in zip(NAMES,
            [-1.91986,-1.74533,-1.69,-1.65806,-2.74385],
            [1.91986,1.74533,1.69,1.65806,2.84121],[1.5,1.5,1.5,2.,2.])],
         'gripper':{'name':'gripper','velocity_percent_s':100.},
         'manual_control':{'solve_budget_ms':legacy_budget}}
    return ManualControl(cfg)


def jog(calculator,key='KeyH'):
    return calculator.step(MEASURED,GRIPPER,keys=[key],dt_s=1/30,level=2,
                           reference_positions=REFERENCE,reference_gripper=GRIPPER)


class CapturedLateralRegressionTests(unittest.TestCase):
    def test_captured_reference_progress_with_frozen_wrist_and_finite_joint_velocity(self):
        calculator=model()
        start=np.array(calculator.fk(REFERENCE,GRIPPER)['position_m'])
        for key,sign in [('KeyH',1),('KeyL',-1)]:
            with self.subTest(key=key):
                result=jog(calculator,key)
                delta=np.array(result['geometry']['position_m'])-start
                self.assertGreater(delta[1]*sign,.001)
                self.assertLess(np.linalg.norm(delta[[0,2]]),.00015)
                self.assertLess(result['residuals']['orientation_rad'],.0005)
                self.assertEqual(result['target']['gripper'],GRIPPER)
                self.assertEqual(result['target']['positions']['wrist_roll'],REFERENCE['wrist_roll'])
                for joint in calculator.config['joints']:
                    name=joint['name']
                    self.assertLessEqual(abs(result['target']['positions'][name]-REFERENCE[name]),joint['velocity_rad_s']/30+1e-8)

    def test_outward_hold_tapers_without_elbow_chatter_and_reverses_immediately(self):
        from soarm_station.manual_control import keyboard_rates
        from soarm_station.jog_kinematics import JogAnchor
        m=model();q=dict(REFERENCE);anchor=JogAnchor();poses=[];elbows=[]
        def advance(key):
            return m._step_rates(q,GRIPPER,rates=keyboard_rates([key],level=2),dt_s=.01,
                                 position_only=False,anchor=anchor)['target']['positions']
        for _ in range(100):
            q=advance('KeyH')
            poses.append(m.fk(q,GRIPPER)['position_m'])
            elbows.append(q['elbow_flex'])
        d=np.diff(elbows)
        self.assertFalse(np.any(d[1:]*d[:-1] < -1e-12))
        start=np.array(m.fk(REFERENCE,GRIPPER)['position_m']);travel=np.asarray(poses)-start
        self.assertGreater(travel[-1,1],.003)
        self.assertLess(np.max(np.linalg.norm(travel[:,[0,2]],axis=1)),.0005)
        np.testing.assert_array_less(-1e-7,np.diff(travel[:,1]))
        back=advance('KeyL')
        self.assertLess(m.fk(back,GRIPPER)['position_m'][1],poses[-1][1]-.0002)
        self.assertAlmostEqual(m.fk(q,GRIPPER)['elevation_rad'],m.fk(REFERENCE,GRIPPER)['elevation_rad'],delta=.0002)

    def test_slow_or_jumping_clock_cannot_change_ik_acceptance(self):
        expected=jog(model())
        with patch('time.perf_counter',side_effect=itertools.count(0.,1000.)):
            actual=jog(model(legacy_budget=.000001))
        self.assertEqual(actual,expected)

    def test_extra_computation_latency_above_legacy_budget_preserves_solution(self):
        calculator=model();expected=jog(calculator);advance=calculator.jog_backend.advance
        def slow(*args):
            time.sleep(.05)
            return advance(*args)
        calculator.jog_backend.advance=slow
        started=time.monotonic();actual=jog(calculator)
        self.assertGreater(time.monotonic()-started,.04)
        self.assertEqual(actual,expected)

    def test_analog_positive_y_uses_the_same_corrected_solver(self):
        calculator=model();keyboard=jog(calculator)
        analog=calculator.analog_step(MEASURED,GRIPPER,translation=[0.,1.,0.],elevation=0.,roll=0.,
            speed_scale=1.,dt_s=1/30,level=2,reference_positions=REFERENCE,reference_gripper=GRIPPER)
        self.assertEqual(analog,keyboard)


if __name__=='__main__':unittest.main()
