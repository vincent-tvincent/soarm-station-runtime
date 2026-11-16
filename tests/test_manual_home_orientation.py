"""Configured Home orientation; pure URDF math, no device/control imports."""
import math
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np
import yaml

from soarm_station.manual_control import ManualControl


ROOT=Path(__file__).resolve().parents[1]


def home_model():
    settings=yaml.safe_load((ROOT/'config/so101-manual-poses.yaml').read_text())['manual_control']
    urdf=ROOT/'models/so101/so101.urdf'
    joints={joint.get('name'):joint for joint in ET.parse(urdf).getroot().findall('joint')}
    names=list(settings['poses']['home']['positions'])
    configuration={'id':'configured_home_test','base_frame':'base_link','tcp_frame':'gripper_frame_link',
        'urdf':str(urdf),'manual_control':settings,'joints':[]}
    for name in names:
        limit=joints[name].find('limit')
        configuration['joints'].append({'name':name,'min_rad':float(limit.get('lower')),
            'max_rad':float(limit.get('upper')),'velocity_rad_s':float(limit.get('velocity'))})
    return ManualControl(configuration),settings


def test_full_home_points_displayed_tcp_y_straight_up_and_stays_inside_declared_joint_limits():
    model,settings=home_model()
    home=settings['poses']['home']['positions']
    result=model.preset('home',{**home,'wrist_roll':.3},37.5)
    target=result['target']['positions']
    np.testing.assert_allclose(np.asarray(result['geometry']['rotation_matrix'])[:,1],[0.,0.,1.],atol=1e-12,rtol=0)
    assert target['shoulder_pan']==0.
    assert target['shoulder_lift']==math.radians(-50.)
    assert target['elbow_flex']==math.radians(65.)
    assert result['target']['gripper']==37.5 and settings['poses']['home']['gripper']=='preserve'
    for joint in model.config['joints']:
        assert joint['min_rad']<=target[joint['name']]<=joint['max_rad']
    old={**home,'wrist_flex':0.,'wrist_roll':0.}
    movement=np.linalg.norm(np.asarray(result['geometry']['position_m'])-np.asarray(model.fk(old,37.5)['position_m']))
    assert abs(movement*1000-49.79860996958811)<1e-8


def test_home_change_preserves_independent_roll_reference_operator_yaw_speeds_and_partial_home_semantics():
    model,settings=home_model();home=settings['poses']['home']['positions']
    assert settings['wrist_roll_reference_rad']==0.
    assert settings['operator_tool_rpy']==[0.,0.,math.pi]
    assert settings['solve_budget_ms']==40.
    for name,expected in [('minus_90',-math.pi/2),('reference',0.),('plus_90',math.pi/2)]:
        assert model.roll_preset(name,home,41.)['target']['positions']['wrist_roll']==expected
    description=model.describe()
    assert description['default_level']==3
    assert [entry['translation_m_s'] for entry in description['speed_levels']]==[.015,.040,.080,.120,.160]
    partial=model.preset('home',{**home,'wrist_roll':.3},41.,preserve_wrist=True)
    assert partial['target']['positions']['wrist_roll']==.3
    assert partial['target']['positions']['wrist_flex']==home['wrist_flex']
    assert partial['target']['gripper']==41.
