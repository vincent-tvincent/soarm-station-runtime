"""Public dashboard descriptors and cached motor telemetry; no device access."""
import math
from .motor_issues import project_motor_issues,validate_motor_issues,motor_issue_for


def finite(value):
    return float(value) if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) else None


def camera_calibrated(camera):
    matrix=(camera.get('intrinsics') or {}).get('k')
    return isinstance(matrix,(list,tuple)) and len(matrix)==9 and finite(matrix[0]) is not None and matrix[0]>0


def resources(config):
    profiles=[]
    for identifier,p in config.get('profiles',{}).items():
        profiles.append({'id':identifier,'label':p.get('label',identifier),'arm_id':p.get('arm_id'),
            'backend':p.get('backend','local'),'language_conditioned':bool(p.get('language_conditioned',False)),
            'action_fps':finite(p.get('action_fps'))})
    arms=[]
    for a in config.get('arms',[]):
        gripper=a.get('gripper',{})
        arms.append({'id':a['id'],'label':a.get('label',a['id']),'backend':a['backend'],
            'read_only':bool(a.get('read_only',False)),'base_frame':a.get('base_frame'),
            'tcp_frame':a.get('tcp_frame','gripper_frame_link') if a.get('urdf') else None,
            'joints':[{'name':j['name'],'label':j.get('label',j['name']),'unit':'rad',
                'min':j['min_rad'],'max':j['max_rad'],'velocity_limit':j['velocity_rad_s']} for j in a['joints']],
            'gripper':{'name':gripper.get('name','gripper'),'label':gripper.get('label',gripper.get('name','gripper')),
                'unit':'percent','min':0,'max':100,'velocity_limit':gripper.get('velocity_percent_s',100)},
            'capabilities':{'operation_types':['DIRECT','TRAJECTORY','MODEL'],'input_modes':['INLINE','STREAM'],
                'writable':not a.get('read_only',False),'cartesian_endpoints':bool(a.get('urdf'))},
            'profile_ids':[p['id'] for p in profiles if p['arm_id']==a['id']]})
    return {'schema_version':1,'arms':arms,'cameras':[{'id':c['id'],'label':c.get('label',c['id']),
        'width':c.get('width',320 if c.get('backend')=='mock' else 640),
        'height':c.get('height',240 if c.get('backend')=='mock' else 480),'requested_fps':c.get('fps',30),
        'frame_id':c.get('frame_id',c['id']+'_optical_frame'),'calibrated':camera_calibrated(c)} for c in config.get('cameras',[])],
        'profiles':profiles,'operation_contract':{'schema_version':1,
            'control_websocket':{'operation_type':'DIRECT','mode':'STREAM'},'stream_endpoint':'/api/stream',
            'operate_endpoint':'/api/operate','feedback_endpoint':'/api/operations/{request_id}'}}


def motor_definitions(config,calibration=None):
    """Native IDs come from calibration; configured IDs label simulated adapters."""
    calibration=calibration or {}
    result=[]
    for role,settings in [('joint',j) for j in config.get('joints',[])]+[('gripper',config.get('gripper',{}))]:
        name=settings.get('name','gripper')
        result.append({'id':calibration.get(name,{}).get('id',settings.get('motor_id')),
            'name':name,'label':settings.get('label',name),'role':role,'unit':'rad' if role=='joint' else 'percent'})
    return result


def health_rows(config,calibration,torque,voltage,temp,goals=None,source='measured'):
    rows=[]
    for row in motor_definitions(config,calibration):
        name=row['name'];goal=(goals or {}).get(name)
        rows.append({**row,'torque_enabled':bool(torque[name]) if name in torque else None,
            'voltage_v':finite(voltage.get(name)),'temperature_c':finite(temp.get(name)),
            'current_a':None,'goal_position':finite(math.radians(goal) if row['role']=='joint' and goal is not None else goal),
            'source_state':source})
    return rows


def motor_snapshot(snapshot,arm_config=None):
    """Wire-only projection of existing owner data, excluding command envelopes."""
    health=snapshot.get('health',{})
    return {'arm_id':snapshot['arm_id'],'health_time_ns':snapshot.get('health_time_ns',0),
        'health_source':health.get('source','unavailable'),'motors':health.get('motors',[]),
        'positions':{n:finite(v) for n,v in snapshot.get('positions',{}).items()},'gripper':finite(snapshot.get('gripper')),
        'position_time_ns':snapshot.get('time_ns',0),
        'reference_positions':{n:finite(v) for n,v in snapshot['reference_positions'].items()} if snapshot.get('reference_positions') is not None else None,
        'reference_gripper':finite(snapshot.get('reference_gripper')),
        'motion_issues':project_motor_issues(snapshot,arm_config=arm_config)}


def stamp_ns(message):
    stamp=message.get('header',{}).get('stamp',{})
    return int(stamp.get('sec',0))*10**9+int(stamp.get('nanosec',0))


