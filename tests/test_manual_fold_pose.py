"""Recorded Fold coordinates are independent of optional display extents."""
import copy
import math
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest
import yaml

from soarm_station.control import ArmController
from soarm_station.manual_control import ManualControl

ROOT=Path(__file__).resolve().parents[1]
OBSERVED_DEG={'shoulder_pan':-0.7032967032967034,'shoulder_lift':-104.79120879120879,
              'elbow_flex':95.25274725274726,'wrist_flex':74.85714285714286,
              'wrist_roll':-0.9230769230769231}

def arm_config():
    settings=yaml.safe_load((ROOT/'config/so101-manual-poses.yaml').read_text())['manual_control']
    urdf=ROOT/'models/so101/so101.urdf';tree=ET.parse(urdf)
    joints=[]
    for name in OBSERVED_DEG:
        limit=tree.find(f"joint[@name='{name}']/limit")
        joints.append({'name':name,'min_rad':float(limit.get('lower')),
                       'max_rad':float(limit.get('upper')),'velocity_rad_s':1.5})
    return {'id':'fold_test','backend':'mock','base_frame':'base_link','tcp_frame':'gripper_frame_link',
            'urdf':str(urdf),'joints':joints,'gripper':{'name':'gripper'},'manual_control':settings}

def test_recorded_fold_is_available_and_preserves_jaw_without_changing_home():
    config=arm_config();before=copy.deepcopy(config)
    model=ManualControl(config);home=config['manual_control']['poses']['home']['positions']
    assert model.describe()['presets']['fold']
    result=model.preset('fold',home,37.)
    assert result['target']['positions']==pytest.approx({n:math.radians(v) for n,v in OBSERVED_DEG.items()},abs=1e-15)
    assert result['target']['gripper']==37.
    assert result['geometry']['frame_id']=='base_link'
    assert config==before

def test_native_target_validation_accepts_finite_angles_below_old_display_floor():
    config=arm_config();shoulder=next(j for j in config['joints'] if j['name']=='shoulder_lift')
    assert shoulder['min_rad']==math.radians(-105)
    model=ManualControl(config)
    target=model.preset('fold',config['manual_control']['poses']['home']['positions'],42.)['target']
    arm=ArmController(config,{'control_hz':100,'profiles':{}},'test_boot',{})
    try:
        arm._target(target)
        outside=copy.deepcopy(target);outside['positions']['shoulder_lift']=math.radians(-105)-1e-6
        positions,_=arm._target(outside)
        assert positions==outside['positions']
        assert arm.backend.writes==0
    finally:arm.pool.shutdown(wait=False,cancel_futures=True)

def test_narrower_display_metadata_does_not_disable_the_recorded_pose():
    config=arm_config()
    next(j for j in config['joints'] if j['name']=='shoulder_lift')['min_rad']=math.radians(-100)
    model=ManualControl(config)
    assert model.describe()['presets']['fold']
    result=model.preset('fold',config['manual_control']['poses']['home']['positions'],50.)
    assert result['target']['positions']['shoulder_lift']==pytest.approx(math.radians(OBSERVED_DEG['shoulder_lift']))
