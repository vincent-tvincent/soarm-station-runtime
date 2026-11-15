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
    def test_captured_positive_y_converges_from_admitted_reference_without_relaxation(self):
        calculator=model();start=np.array(calculator.fk(REFERENCE,GRIPPER)['position_m'])
        attempts=[];solve=calculator._solve
        def observe(*args,**kwargs):
            attempts.append(float((args[1][1]-start[1])/(.04/30)))
            return solve(*args,**kwargs)
        calculator._solve=observe
        result=jog(calculator)
        self.assertEqual(result['status'],'OK')
        self.assertAlmostEqual(result['step_fraction'],.125)
        self.assertLess(attempts[0],1.,'oversized branch-crossing step should be reduced before iterations')
        delta=np.array(result['geometry']['position_m'])-start
        self.assertGreater(delta[1],.00016)
        self.assertLess(np.linalg.norm(delta-np.array([0.,.04/30*.125,0.])),.000004)
        self.assertLessEqual(result['residuals']['orientation_rad'],.00001)
        self.assertEqual(result['target']['positions']['wrist_roll'],REFERENCE['wrist_roll'])
        self.assertEqual(result['target']['gripper'],GRIPPER)
        for joint in calculator.config['joints']:
            name=joint['name'];value=result['target']['positions'][name]
            self.assertLessEqual(abs(value-REFERENCE[name]),joint['velocity_rad_s']/30+1e-10)
            self.assertTrue(joint['min_rad']<=value<=joint['max_rad'])
        # The admitted/measured TCPs differ by centimeters; a successful test
        # cannot be achieved by quietly reseeding from the measured pose.
        self.assertGreater(result['geometry']['position_m'][2],.05)

    def test_feasible_negative_y_retains_full_step(self):
        calculator=model();start=np.array(calculator.fk(REFERENCE,GRIPPER)['position_m'])
        result=jog(calculator,'KeyL')
        self.assertEqual(result['status'],'OK');self.assertEqual(result['step_fraction'],1.)
        np.testing.assert_allclose(np.array(result['geometry']['position_m'])-start,[0.,-.04/30,0.],atol=.000004)

    def test_slow_or_jumping_clock_cannot_change_ik_acceptance(self):
        expected=jog(model())
        with patch('time.perf_counter',side_effect=itertools.count(0.,1000.)):
            actual=jog(model(legacy_budget=.000001))
        self.assertEqual(actual,expected)

    def test_extra_computation_latency_above_legacy_budget_preserves_solution(self):
        calculator=model();expected=jog(calculator);forward=calculator._forward
        def slow(*args):
            time.sleep(.002)
            return forward(*args)
        calculator._forward=slow
        started=time.monotonic();actual=jog(calculator)
        self.assertGreater(time.monotonic()-started,.04)
        self.assertEqual(actual,expected)

    def test_analog_positive_y_uses_the_same_corrected_solver(self):
        calculator=model();keyboard=jog(calculator)
        analog=calculator.analog_step(MEASURED,GRIPPER,translation=[0.,1.,0.],elevation=0.,roll=0.,
            speed_scale=1.,dt_s=1/30,level=2,reference_positions=REFERENCE,reference_gripper=GRIPPER)
        self.assertEqual(analog,keyboard)


if __name__=='__main__':unittest.main()
