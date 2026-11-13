"""Portable, explicit resources. Local paths are resolved relative to the YAML."""
import hashlib
import json
import math
import re
from pathlib import Path
import yaml

def load(path):
    path = Path(path).expanduser().resolve()
    c = yaml.safe_load(path.read_text())
    if not isinstance(c, dict) or c.get('schema_version') != 1:
        raise ValueError('schema_version must be 1')
    for kind in ('arms', 'cameras'):
        resources = c.setdefault(kind, [])
        ids = [r['id'] for r in resources]
        if len(ids) != len(set(ids)) or any(not re.fullmatch('[a-z][a-z0-9_]*', x) for x in ids):
            raise ValueError(f'{kind}: duplicate or invalid resource ID')
    ports = [str(Path(a['port']).resolve()) for a in c['arms'] if a['backend'] != 'mock']
    if len(ports) != len(set(ports)):
        raise ValueError('One arm adapter per physical serial bus is required')
    devices = [str(Path(str(a['device'])).resolve()) for a in c['cameras'] if a.get('backend') != 'mock']
    if len(devices) != len(set(devices)):
        raise ValueError('Camera devices must have exactly one capture owner')
    for a in c['arms']:
        if a['backend'] not in ('mock','so101'):
            raise ValueError('Unsupported arm backend: ' + a['backend'])
        if not a.get('joints') or len({j['name'] for j in a['joints']}) != len(a['joints']):
            raise ValueError('An explicit unique joint list is required')
        for j in a['joints']:
            for k in ('min_rad', 'max_rad', 'velocity_rad_s'):
                if not math.isfinite(j[k]): raise ValueError('Nonfinite joint limit')
            if j['min_rad'] >= j['max_rad'] or j['velocity_rad_s'] <= 0:
                raise ValueError('Invalid joint limit')
            tolerance=j.get('goal_tolerance_rad',.03)
            if not math.isfinite(tolerance) or not 0<tolerance<=j['max_rad']-j['min_rad']:
                raise ValueError('Invalid joint goal tolerance')
        lead=a.get('max_target_lead_rad',.12)
        if not math.isfinite(lead) or lead<=0:raise ValueError('Invalid target lead')
        gripper_tolerance=a.get('gripper',{}).get('goal_tolerance_percent',3.)
        if not math.isfinite(gripper_tolerance) or not 0<gripper_tolerance<=100:
            raise ValueError('Invalid gripper goal tolerance')
        for k in ('calibration', 'urdf'):
            if a.get(k): a[k] = str((path.parent / Path(a[k]).expanduser()).resolve())
    for k in ('database','socket','frame_dir','lock_dir'):
        v=c.get(k, f'local/{k}')
        c[k]=str((path.parent / Path(v).expanduser()).resolve())
    c.setdefault('control_hz',100)
    if not 1 <= c['control_hz'] <= 500: raise ValueError('Invalid control_hz')
    c.setdefault('profiles', {})
    for profile in c['profiles'].values():
        if profile.get('bundle'):profile['bundle']=str((path.parent/Path(profile['bundle']).expanduser()).resolve())
    if c.get('collector',{}).get('ca'):
        c['collector']['ca']=str((path.parent/Path(c['collector']['ca']).expanduser()).resolve())
    c['_path']=str(path)
    c['_revision']=hashlib.sha256(json.dumps(c,sort_keys=True).encode()).hexdigest()
    return c
