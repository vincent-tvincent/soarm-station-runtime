"""Offline geometry/key semantics. No ROS, LeRobot, device or service imports."""
import copy
import math
from pathlib import Path
import tempfile
import unittest

import numpy as np

from soarm_station.manual_control import ManualControl, ManualUnavailable, keyboard_rates

ROOT = Path(__file__).resolve().parents[1]
NAMES = ['shoulder_pan', 'shoulder_lift', 'elbow_flex', 'wrist_flex', 'wrist_roll']
HOME = dict(zip(NAMES, map(math.radians, [0., -50., 65., 0., 0.])))


def config():
    return {'id': 'test_arm', 'base_frame': 'test_arm_base',
            'urdf': str(ROOT/'models/so101/so101.urdf'), 'tcp_frame': 'gripper_frame_link',
            'joints': [{'name': n, 'min_rad': lo, 'max_rad': hi, 'velocity_rad_s': 2.}
                       for n, lo, hi in zip(NAMES, [-1.91986,-1.74533,-1.69,-1.65806,-2.74385], [1.91986,1.74533,1.69,1.65806,2.84121])],
            'gripper': {'name':'gripper','velocity_percent_s':100.},
            'manual_control': {'poses': {'home': {'positions':dict(HOME), 'gripper':'preserve'}}}}


