import json
import plistlib
import sys
from pathlib import Path
import pytest
from soarm_station.input_host_service import install
from soarm_station.leader_input import load_config


def test_install_survives_original_source_and_profile_removal(tmp_path):
    source=tmp_path/'source';package=source/'python'/'soarm_station';package.mkdir(parents=True)
    (package/'__init__.py').write_text('')
    (package/'leader_input.py').write_text('# source snapshot')
    original=tmp_path/'original';original.mkdir()
    calibration=b'{"pan":{"id":1,"homing_offset":42}}\n'
    (original/'leader.json').write_bytes(calibration)
    profile={'host_id':'mac','leaders':[{'id':'one','calibration':'leader.json','match':{'serial':'abc'}}]}
    (original/'input.json').write_text(json.dumps(profile))
    destination=tmp_path/'Application Support'/'leader'
    plist=install(source,destination,original/'input.json',sys.executable,'https://station.invalid',launch_agents=tmp_path/'LaunchAgents')
    for path in original.iterdir():path.unlink()
    (package/'leader_input.py').unlink()
    installed=load_config(destination/'input-host.json')
    assert Path(installed['leaders'][0]['calibration']).read_bytes()==calibration
    job=plistlib.loads(plist.read_bytes())
    assert job['KeepAlive'] and job['RunAtLoad']
    assert job['ProgramArguments'][0]==sys.executable
    assert (Path(job['EnvironmentVariables']['PYTHONPATH'])/'soarm_station'/'leader_input.py').read_text()=='# source snapshot'
    assert not any('start'==arg or 'permission' in arg for arg in job['ProgramArguments'])


def test_install_rejects_unrelated_nonempty_destination(tmp_path):
    config=tmp_path/'input.json';config.write_text('{"leaders":[]}')
    destination=tmp_path/'foreign';destination.mkdir();(destination/'file').write_text('preserve')
    with pytest.raises(ValueError,match='not a Station'):
        install(tmp_path,destination,config,sys.executable,'https://station.invalid')
    assert (destination/'file').read_text()=='preserve'
