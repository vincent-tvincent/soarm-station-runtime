"""Analog rates and read-only HTTP integration; no devices or native runtimes."""
import asyncio
import copy
import json
import math
from pathlib import Path
import threading
import unittest

import numpy as np

from soarm_station.manual_control import ManualControl, keyboard_rates

ROOT = Path(__file__).resolve().parents[1]
NAMES = ['shoulder_pan','shoulder_lift','elbow_flex','wrist_flex','wrist_roll']
HOME = dict(zip(NAMES,map(math.radians,[0,-50,65,0,0])))


def config(identifier='arm'):
    return {'id':identifier,'urdf':str(ROOT/'models/so101/so101.urdf'),
            'joints':[{'name':name,'min_rad':lower,'max_rad':upper,'velocity_rad_s':2.}
                for name,lower,upper in zip(NAMES,[-1.91986,-1.74533,-1.69,-1.65806,-2.74385],
                                            [1.91986,1.74533,1.69,1.65806,2.84121])],
            'gripper':{'name':'gripper','velocity_percent_s':100.},
            'manual_control':{'poses':{'home':{'positions':dict(HOME),'gripper':'preserve'}}}}


def body(**overrides):
    return {'positions':dict(HOME),'gripper':50.,'translation':[0.,0.,0.],
            'elevation':0.,'roll':0.,'speed_scale':1.,'gripper_target':None,
            'dt_s':.05,'level':2,**overrides}


class AnalogStepTests(unittest.TestCase):
    def setUp(self):
        self.model=ManualControl(config())
        self.origin=np.array(self.model.fk(HOME,50.)['position_m'])

    def step(self,**kwargs):
        values=body(**kwargs)
        return self.model.analog_step(values.pop('positions'),values.pop('gripper'),**values)

    def displacement(self,result):
        return np.array(result['geometry']['position_m'])-self.origin

    def test_fractional_axis_and_speed_scale_preserve_continuous_magnitude(self):
        results=[self.step(translation=[value,0.,0.]) for value in [.25,.5,1.]]
        for value,result in zip([.25,.5,1.],results):
            self.assertEqual(result['status'],'OK')
            self.assertEqual(result['step_fraction'],1.)
            np.testing.assert_allclose(self.displacement(result),[.04*.05*value,0,0],atol=.00004)
        np.testing.assert_allclose(self.displacement(results[1]),self.displacement(results[2])*.5,atol=.00004)
        scaled=self.step(translation=[1.,0.,0.],speed_scale=.25)
        np.testing.assert_allclose(list(scaled['target']['positions'].values()),list(results[0]['target']['positions'].values()),atol=1e-12)

    def test_diagonal_is_capped_without_amplifying_subunit_inputs(self):
        capped=self.step(translation=[1.,1.,0.])
        unit=self.step(translation=[1/math.sqrt(2),1/math.sqrt(2),0.])
        np.testing.assert_allclose(self.displacement(capped),self.displacement(unit),atol=1e-12)
        self.assertLessEqual(np.linalg.norm(self.displacement(capped)),.00204)
        small=self.step(translation=[.2,.2,0.])
        np.testing.assert_allclose(self.displacement(small),[.0004,.0004,0.],atol=.00004)

    def test_control_frame_yaw_rotates_translation_once(self):
        c=config();c['manual_control']['control_frame_yaw_rad']=math.pi/2
        self.model=ManualControl(c)
        r=self.step(translation=[1.,0.,0.])
        self.assertEqual(r['status'],'OK')
        np.testing.assert_allclose(self.displacement(r),[0.,.002,0.],atol=.00004)

    def test_elevation_and_roll_are_analog_rates_not_key_repeats(self):
        start=self.model.fk(HOME,50.)
        for axis,field in [('elevation','elevation_rad'),('roll','tool_roll_rad')]:
            with self.subTest(axis=axis):
                half=self.step(**{axis:.5},dt_s=.03)
                full=self.step(**{axis:1.},dt_s=.03)
                self.assertEqual(half['status'],'OK');self.assertEqual(full['status'],'OK')
                self.assertGreater(full['geometry'][field]-start[field],.008)
                self.assertAlmostEqual(half['geometry'][field]-start[field],(full['geometry'][field]-start[field])/2,delta=.0005)

    def test_zero_throttle_holds_body_but_gripper_target_is_independent(self):
        for level in [1,5]:
            r=self.step(translation=[1.,1.,1.],elevation=1.,roll=-1.,speed_scale=0.,
                        level=level,reference_gripper=20.,gripper_target=100.)
            self.assertEqual(r['target']['positions'],HOME)
            self.assertEqual(r['target']['gripper'],25.)
            self.assertEqual(r['status'],'OK')
        close=self.step(speed_scale=0.,reference_gripper=20.,gripper_target=0.)
        self.assertEqual(close['target']['gripper'],15.)
        exact=self.step(reference_gripper=20.,gripper_target=22.)
        self.assertEqual(exact['target']['gripper'],22.)

    def test_zero_sticks_none_or_unchanged_gripper_hold_the_reference(self):
        reference={**HOME,'shoulder_pan':.1}
        for target in [None,37.]:
            r=self.step(reference_positions=reference,reference_gripper=37.,gripper_target=target)
            self.assertEqual(r['status'],'IDLE')
            self.assertEqual(r['target']['positions'],reference)
            self.assertEqual(r['target']['gripper'],37.)
        r=self.step(translation=[1.,0.,0.],elevation=1.,gripper_target=100.,dt_s=0.)
        self.assertEqual(r['status'],'IDLE');self.assertEqual(r['target']['positions'],HOME)
        self.assertEqual(r['target']['gripper'],50.)

    def test_dt_cap_joint_velocity_bounds_and_repeated_reference_progress(self):
        limited=self.step(gripper_target=100.,dt_s=10.)
        self.assertEqual(limited['dt_s'],.1);self.assertEqual(limited['target']['gripper'],60.)
        first=self.step(translation=[1.,0.,0.],dt_s=.03)
        second=self.step(translation=[1.,0.,0.],dt_s=.03,reference_positions=first['target']['positions'])
        self.assertGreater(second['geometry']['position_m'][0],first['geometry']['position_m'][0]+.0008)
        for name,value in first['target']['positions'].items():self.assertLessEqual(abs(value-HOME[name]),2*.03+1e-10)
        for joint in config()['joints']:
            self.assertLessEqual(second['target']['positions'][joint['name']],joint['max_rad'])
            self.assertGreaterEqual(second['target']['positions'][joint['name']],joint['min_rad'])

    def test_invalid_schema_values_reject_before_any_target_is_returned(self):
        cases=[{'translation':v} for v in [None,[0.,0.],[0.,0.,0.,0.],[[0.],[0.],[0.]],['0',0,0],[True,0,0],[float('nan'),0,0],[1.01,0,0]]]
        for field in ['elevation','roll']:
            cases.extend({field:v} for v in [True,'0',None,float('inf'),-1.01,1.01])
        cases.extend({'speed_scale':v} for v in [True,'1',float('nan'),-.1,1.1])
        cases.extend({'gripper_target':v} for v in [True,'50',float('nan'),-1.,101.])
        cases.extend({'dt_s':v} for v in [True,'0.1',float('inf'),-.1])
        cases.extend({'level':v} for v in [True,0,6,2.5])
        for values in cases:
            with self.subTest(values=values),self.assertRaises((ValueError,TypeError)):
                self.step(**values)

    def test_keyboard_home_and_input_objects_are_unchanged(self):
        home_before=self.model.preset('home',{**HOME,'wrist_roll':.2},37.)
        keyboard_before=self.model.step(HOME,50.,keys=['KeyJ'],dt_s=.03,level=2)
        request=body(translation=[.4,.1,0.]);original=copy.deepcopy(request)
        self.model.analog_step(request['positions'],request['gripper'],**{k:v for k,v in request.items() if k not in ('positions','gripper')})
        self.assertEqual(request,original)
        self.assertEqual(self.model.preset('home',{**HOME,'wrist_roll':.2},37.),home_before)
        keyboard_after=self.model.step(HOME,50.,keys=['KeyJ'],dt_s=.03,level=2)
        self.assertEqual(keyboard_after,keyboard_before)
        self.assertGreater(keyboard_rates(['KeyJ'])['translation_m_s'][0],0.)
        self.assertLess(keyboard_rates(['KeyK'])['translation_m_s'][0],0.)
        self.assertTrue(self.model.describe()['analog_rates'])


