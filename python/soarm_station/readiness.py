"""Monitoring-only readiness projection. Never grants or revokes motion."""
import json
import math


def project_readiness(config, states, receipts, diagnostics, hosts, now):
    components = []
    def add(identifier, label, state, detail, required=True):
        components.append(dict(id=identifier, label=label, state=state,
                               detail=str(detail)[:512], required=required))
    def fresh(key, seconds):
        received = receipts.get(key)
        return received is not None and 0 <= now-received < seconds
    for arm in config.get('arms', []):
        name = arm['id']; state = states.get(name, {})
        age = state.get('feedback_age_ms')
        if not fresh('controller:'+name, 2):
            kind, detail = 'waiting', 'Waiting for controller feedback'
        elif state.get('phase') == 'FAULT':
            kind, detail = 'fault', state.get('fault') or 'Controller reports a fault'
        elif not state.get('feedback_valid',False) or not isinstance(age, (float, int)) or not math.isfinite(age) or not 0 <= age+(now-receipts['controller:'+name])*1000 < 1000:
            kind, detail = 'waiting', 'Controller is connected; measured joint feedback is unavailable'
        else:
            kind, detail = 'ready', 'Fresh measured joint feedback'
        add('controller:'+name, arm.get('label',name), kind, detail)
    for camera in config.get('cameras', []):
        name=camera['id']; key='camera:'+name; record=diagnostics.get(key, {})
        try:source=json.loads(record.get('values',{}).get('status','{}'))
        except (ValueError,TypeError):source={}
        if not isinstance(source,dict):source={}
        source_age=source.get('age_ms')
        if not fresh('diagnostic:'+key, 3):
            kind,detail='waiting','Waiting for camera capture status'
        elif source.get('error') or not source.get('ready'):
            kind='fault' if source.get('error') else 'starting'
            detail=source.get('error') or 'Opening camera and validating capture mode'
        elif type(source_age) not in (int,float) or not math.isfinite(source_age) or not 0 <= source_age+(now-receipts['diagnostic:'+key])*1000 < 1000:
            kind,detail='waiting','Camera capture has no recent source frame'
        elif not fresh('image:'+name, 2):
            kind,detail='waiting','Capture is live; waiting for image delivery to dashboard'
        else:
            kind,detail='ready',f"{source.get('width')} × {source.get('height')} · capture and dashboard delivery live"
        add(key,camera.get('label',name),kind,detail)
    key='/soarm_station/telemetry_node/storage'
    record=diagnostics.get(key,{})
    if not fresh('diagnostic:'+key, 4):
        add('telemetry','Telemetry','waiting','Waiting for storage and logging status')
    else:
        values=record.get('values',{})
        state=values.get('storage_state','waiting')
        if state not in ('ready','waiting','fault'):state='waiting'
        add('telemetry','Telemetry',state,values.get('storage_reason') or record.get('message') or 'Logging is ready')
    for host in hosts:
        connected=host.get('connected',False)
        configured=any(d.get('configured') for d in host.get('devices',[]))
        state='ready' if connected and configured else 'waiting' if connected else 'offline'
        detail=('Configured leader is available; following requires Start' if configured else 'Connected; no configured leader device detected') if connected else 'Input host is offline; remembered configuration only'
        add('input:'+host['id'],host.get('label',host['id']),state,detail,False)
    pending=[c for c in components if c['required'] and c['state']!='ready']
    return {'state':'degraded' if pending else 'ready','components':components}