def validate_motor_record(record,arm):
    """Validate/cache only the small public projection, never arbitrary diagnostics."""
    if not isinstance(record,dict) or record.get('arm_id')!=arm['id']:raise ValueError('Motor telemetry arm mismatch')
    definitions={d['name']:d for d in motor_definitions(arm)}
    def timestamp(key):
        value=record.get(key,0)
        if type(value)!=int or not 0<=value<2**63:raise ValueError('Invalid motor timestamp')
        return value
    def number(value):
        if value is not None and finite(value) is None:raise ValueError('Invalid motor numeric value')
        return finite(value)
    def positions(key,nullable=False):
        values=record.get(key,None if nullable else {})
        if values is None and nullable:return None
        if not isinstance(values,dict) or len(values)>len(arm['joints']):raise ValueError('Invalid motor positions')
        expected={j['name'] for j in arm['joints']}
        if not set(values)<=expected:raise ValueError('Unknown motor position name')
        return {name:number(value) for name,value in values.items()}
    source=record.get('health_source','unavailable')
    if source not in ('measured','simulated','unavailable'):raise ValueError('Invalid motor telemetry source')
    motors=record.get('motors',[])
    if not isinstance(motors,list) or len(motors)>len(definitions):raise ValueError('Invalid motor array')
    checked=[];seen=set()
    for row in motors:
        if not isinstance(row,dict) or not isinstance(row.get('name'),str) or row['name'] not in definitions or row['name'] in seen:
            raise ValueError('Invalid or duplicate motor name')
        name=row['name'];seen.add(name);identifier=row.get('id')
        if identifier is not None and (type(identifier)!=int or not 1<=identifier<=253):raise ValueError('Invalid motor ID')
        torque=row.get('torque_enabled')
        if torque is not None and type(torque)!=bool:raise ValueError('Invalid motor torque state')
        checked.append({**definitions[name],'id':identifier,'torque_enabled':torque,
            **{key:number(row.get(key)) for key in ('goal_position','temperature_c','voltage_v','current_a')}})
    return {'arm_id':arm['id'],'health_time_ns':timestamp('health_time_ns'),'position_time_ns':timestamp('position_time_ns'),
        'health_source':source,'motors':checked,'positions':positions('positions'),'gripper':number(record.get('gripper')),
        'reference_positions':positions('reference_positions',True),'reference_gripper':number(record.get('reference_gripper')),
        'motion_issues':validate_motor_issues(record.get('motion_issues'),definitions.keys())}


def render_motors(config,records,joints,states,wall_ns,errors=None):
    result={}
    for arm in config.get('arms',[]):
        identifier=arm['id'];record=records.get(identifier,{})
        health_ns=int(record.get('health_time_ns') or 0);gripper_ns=int(record.get('position_time_ns') or 0);position_ns=gripper_ns
        positions=dict(record.get('positions',{}));message=joints.get(identifier,{})
        if stamp_ns(message)>=position_ns and stamp_ns(message)>0:
            positions=dict(zip(message.get('name',[]),message.get('position',[])));position_ns=stamp_ns(message)
        health_age=max(0,(wall_ns-health_ns)/1e6) if health_ns else None
        raw={r['name']:r for r in record.get('motors',[])}
        source=record.get('health_source','unavailable')
        error=(errors or {}).get(identifier)
        stale=(health_age is not None and health_age>2500) or bool(error)
        if states.get(identifier,{}).get('phase')=='FAULT':stale=True
        state='unavailable' if not health_ns else 'stale' if stale else source
        rows=[]
        for definition in motor_definitions(arm):
            name=definition['name'];r=raw.get(name,{})
            stamp=position_ns if definition['role']=='joint' else gripper_ns
            position_age=max(0,(wall_ns-stamp)/1e6) if stamp else None
            position=positions.get(name) if definition['role']=='joint' else record.get('gripper')
            reference=(record.get('reference_positions') or {}).get(name) if definition['role']=='joint' else record.get('reference_gripper')
            row={**definition,'id':r.get('id',definition['id']),'position':finite(position) if stamp else None,
                'position_time_ns':stamp or None,'position_age_ms':position_age,
                'goal_position':finite(r.get('goal_position')),'reference_position':finite(reference),
                'torque_enabled':r.get('torque_enabled') if isinstance(r.get('torque_enabled'),bool) else None,
                'temperature_c':finite(r.get('temperature_c')),'voltage_v':finite(r.get('voltage_v')),'current_a':finite(r.get('current_a')),
                'source_state':('stale' if position_age is not None and position_age>500 and health_ns else state) if r else 'unavailable'}
            issue_fresh=bool(gripper_ns) and 0<=(wall_ns-gripper_ns)/1e6<=500 and not error and not stale
            row['motion_issue']=motor_issue_for(record.get('motion_issues'),name,fresh=issue_fresh)
            row['unavailable_fields']=[k for k in ('id','position','goal_position','reference_position','torque_enabled','temperature_c','voltage_v','current_a') if row[k] is None]
            rows.append(row)
        if state!='unavailable' and any(row['source_state']=='stale' for row in rows):state='stale'
        result[identifier]={'schema_version':1,'arm_id':identifier,'state':state,'health_time_ns':health_ns or None,
            'health_age_ms':health_age,'motors':rows,'error':error}
    return result