try:
    from soarm_station.manual_http import ManualRoutes
    from aiohttp import web
except ImportError:
    ManualRoutes=None


class Request:
    method='POST'
    def __init__(self,operation,payload,arm='arm'):
        self.match_info={'arm':arm,'operation':operation};self.payload=payload
    async def json(self):return self.payload


@unittest.skipIf(ManualRoutes is None,'aiohttp is not installed')
class AnalogRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_route_mount_and_complete_analog_request(self):
        routes=ManualRoutes({'arms':[config()]});app=web.Application();routes.mount(app)
        resources=[r.get_info().get('pattern') for r in app.router.resources()]
        self.assertTrue(any(p and 'analog-step' in p.pattern for p in resources))
        response=await routes.handle(Request('analog-step',body(speed_scale=0.,gripper_target=100.)))
        self.assertEqual(response.status,200)
        payload=json.loads(response.text);self.assertEqual(payload['target']['gripper'],55.)
        self.assertEqual(payload['target']['positions'],HOME)

    async def test_missing_or_invalid_analog_payload_returns_bad_request(self):
        routes=ManualRoutes({'arms':[config()]})
        invalid=body();invalid.pop('translation')
        for payload in [invalid,body(elevation=2.),body(speed_scale=float('nan'))]:
            response=await routes.handle(Request('analog-step',payload));self.assertEqual(response.status,400)
        self.assertEqual((await routes.handle(Request('analog-step',body(),arm='missing'))).status,400)

    async def test_keyboard_and_analog_share_one_per_arm_inflight_lock(self):
        for first,second in [('step','analog-step'),('analog-step','step')]:
            with self.subTest(first=first):
                routes=ManualRoutes({'arms':[config(),config('other')]});entered=threading.Event();release=threading.Event()
                def blocked(*args,**kwargs):
                    entered.set()
                    if not release.wait(2.):raise RuntimeError('test failed to release calculation')
                    return {'status':'IDLE'}
                model=routes.model('arm');model.step=blocked;model.analog_step=blocked
                task=asyncio.create_task(routes.handle(Request(first,body())))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait,1.))
                    rejected=await routes.handle(Request(second,body()))
                    self.assertEqual(rejected.status,409)
                    independent=await routes.handle(Request('analog-step',body(),arm='other'))
                    self.assertEqual(independent.status,200)
                finally:release.set()
                self.assertEqual((await task).status,200)
                accepted=await routes.handle(Request(second,body()))
                self.assertEqual(accepted.status,200)


if __name__=='__main__':unittest.main()
