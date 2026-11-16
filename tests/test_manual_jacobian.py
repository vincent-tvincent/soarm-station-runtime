"""Full geometric arm Jacobian rank; independent analytic chains, no devices."""
import copy
import json
import math
from pathlib import Path
import tempfile
import unittest

import numpy as np

from soarm_station.manual_control import ManualControl

ROOT=Path(__file__).resolve().parents[1]
NAMES=['shoulder_pan','shoulder_lift','elbow_flex','wrist_flex','wrist_roll']
HOME=dict(zip(NAMES,map(math.radians,[0.,-50.,65.,0.,0.])))
NEAR_EXTENSION=dict(zip(NAMES,[-.06178812909299237,1.49576805973028,-1.2662957333374099,-.13204595344529957,-1.5182446548117707]))


def config():
    return {'id':'arm','urdf':str(ROOT/'models/so101/so101.urdf'),
            'joints':[{'name':n,'min_rad':-3.14,'max_rad':3.14,'velocity_rad_s':2.} for n in NAMES],
            'gripper':{'name':'gripper'}}


class GeometricJacobianTests(unittest.TestCase):
    def planar(self, lengths=(1.,.5,.25)):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        path=Path(folder.name)/'planar.urdf'
        a,b,tip=lengths
        path.write_text(f'''<robot name="planar"><link name="base"/><link name="a"/><link name="b"/><link name="c"/><link name="tip"/>
        <joint name="q1" type="revolute"><parent link="base"/><child link="a"/><axis xyz="0 0 1"/></joint>
        <joint name="q2" type="revolute"><parent link="a"/><child link="b"/><origin xyz="{a} 0 0"/><axis xyz="0 0 1"/></joint>
        <joint name="q3" type="revolute"><parent link="b"/><child link="c"/><origin xyz="{b} 0 0"/><axis xyz="0 0 1"/></joint>
        <joint name="fixed_tip" type="fixed"><parent link="c"/><child link="tip"/><origin xyz="{tip} 0 0"/></joint></robot>''')
        return ManualControl({'urdf':str(path),'tcp_frame':'tip',
            'joints':[{'name':n,'min_rad':-math.pi,'max_rad':math.pi,'velocity_rad_s':2.} for n in ['q1','q2','q3']]})

    def test_bent_three_joint_planar_chain_matches_independent_geometric_matrix(self):
        result=self.planar().jacobian_diagnostics({'q1':0.,'q2':math.pi/2,'q3':0.})
        # Hand-derived velocities and angular axes at q=(0,pi/2,0).
        expected=np.array([[-.75,-.75,-.25],[1.,0.,0.],[0.,0.,0.],
                           [0.,0.,0.],[0.,0.,0.],[1.,1.,1.]])
        singular=np.linalg.svd(expected,compute_uv=False)
        self.assertEqual((result['rank'],result['max_rank']),(3,3))
        self.assertEqual((result['rows'],result['columns']),(6,3))
        np.testing.assert_allclose(result['singular_values'],singular,atol=1e-12)
        self.assertAlmostEqual(result['rank_tolerance'],np.finfo(float).eps*6*singular[0])
        self.assertEqual(result['matrix_kind'],'geometric')
        self.assertEqual(result['reference_frame'],'base')

    def test_exact_straight_rank_loss_and_near_singularity_are_distinct(self):
        model=self.planar()
        exact=model.jacobian_diagnostics({'q1':0.,'q2':0.,'q3':0.})
        self.assertEqual(exact['rank'],2);self.assertEqual(exact['max_rank'],3)
        near=model.jacobian_diagnostics({'q1':0.,'q2':1e-6,'q3':0.})
        self.assertEqual(near['rank'],3,'near-singular remains full rank by request')
        self.assertLess(near['singular_values'][-1],1e-6)
        self.assertGreater(near['singular_values'][-1],near['rank_tolerance'])
        json.dumps(exact,allow_nan=False);json.dumps(near,allow_nan=False)

    def test_zero_linear_jacobian_keeps_real_angular_twist_rank(self):
        result=self.planar((0.,0.,0.)).jacobian_diagnostics({'q1':.2,'q2':-.1,'q3':.4})
        self.assertEqual(result['rank'],1,'coincident parallel rotational axes still provide angular velocity')
        np.testing.assert_allclose(result['singular_values'],[math.sqrt(3),0.,0.],atol=1e-14)
        json.dumps(result,allow_nan=False)

    def test_all_five_so101_body_joints_participate_and_gripper_is_excluded(self):
        model=ManualControl(config());result=model.jacobian_diagnostics(HOME)
        self.assertEqual((result['rank'],result['max_rank'],result['rows'],result['columns']),(5,5,6,5))
        self.assertEqual(result['joint_names'],NAMES)
        self.assertNotIn('gripper',result['joint_names'])
        self.assertEqual(result['task_label'],'Arm Jacobian')
        self.assertNotIn('condition_number',result)
        self.assertNotIn('inverse_condition',result)
        self.assertNotIn('mode',result)

    def test_captured_extension_does_not_turn_conditioning_into_rank_deficiency(self):
        model=ManualControl(config());home=model.jacobian_diagnostics(HOME)
        extended=model.jacobian_diagnostics(NEAR_EXTENSION)
        self.assertEqual((home['rank'],extended['rank']),(5,5))
        self.assertLess(extended['singular_values'][-1],home['singular_values'][-1]/10)
        self.assertGreater(extended['singular_values'][-1],extended['rank_tolerance'])

    def test_conditioning_matches_independent_scaled_matrix(self):
        result=self.planar().jacobian_diagnostics({'q1':0.,'q2':math.pi/2,'q3':0.})['conditioning']
        jac=np.array([[-.75,-.75,-.25],[1.,0.,0.],[0.,0.,0.],
                      [0.,0.,0.],[0.,0.,0.],[1.,1.,1.]])
        jac[:3]/=1.75
        singular=np.linalg.svd(jac,compute_uv=False)
        self.assertTrue(result['available'])
        self.assertEqual(result['characteristic_length_m'],1.75)
        self.assertAlmostEqual(result['reciprocal_condition'],singular[-1]/singular[0])

    def test_conditioning_approaches_zero_before_rank_changes(self):
        model=self.planar();values=[]
        for angle in (1.,.1,.01,.001,1e-6):
            result=model.jacobian_diagnostics({'q1':0.,'q2':angle,'q3':0.})
            self.assertEqual(result['rank'],3)
            values.append(result['conditioning']['reciprocal_condition'])
        self.assertTrue(all(a>b>0 for a,b in zip(values,values[1:])))
        exact=model.jacobian_diagnostics({'q1':0.,'q2':0.,'q3':0.})
        self.assertEqual(exact['conditioning']['reciprocal_condition'],0.)

    def test_conditioning_is_invariant_under_uniform_model_scaling(self):
        pose={'q1':.4,'q2':.7,'q3':-.2}
        unit=self.planar().jacobian_diagnostics(pose)['conditioning']
        large=self.planar((1000.,500.,250.)).jacobian_diagnostics(pose)['conditioning']
        self.assertAlmostEqual(unit['reciprocal_condition'],large['reciprocal_condition'],places=12)
        self.assertAlmostEqual(large['characteristic_length_m'],1000*unit['characteristic_length_m'])

    def test_zero_length_model_does_not_invent_a_conditioning_scale(self):
        metric=self.planar((0.,0.,0.)).jacobian_diagnostics({'q1':0.,'q2':0.,'q3':0.})['conditioning']
        self.assertFalse(metric['available']);self.assertIsNone(metric['reciprocal_condition'])

    def test_diagnostics_do_not_mutate_fk_or_ik(self):
        model=ManualControl(config());positions=copy.deepcopy(HOME)
        geometry=model.fk(positions,37.)
        target=model.step(positions,37.,keys=['KeyJ'],dt_s=.03,level=2)
        model.jacobian_diagnostics(positions)
        self.assertEqual(model.fk(positions,37.),geometry)
        self.assertNotIn('jacobian',geometry,'plain FK API remains unchanged')
        self.assertEqual(model.step(positions,37.,keys=['KeyJ'],dt_s=.03,level=2),target)
        self.assertEqual(positions,HOME)

    def test_partial_or_nonfinite_pose_rejects(self):
        model=ManualControl(config())
        with self.assertRaises(ValueError):model.jacobian_diagnostics({'shoulder_pan':0.})
        with self.assertRaises(ValueError):model.jacobian_diagnostics({**HOME,'wrist_roll':float('nan')})


