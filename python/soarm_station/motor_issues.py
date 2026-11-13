"""Pure, non-actuating projection of reported motor issues for the FK view.

Sustained body-joint nonprogress uses an existing runtime progress timer plus
fresh tracking evidence; elapsed time or lead limiting alone is insufficient.
The display does not add a motion guard, and expected gripper contact is excluded.
UI latches use positive issues, then clear only the explicitly listed clear_kinds.
Unknown/missing/stale evidence is not a clear signal.
"""
import copy
import math
import re


KINDS=frozenset(('stall','fault'))
STATES=frozenset(('stall','fault','clear','unknown'))


def _number(value):
    return float(value) if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) else None


def _unknown(reason='No current motor-specific issue observation'):
    return {'state':'unknown','active':None,'kind':None,'code':'','reason':reason,
            'source':'none','clear_kinds':[],'feedback_fresh':False,
            'tracking_error':None,'tracking_error_unit':None,'sample_sequence':None}


def _definitions(snapshot,definitions,arm_config):
    result={}
    if arm_config is not None:
        for joint in arm_config.get('joints',[]):
            result[joint['name']]={'name':joint['name'],'role':'joint','unit':'rad',
                'id':joint.get('motor_id'),
                # Same effective defaults as ArmController's completion check.
                'goal_tolerance':joint.get('goal_tolerance_rad',.03)}
        gripper=arm_config.get('gripper',{});name=gripper.get('name','gripper')
        result[name]={'name':name,'role':'gripper','unit':'percent','id':gripper.get('motor_id'),
                      'goal_tolerance':gripper.get('goal_tolerance_percent',3.)}
    if definitions is not None:
        for item in definitions:result[item['name']]={**result.get(item['name'],{}),**item}
    restricted=bool(result)
    rows=snapshot.get('health',{}).get('motors',[])
    rows=rows if isinstance(rows,list) else []
    for row in rows:
        if not isinstance(row,dict) or not isinstance(row.get('name'),str):continue
        name=row['name']
        if not restricted or name in result:
            result[name]={**result.get(name,{}),'name':name,
                          **{key:row[key] for key in ('id','role','unit') if key in row}}
    if not restricted:
        for name in snapshot.get('positions',{}):result.setdefault(name,{'name':name,'role':'joint','unit':'rad'})
        if not any(row.get('role')=='gripper' for row in result.values()) and 'gripper' in snapshot:
            result.setdefault('gripper',{'name':'gripper','role':'gripper','unit':'percent'})
    return result,{row['name']:row for row in rows if isinstance(row,dict) and isinstance(row.get('name'),str)}