class ManualControlTests(unittest.TestCase):
    def test_exact_keys_levels_and_momentary_shift(self):
        for negative,positive,axis in [('KeyJ','KeyK',0),('KeyL','KeyH',1),('KeyF','KeyD',2)]:
            for key,sign in [(negative,-1),(positive,1)]:
                rates=keyboard_rates([key],level=2)
                self.assertAlmostEqual(rates['translation_m_s'][axis],sign*.04)
                self.assertEqual(sum(v!=0 for v in rates['translation_m_s']),1)
        self.assertLess(keyboard_rates(['KeyI'])['elevation_rad_s'],0)
        self.assertGreater(keyboard_rates(['KeyU'])['elevation_rad_s'],0)
        self.assertLess(keyboard_rates(['KeyG'])['roll_rad_s'],0)
        self.assertGreater(keyboard_rates(['KeyS'])['roll_rad_s'],0)
        self.assertLess(keyboard_rates(['['])['gripper_percent_s'],0)
        self.assertGreater(keyboard_rates([']'])['gripper_percent_s'],0)
        diagonal=keyboard_rates(['KeyK','KeyH','KeyD'],level=3)
        self.assertAlmostEqual(np.linalg.norm(diagonal['translation_m_s']),.08)
        self.assertEqual(keyboard_rates(['KeyK'],level=1,shift=True)['level'],5)
        self.assertEqual(keyboard_rates(['KeyK'],level=1)['level'],1)
        self.assertEqual(keyboard_rates(['KeyJ','KeyK'])['translation_m_s'],[0.,0.,0.])
        self.assertEqual(keyboard_rates(['KeyU','KeyS'],position_only=True)['roll_rad_s'],0.)
        with self.assertRaises(ValueError):keyboard_rates([],level=True)

    def test_urdf_fk_against_independent_planar_chain_and_fixed_tcp(self):
        xml='''<robot name="two"><link name="base"/><link name="a"/><link name="b"/><link name="tip"/>
        <joint name="q1" type="revolute"><parent link="base"/><child link="a"/><axis xyz="0 0 1"/></joint>
        <joint name="q2" type="revolute"><parent link="a"/><child link="b"/><origin xyz="1 0 0"/><axis xyz="0 0 1"/></joint>
        <joint name="tip_fixed" type="fixed"><parent link="b"/><child link="tip"/><origin xyz=".5 0 0" rpy="0 0 .2"/></joint></robot>'''
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'model.urdf';path.write_text(xml)
            c={'urdf':str(path),'tcp_frame':'tip','joints':[{'name':n,'min_rad':-3.,'max_rad':3.,'velocity_rad_s':2.} for n in ['motor_a','motor_b']],
               'urdf_joint_names':['q1','q2']}
            model=ManualControl(c)
            for first,second in [(.3,-.2),(-.8,.7),(1.,.5)]:
                result=model.fk({'motor_b':second,'motor_a':first},40.)
                expected=[math.cos(first)+.5*math.cos(first+second),math.sin(first)+.5*math.sin(first+second),0.]
                np.testing.assert_allclose(result['position_m'],expected,atol=1e-12)
                np.testing.assert_allclose(result['rotation_matrix'][0],[math.cos(first+second+.2),-math.sin(first+second+.2),0.],atol=1e-12)
            self.assertEqual(len(result['joints']),2)
            self.assertEqual(result['segments'][-1]['to'],'tcp')

    def test_so101_geometry_has_five_body_axes_separate_jaw_and_true_tcp(self):
        model=ManualControl(config());result=model.fk(HOME,32.)
        self.assertEqual([j['name'] for j in result['joints'] if j['role']=='joint'],NAMES)
        self.assertEqual(result['joints'][-1]['role'],'gripper')
        self.assertIsNone(result['joints'][-1]['position_rad'])
        self.assertEqual(result['gripper_percent'],32.)
        self.assertEqual(result['frame_id'],'test_arm_base')
        np.testing.assert_allclose(result['position_m'],[.28440490636805316,-.000009637206194513344,.1316734300711337],atol=1e-10)
        self.assertGreater(np.linalg.norm(np.array(result['tcp']['position_m'])-np.array(result['joints'][4]['position_m'])),.09)
        self.assertEqual(model.fk(HOME,0.)['position_m'],model.fk(HOME,100.)['position_m'])

    def test_each_translation_direction_progresses_without_wrist_roll_or_mutation(self):
        model=ManualControl(config());original=copy.deepcopy(HOME);start=np.array(model.fk(HOME,50.)['position_m'])
        for key,axis,sign in [('KeyJ',0,-1),('KeyK',0,1),('KeyL',1,-1),('KeyH',1,1),('KeyF',2,-1),('KeyD',2,1)]:
            with self.subTest(key=key):
                result=model.step(HOME,50.,keys=[key],dt_s=.03,level=2)
                self.assertEqual(result['status'],'OK')
                delta=np.array(result['geometry']['position_m'])-start
                self.assertGreater(delta[axis]*sign,.0008)
                self.assertEqual(result['target']['positions']['wrist_roll'],HOME['wrist_roll'])
                self.assertLess(result['residuals']['position_m'],.00015)
                for n in NAMES:self.assertLessEqual(abs(result['target']['positions'][n]-HOME[n]),.060000001)
        self.assertEqual(HOME,original)

    def test_gripper_fingers_follow_opening_without_changing_body_fk(self):
        model=ManualControl(config())
        poses=[model.fk(HOME,opening) for opening in (0.,50.,100.)]
        moving=[]
        for opening,pose in zip((0.,50.,100.),poses):
            fixed=next(s for s in pose['segments'] if s.get('role')=='fixed_finger')
            finger=next(s for s in pose['segments'] if s.get('role')=='moving_finger')
            hinge=next(j for j in pose['joints'] if j['role']=='gripper')
            np.testing.assert_allclose(fixed['end_m'],pose['tcp']['position_m'])
            np.testing.assert_allclose(finger['start_m'],hinge['position_m'])
            self.assertAlmostEqual(np.linalg.norm(np.array(finger['end_m'])-finger['start_m']),.065)
            self.assertEqual(pose['gripper_visual']['opening_percent'],opening)
            self.assertTrue(pose['gripper_visual']['schematic'])
            np.testing.assert_allclose(pose['position_m'],poses[0]['position_m'])
            np.testing.assert_allclose(pose['rotation_matrix'],poses[0]['rotation_matrix'])
            self.assertEqual(pose['joints'],poses[0]['joints'])
            moving.append(np.array(finger['end_m'])-finger['start_m'])
        self.assertAlmostEqual(poses[0]['gripper_visual']['visual_angle_rad'],-.174533)
        self.assertAlmostEqual(poses[2]['gripper_visual']['visual_angle_rad'],1.74533)
        # Independent rotation invariant: equal percentage steps rotate equally,
        # with all three tip vectors lying in the real hinge's rotation plane.
        axis=np.array(poses[0]['joints'][-1]['axis_base'])
        for vector in moving:self.assertAlmostEqual(float(vector@axis),0.,places=10)
        expected_cos=math.cos((1.74533+.174533)/2)
        for a,b in zip(moving,moving[1:]):self.assertAlmostEqual(float(a@b)/.065**2,expected_cos)

    def test_elevation_roll_and_position_only_have_distinct_meaning(self):
        model=ManualControl(config());start=model.fk(HOME,50.)
        for key,field,sign in [('KeyI','elevation_rad',-1),('KeyU','elevation_rad',1),('KeyG','tool_roll_rad',-1),('KeyS','tool_roll_rad',1)]:
            with self.subTest(key=key):
                result=model.step(HOME,50.,keys=[key],dt_s=.03,level=2)
                self.assertEqual(result['status'],'OK')
                self.assertGreater((result['geometry'][field]-start[field])*sign,.008)
                self.assertLess(np.linalg.norm(np.array(result['geometry']['position_m'])-start['position_m']),.00015)
        ignored=model.step(HOME,50.,keys=['KeyU','KeyS'],dt_s=.03,position_only=True)
        self.assertEqual(ignored['target']['positions'],HOME)
        self.assertEqual(ignored['status'],'IDLE')

    def test_reference_integration_gripper_sign_bounds_and_no_catchup(self):
        model=ManualControl(config())
        left=model.step(HOME,50.,keys=['['],dt_s=.03,level=2)
        right=model.step(HOME,50.,keys=[']'],dt_s=.03,level=2)
        self.assertAlmostEqual(left['target']['gripper'],49.1)
        self.assertAlmostEqual(right['target']['gripper'],50.9)
        self.assertEqual(model.step(HOME,99.,keys=[']'],dt_s=10,shift=True)['target']['gripper'],100.)
        self.assertEqual(model.step(HOME,1.,keys=['['],dt_s=10,shift=True)['dt_s'],.1)
        first=model.step(HOME,50.,keys=['KeyK'],dt_s=.03,level=2)
        second=model.step(HOME,50.,keys=['KeyK'],dt_s=.03,level=2,reference_positions=first['target']['positions'])
        self.assertGreater(second['geometry']['position_m'][0],first['geometry']['position_m'][0]+.0008)

    def test_blocked_joints_do_not_report_best_effort_as_success(self):
        c=config()
        for j in c['joints']:j['velocity_rad_s']=1e-8
        model=ManualControl(c)
        result=model.step(HOME,50.,keys=['KeyK'],dt_s=.03,level=5)
        self.assertEqual(result['status'],'BLOCKED')
        self.assertEqual(result['step_fraction'],0.)
        self.assertEqual(result['target']['positions'],HOME)
        self.assertIn('No converged',result['reason'])
        partial=model.step(HOME,50.,keys=['KeyK','['],dt_s=.03,level=5)
        self.assertEqual(partial['status'],'PARTIAL')
        self.assertLess(partial['target']['gripper'],50.)

    def test_home_fold_and_reference_roll_are_configured_absolute_targets(self):
        c=config();model=ManualControl(c);current={**HOME,'wrist_roll':.3}
        self.assertEqual(model.preset('home',current,37.)['target']['gripper'],37.)
        self.assertEqual(model.preset('home',current,37.,preserve_wrist=True)['target']['positions']['wrist_roll'],.3)
        self.assertFalse(model.describe()['poses']['fold']['available'])
        with self.assertRaises(ManualUnavailable):model.preset('fold',current,37.)
        minus=model.roll_preset('minus_90',current,37.)['target']
        self.assertAlmostEqual(minus['positions']['wrist_roll'],-math.pi/2)
        self.assertEqual(minus,model.roll_preset('minus_90',minus['positions'],37.)['target'])
        for n in NAMES[:-1]:self.assertEqual(minus['positions'][n],current[n])
        c['manual_control']['poses']['fold']={'positions':{**HOME,'shoulder_lift':math.radians(-104.79)}}
        invalid=ManualControl(c)
        self.assertFalse(invalid.describe()['poses']['fold']['available'])
        with self.assertRaises(ValueError):invalid.preset('fold',current,37.)

    def test_missing_model_bad_units_and_partial_joint_input_are_explicit(self):
        c=config();c.pop('urdf');model=ManualControl(c)
        self.assertFalse(model.describe()['available'])
        with self.assertRaises(ManualUnavailable):model.fk(HOME,50.)
        model=ManualControl(config())
        with self.assertRaises(ValueError):model.fk({NAMES[0]:0.},50.)
        with self.assertRaises(ValueError):model.fk({**HOME,'wrist_roll':float('nan')},50.)
        with self.assertRaises(ValueError):model.step(HOME,None,keys=['KeyK'],dt_s=.03)
        with self.assertRaises(ValueError):model.step(HOME,50.,keys=['KeyK'],dt_s=-1.)
        # Real observations outside the command domain remain displayable, not clipped.
        self.assertTrue(np.isfinite(model.fk({**HOME,'shoulder_pan':2.},50.)['position_m']).all())
        with self.assertRaises(ValueError):model.step({**HOME,'shoulder_pan':2.},50.,keys=['KeyK'],dt_s=.03)


if __name__=='__main__':unittest.main()
