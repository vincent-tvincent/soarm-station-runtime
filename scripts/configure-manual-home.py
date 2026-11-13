#!/usr/bin/env python3
"""Install the established Home descriptor for one arm; no hardware access."""
import argparse
import copy
from datetime import datetime,timezone
from pathlib import Path
import sys
import yaml
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'python'))
from soarm_station.config import load
from soarm_station.manual_control import ManualControl
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--config',type=Path,required=True);p.add_argument('--arm',required=True)
a=p.parse_args();path=a.config.resolve();raw=yaml.safe_load(path.read_text())
arm=next((arm for arm in raw.get('arms',[]) if arm['id']==a.arm),None)
if arm is None:raise SystemExit('Explicit configured arm not found')
settings=yaml.safe_load((ROOT/'config/so101-manual-poses.yaml').read_text())['manual_control']
existing=arm.get('manual_control',{})
if existing.get('poses',{}).get('home'):
    print('Existing Home preserved; no configuration changed.');raise SystemExit(0)
settings={**settings,**existing,'poses':{**settings['poses'],**existing.get('poses',{})}}
validated=next(x for x in load(path)['arms'] if x['id']==a.arm)
validated['manual_control']=settings
model=ManualControl(validated)
if not model.describe()['presets']['home']:raise SystemExit('Home does not fit the configured model/domain')
backup=path.with_name(path.name+'.before-manual-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
backup.write_bytes(path.read_bytes());backup.chmod(0o600)
arm['manual_control']=copy.deepcopy(settings)
path.write_text(yaml.safe_dump(raw,sort_keys=False));path.chmod(0o600)
print('Installed the explicit Home descriptor for '+a.arm+'; existing fields preserved. No device opened.')