def project_motor_issues(snapshot,definitions=None,*,arm_config=None,max_feedback_age_ms=150.,max_health_age_ms=2500.):
    """Return {schema_version, summary, by_name} from an existing owner snapshot.

    Pass arm_config for its existing per-motor goal tolerances. Without a known
    tolerance, a positive issue is still reported but tracking is never declared
    resolved. A clear observation requires fresh following feedback/reference,
    within that tolerance, and no current runtime error. Fault clearing also
    requires fresh existing per-motor health. Nothing here reads a device.
    """
    definitions,health_rows=_definitions(snapshot,definitions,arm_config)
    positions=snapshot.get('positions',{})
    reference=snapshot.get('reference_positions') or {}
    stats=snapshot.get('stats') or {}
    phase=snapshot.get('phase')
    error=snapshot.get('error')
    error=error[:512] if isinstance(error,str) else ''
    reason=error;source='runtime_error'
    if not reason and phase in ('HOLD','FAULT'):
        held=(snapshot.get('hold_context') or {}).get('reason')
        if isinstance(held,str):reason=held[:512];source='hold_context'
    normalized=reason.removeprefix('HARDWARE_ERROR: ')
    grippers=[name for name,row in definitions.items() if row.get('role')=='gripper']
    gripper_name=grippers[0] if len(grippers)==1 else None
    def named(name):
        if name=='_gripper':name=gripper_name
        return name if name in definitions else None
    id_map={}
    for name,row in definitions.items():
        identifier=row.get('id')
        if type(identifier)==int:id_map.setdefault(identifier,[]).append(name)
    def by_id(identifier):
        names=id_map.get(int(identifier),[])
        return names[0] if len(names)==1 else None
    positives={}
    match=re.fullmatch(r'TRACKING_STALLED:\s*(\S+)',normalized)
    if match and named(match[1]):positives[named(match[1])]=('stall','TRACKING_STALLED',source)
    match=re.fullmatch(r'Servo protection id=(\d+) status=(\d+)',normalized)
    if match and int(match[2])>0 and by_id(match[1]):positives[by_id(match[1])]=('fault','SERVO_PROTECTION',source)
    match=re.fullmatch(r'Invalid read reply id=(\d+) address=\d+ bytes=\d+',normalized)
    if match and by_id(match[1]):positives[by_id(match[1])]=('fault','FEEDBACK_ERROR',source)
    match=re.fullmatch(r'Calibration mismatch:\s*(\S+)',normalized)
    if match and named(match[1]):positives[named(match[1])]=('fault','CALIBRATION_MISMATCH',source)
    limited=stats.get('limited_actuators')
    valid_limited=isinstance(limited,(list,tuple)) and all(isinstance(name,str) for name in limited)
    limited_names={named(name) for name in limited} if valid_limited else set()
    if normalized=='TRACKING_ENVELOPE_LOST' and stats.get('tracking_state')=='REFERENCE_OUTSIDE_ENVELOPE' and valid_limited:
        for name in limited_names-{None}:positives[name]=('fault','TRACKING_ENVELOPE_LOST','runtime_tracking')

    age=_number(snapshot.get('age_ms'))
    sample_ns=snapshot.get('time_ns');health_ns=snapshot.get('health_time_ns')
    timestamp_ok=type(sample_ns)==int and sample_ns>0
    fresh=age is not None and 0<=age<=max_feedback_age_ms and timestamp_ok
    health_age=None
    if timestamp_ok and type(health_ns)==int and health_ns>0 and health_ns<=sample_ns+250_000_000 and age is not None:
        health_age=age+max(0.,(sample_ns-health_ns)/1e6)
    health_fresh=health_age is not None and 0<=health_age<=max_health_age_ms
    tracking=stats.get('tracking_state')
    progress=stats.get('no_progress_s')
    progress=progress if isinstance(progress,dict) else {}
    settings=(arm_config or {}).get('manual_control',{})
    indicator_s=_number(settings.get('stall_indicator_s',1.)) if isinstance(settings,dict) else None
    runtime_clear=(snapshot.get('error')=='' and phase in ('ACTIVE','IDLE') and valid_limited
                   and tracking in ('FOLLOWING','LEAD_LIMITED'))
    sequence=snapshot.get('sequence')
    result={}
    for name,definition in definitions.items():
        row=_unknown()
        is_gripper=definition.get('role')=='gripper'
        measured=_number(snapshot.get('gripper') if is_gripper else positions.get(name))
        admitted=_number(snapshot.get('reference_gripper') if is_gripper else reference.get(name))
        tolerance=_number(definition.get('goal_tolerance',definition.get('goal_tolerance_percent' if is_gripper else 'goal_tolerance_rad')))
        tracking_error=abs(admitted-measured) if admitted is not None and measured is not None else None
        row.update(feedback_fresh=fresh and measured is not None,tracking_error=tracking_error,
                   tracking_error_unit='percent' if is_gripper else 'rad',
                   sample_sequence=sequence if type(sequence)==int and sequence>=0 else None)
        if name in positives:
            kind,code,provenance=positives[name]
            row.update(state=kind,active=True,kind=kind,code=code,reason=reason,source=provenance)
        elif (definition.get('role')=='joint' and phase=='ACTIVE' and row['feedback_fresh']
              and name in limited_names and tolerance is not None and tolerance>0
              and tracking_error is not None and tracking_error>tolerance
              and tracking=='REFERENCE_OUTSIDE_ENVELOPE'):
            row.update(state='fault',active=True,kind='fault',code='REFERENCE_OUTSIDE_ENVELOPE',source='runtime_tracking',
                       reason=f'The runtime reports {name} outside its tracking envelope; admitted reference error is {tracking_error:.4f} rad. Target advancement is waiting; this display adds no stop condition.')
        elif (definition.get('role')=='joint' and phase=='ACTIVE' and row['feedback_fresh']
              and name in limited_names and tolerance is not None and tolerance>0
              and tracking_error is not None and tracking_error>tolerance
              and tracking=='LEAD_LIMITED' and indicator_s is not None and indicator_s>0
              and _number(progress.get(name)) is not None and progress[name]>=indicator_s):
            row.update(state='stall',active=True,kind='stall',code='TRACKING_NOT_PROGRESSING',source='runtime_progress',
                       reason=f'No qualifying measured progress for {progress[name]:.2f} s while the admitted reference remains ahead by {tracking_error:.4f} rad (goal tolerance {tolerance:.4f} rad). Diagnostic only; control is not stopped by this indicator.')
        elif (row['feedback_fresh'] and runtime_clear and name not in limited_names and
              tolerance is not None and tolerance>0 and tracking_error is not None and tracking_error<=tolerance):
            clears=['stall'];health=health_rows.get(name,{})
            if (health_fresh and snapshot.get('health',{}).get('source') in ('measured','simulated')
                and type(health.get('torque_enabled'))==bool
                and _number(health.get('voltage_v')) is not None and _number(health.get('temperature_c')) is not None):
                clears.append('fault')
            row.update(state='clear',code='FOLLOWING_REFERENCE',source='runtime_tracking',clear_kinds=clears,
                       reason='Fresh following feedback is within the configured goal tolerance of the admitted reference; no runtime error is reported.')
        elif not row['feedback_fresh']:
            row['reason']='Feedback is stale or unavailable; retain any previously reported issue.'
        elif name in limited_names:
            row['reason']='Lead limiting is reported, not a confirmed stall or a clear observation.'
        result[name]=row
    return {'schema_version':1,'summary':reason,'by_name':result}


