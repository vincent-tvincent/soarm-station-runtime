import copy
import json
import math
from pathlib import Path

import pytest
import yaml

from soarm_station.config import load
from soarm_station.dashboard_data import resources


def configuration(tmp_path,joint=None):
    path=tmp_path/'robot.yaml'
    path.write_text(yaml.safe_dump({'schema_version':1,'device_id':'test','cameras':[],
        'arms':[{'id':'arm','backend':'mock','joints':[joint or {'name':'q','velocity_rad_s':1.}]}]}))
    return load(path)


def test_joint_angle_fields_are_optional_and_tolerance_does_not_depend_on_their_span(tmp_path):
    config=configuration(tmp_path)
    joint=resources(config)['arms'][0]['joints'][0]
    assert (joint['min'],joint['max'])==(-math.pi,math.pi)
    assert joint['range_source']=='default_display' and joint['range_enforced'] is False
    config=configuration(tmp_path,{'name':'q','velocity_rad_s':1.,'min_rad':-.001,'max_rad':.001,'goal_tolerance_rad':.1})
    assert config['arms'][0]['joints'][0]['goal_tolerance_rad']==.1


@pytest.mark.parametrize('metadata',[{'min_rad':2.,'max_rad':-2.}, {'min_rad':None,'max_rad':'invalid'}])
def test_invalid_legacy_display_extents_are_not_startup_or_command_gates(tmp_path,metadata):
    config=configuration(tmp_path,{'name':'q','velocity_rad_s':1.,**metadata})
    joint=resources(config)['arms'][0]['joints'][0]
    assert joint['range_source']=='default_display' and not joint['range_enforced']


def test_explicit_display_metadata_remains_display_only(tmp_path):
    config=configuration(tmp_path,{'name':'q','velocity_rad_s':1.,'display_min_rad':-4.,'display_max_rad':5.})
    joint=resources(config)['arms'][0]['joints'][0]
    assert (joint['min'],joint['max'],joint['range_source'])==(-4.,5.,'configured_display')
    assert joint['range_enforced'] is False


def test_so101_display_uses_existing_calibration_normalization_without_rewriting_it(tmp_path):
    config=configuration(tmp_path,{'name':'q','velocity_rad_s':1.,'min_rad':-.01,'max_rad':.01})
    calibration=tmp_path/'calibration.json'
    data={'q':{'id':1,'drive_mode':0,'homing_offset':-7,'range_min':512,'range_max':3584}}
    calibration.write_text(json.dumps(data));original=calibration.read_bytes()
    config['arms'][0].update(backend='so101',calibration=str(calibration))
    before=copy.deepcopy(config)
    joint=resources(config)['arms'][0]['joints'][0]
    assert joint['range_source']=='servo_calibration' and joint['range_enforced'] is False
    midpoint=(512+3584)/2
    assert joint['min']==pytest.approx(math.radians((512-midpoint)*360/4095))
    assert joint['max']==pytest.approx(math.radians((3584-midpoint)*360/4095))
    assert calibration.read_bytes()==original and config==before
    assert str(calibration) not in json.dumps(resources(config))


@pytest.mark.parametrize('velocity',[0,-1,True,float('nan')])
def test_velocity_contract_is_still_validated(tmp_path,velocity):
    with pytest.raises(ValueError,match='velocity'):
        configuration(tmp_path,{'name':'q','velocity_rad_s':velocity})