try:
    from soarm_station.manual_http import ManualRoutes
except ImportError:
    ManualRoutes=None


class Request:
    method='POST'
    match_info={'arm':'arm','operation':'fk'}
    def __init__(self,body):self.body=body
    async def json(self):return self.body


@unittest.skipIf(ManualRoutes is None,'aiohttp unavailable')
class JacobianHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_fk_attaches_geometric_diagnostics_only_to_measured_geometry(self):
        routes=ManualRoutes({'arms':[config()]})
        body={'positions':dict(HOME),'gripper':50.,'target':{'positions':dict(HOME),'gripper':35.}}
        response=await routes.handle(Request(body));self.assertEqual(response.status,200)
        value=json.loads(response.text)
        self.assertEqual(value['measured']['jacobian']['matrix_kind'],'geometric')
        self.assertEqual(value['measured']['jacobian']['rows'],6)
        metric=value['measured']['jacobian']['conditioning']
        self.assertTrue(metric['available']);self.assertGreater(metric['reciprocal_condition'],0.)
        self.assertLessEqual(metric['reciprocal_condition'],1.)
        self.assertEqual(value['measured']['gripper_percent'],50.)
        self.assertEqual(value['target']['gripper_percent'],35.)
        self.assertNotIn('jacobian',value['target'])
        self.assertNotIn('mobility',value['measured'])
        self.assertNotIn('permission',value)

    async def test_fk_has_no_mode_selection_and_bad_pose_still_rejects(self):
        routes=ManualRoutes({'arms':[config()]})
        body={'positions':HOME,'gripper':50.,'mobility_mode':'position'}
        response=await routes.handle(Request(body))
        result=json.loads(response.text)['measured']['jacobian']
        self.assertEqual((result['rows'],result['columns'],result['rank']),(6,5,5))
        response=await routes.handle(Request({'positions':{},'gripper':50.}))
        self.assertEqual(response.status,400)


if __name__=='__main__':unittest.main()