def validate_motor_issues(value,known_names):
    """Validate the small public projection before caching diagnostic JSON."""
    if value is None:return {'schema_version':1,'summary':'','by_name':{}}
    if not isinstance(value,dict) or value.get('schema_version')!=1:raise ValueError('Invalid motor issue schema')
    summary=value.get('summary','');rows=value.get('by_name')
    if not isinstance(summary,str) or len(summary)>512 or not isinstance(rows,dict):raise ValueError('Invalid motor issue projection')
    if not set(rows)<=set(known_names):raise ValueError('Unknown motor issue name')
    checked={}
    for name,row in rows.items():
        if not isinstance(row,dict) or not set(_unknown())<=set(row):raise ValueError('Invalid motor issue row')
        state=row.get('state');kind=row.get('kind');active=row.get('active');clears=row.get('clear_kinds')
        if not isinstance(state,str) or state not in STATES or type(row.get('feedback_fresh'))!=bool:raise ValueError('Invalid motor issue state')
        if (not isinstance(clears,list) or any(not isinstance(kind,str) for kind in clears)
            or len(clears)!=len(set(clears)) or not set(clears)<=KINDS):raise ValueError('Invalid motor issue clear kinds')
        if state in KINDS:
            if active is not True or kind!=state or clears:raise ValueError('Conflicting active motor issue')
        elif active is not None or kind is not None or (state=='clear')!=bool(clears):raise ValueError('Conflicting clear motor issue')
        if clears and not row['feedback_fresh']:raise ValueError('Stale motor issue cannot clear')
        for field,limit in (('code',64),('reason',512),('source',64)):
            if not isinstance(row.get(field),str) or len(row[field])>limit:raise ValueError('Invalid motor issue text')
        error=row.get('tracking_error')
        if error is not None and (_number(error) is None or error<0):raise ValueError('Invalid motor tracking error')
        if row.get('tracking_error_unit') not in (None,'rad','percent'):raise ValueError('Invalid tracking error unit')
        sequence=row.get('sample_sequence')
        if sequence is not None and (type(sequence)!=int or not 0<=sequence<2**64):raise ValueError('Invalid motor issue sequence')
        checked[name]={key:copy.deepcopy(row[key]) for key in _unknown()}
    return {'schema_version':1,'summary':summary,'by_name':checked}


def motor_issue_for(projection,name,*,fresh=True):
    """Attach one validated row to a dashboard motor; stale transport cannot clear.

    An explicitly reported positive issue remains evidence of that unresolved
    report, even if measurements have since aged. Staleness alone creates none.
    """
    row=copy.deepcopy((projection or {}).get('by_name',{}).get(name))
    if row is None:return _unknown()
    if not fresh:
        row['feedback_fresh']=False;row['clear_kinds']=[]
        if row.get('active') is not True:
            row.update(state='unknown',active=None,kind=None,code='',source='stale',
                       reason='Issue observation is stale; retain any previously reported issue.')
    return row
