"""Portable, explicit resources. Local paths are resolved relative to the YAML."""
import hashlib
import json
import math
import os
import re
from pathlib import Path
import yaml

def validate_telemetry_storage(config):
    """Check a required data mount only for the component that opens the journal.

    Configuration loading remains usable by control/camera nodes when optional
    telemetry storage is absent. This check performs no filesystem writes.
    """
    from .telemetry_storage import validate_storage
    return validate_storage(config)

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
    for camera in c['cameras']:
        if 'label' in camera:
            label=camera['label']
            if not isinstance(label,str) or not 1<=len(label)<=128 or not label.strip():
                raise ValueError('Camera labels require 1..128 characters and must not be blank')
    for a in c['arms']:
        if a['backend'] not in ('mock','so101'):
            raise ValueError('Unsupported arm backend: ' + a['backend'])
        if not a.get('joints') or len({j['name'] for j in a['joints']}) != len(a['joints']):
            raise ValueError('An explicit unique joint list is required')
        metadata=[*a['joints'],a.get('gripper',{})]
        motor_ids=[item['motor_id'] for item in metadata if item.get('motor_id') is not None]
        if any(type(value)!=int or not 1<=value<=253 for value in motor_ids) or len(set(motor_ids))!=len(motor_ids):
            raise ValueError('Configured motor IDs must be unique integers in [1,253] per arm')
        for item in [a,*metadata]:
            if 'label' in item and (not isinstance(item['label'],str) or not 1<=len(item['label'])<=128):
                raise ValueError('Resource labels require 1..128 characters')
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
        for key,value in a.get('operation_limits',{}).items():
            if key not in ('max_stream_duration_s','max_inline_duration_s','max_path_duration_s','default_model_step_duration_s','max_model_step_duration_s','settle_timeout_s'):
                raise ValueError('Unknown operation limit: '+key)
            if value is None and key=='max_stream_duration_s':continue
            if not isinstance(value,(int,float)) or not math.isfinite(value) or value<0 or (value==0 and key!='settle_timeout_s'):
                raise ValueError('Invalid operation limit: '+key)
        for actuator in [*a['joints'],a.get('gripper',{})]:
            timeout=actuator.get('tracking_timeout_s')
            values=timeout.values() if isinstance(timeout,dict) else [timeout]
            if any(v is not None and (not isinstance(v,(int,float)) or not math.isfinite(v) or v<=0) for v in values):
                raise ValueError('Tracking timeout must be null or a positive mode/actuator-specific value')
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
    monitoring=c.get('monitoring',{})
    if not isinstance(monitoring,dict):raise ValueError('monitoring must be a mapping')
    for name,default in (('period_s',10),('retention_days',30),('storage_limit_mb',512)):
        value=monitoring.get(name,default)
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0:
            raise ValueError(f'Monitoring {name} must be finite and positive')
    if type(monitoring.get('event_limit',10000))!=int or monitoring.get('event_limit',10000)<1:
        raise ValueError('Monitoring event_limit must be a positive integer')
    storage_mount=monitoring.get('storage_mount')
    if storage_mount is not None:
        if not isinstance(storage_mount,str) or not storage_mount.strip():
            raise ValueError('Monitoring storage_mount must be a nonempty path or null')
        monitoring['storage_mount']=str((path.parent/Path(storage_mount).expanduser()).resolve())
    storage_uuid=monitoring.get('storage_uuid')
    if storage_uuid is not None:
        if not isinstance(storage_uuid,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,127}',storage_uuid):
            raise ValueError('Monitoring storage_uuid must be a filesystem UUID')
        if storage_mount is None:raise ValueError('Monitoring storage_uuid requires storage_mount')
    graph=c.get('graph_observer',{})
    if not isinstance(graph,dict):raise ValueError('graph_observer must be a mapping')
    refresh=graph.get('refresh_s',2.)
    if isinstance(refresh,bool) or not isinstance(refresh,(int,float)) or not math.isfinite(refresh) or not .2<=refresh<=30:
        raise ValueError('Graph refresh_s must be in [0.2,30]')
    sampling=graph.get('sampling',{})
    if not isinstance(sampling,dict) or type(sampling.get('enabled',True))!=bool:raise ValueError('Invalid graph sampling settings')
    topics=sampling.get('max_topics',2);duration=sampling.get('max_duration_s',10.)
    if type(topics)!=int or not 1<=topics<=2:raise ValueError('At most two graph topics may be sampled concurrently')
    if isinstance(duration,bool) or not isinstance(duration,(int,float)) or not math.isfinite(duration) or not .1<=duration<=10:
        raise ValueError('Graph sampling duration must be in [0.1,10]')
    watch=graph.get('watch',{})
    if not isinstance(watch,dict) or type(watch.get('enabled',True))!=bool:
        raise ValueError('Invalid graph watch settings')
    for key,default,maximum in (('max_topics',64,64),('max_clients',8,8),
            ('max_messages_per_window',10000,50000),('max_bytes_per_window',8388608,33554432)):
        value=watch.get(key,default)
        if type(value)!=int or not 1<=value<=maximum:
            raise ValueError(f'Graph watch {key} must be an integer in [1,{maximum}]')
    lease=watch.get('lease_s',12.)
    if isinstance(lease,bool) or not isinstance(lease,(int,float)) or not math.isfinite(lease) or not 3<=lease<=30:
        raise ValueError('Graph watch lease_s must be in [3,30]')
    management=c.setdefault('policy_management',{})
    if not isinstance(management,dict):raise ValueError('policy_management must be a mapping')
    root=Path(__file__).resolve().parents[2]
    defaults={'source_config':root/'config/model-sources.yaml','profiles_dir':root/'local/policy-profiles',
        'cache_db':root/'local/policy-management.sqlite3','run_dir':root/'local/policy-runs'}
    if management.get('profile_store') and not management.get('profiles_dir'):management['profiles_dir']=management['profile_store']
    for key,default in defaults.items():
        value=Path(management.get(key,default)).expanduser()
        management[key]=str((value if value.is_absolute() else path.parent/value).resolve())
    c['_path']=str(path)
    c['_revision']=hashlib.sha256(json.dumps(c,sort_keys=True).encode()).hexdigest()
    return c
