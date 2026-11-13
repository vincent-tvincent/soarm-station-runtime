from soarm_interfaces.action import ExecuteOperation
from soarm_interfaces.msg import OperationInput,DirectJointStep
from .common import duration_s
def direct_body(m):
    if len(m.joint_names)!=len(m.positions_rad):raise ValueError('Joint array length mismatch')
    if len(set(m.joint_names))!=len(m.joint_names):raise ValueError('Duplicate joint names')
    return {'positions':dict(zip(m.joint_names,m.positions_rad)), 'gripper':m.gripper_target.value,
            'gripper_unit':m.gripper_target.unit or 'percent'}
def trajectory_points(m):
    result=[]
    if len(m.points)>256 or len(set(m.joint_names))!=len(m.joint_names):raise ValueError('Invalid trajectory size/names')
    for p in m.points:
        if p.velocities or p.accelerations or p.effort:raise ValueError('Only position/time trajectories supported')
        if len(p.positions)!=len(m.joint_names):raise ValueError('Trajectory joint length mismatch')
        result.append({'time_s':duration_s(p.time_from_start),'positions':dict(zip(m.joint_names,p.positions))})
    return result
def request_from_goal(g):
    if g.context.schema_version!=1:raise ValueError('Unsupported schema')
    s=g.input.inline_sequence;k=g.operation.operation_type;mode=g.input.mode
    selected={'MODEL':'model_steps','DIRECT':'direct_steps','TRAJECTORY':'joint_trajectory'}[k]
    if mode=='STREAM':
        if s.model_steps or s.direct_steps or s.cartesian_steps or s.joint_trajectory.points:raise ValueError('STREAM contains inline payload')
        if g.input.stream_topic!='/soarm/lerobot/command_stream':raise ValueError('Unknown stream topic')
    elif mode=='INLINE':
        if g.input.stream_topic:raise ValueError('INLINE has stream topic')
        if k!='MODEL' and s.model_steps or k!='DIRECT' and s.direct_steps:raise ValueError('Incompatible inline payload')
        if k!='TRAJECTORY' and (s.joint_trajectory.points or s.cartesian_steps):raise ValueError('Incompatible trajectory payload')
    else:raise ValueError('Unknown delivery mode')
    c=g.operation.requested_constraints
    import math
    if not math.isfinite(c.required_clearance_m) or c.required_clearance_m<0:raise ValueError('Invalid clearance constraint')
    if c.path_mode not in ('','JOINT_ENDPOINT','CARTESIAN_PATH'):raise ValueError('Unknown path mode')
    if c.max_input_age_ms and not 10<=c.max_input_age_ms<=250:raise ValueError('Requested input age must tighten the 250 ms cap')
    if c.velocity_scaling not in (0.,1.):raise ValueError('Velocity scaling requires a registered motion profile')
    result={'principal':g.context.operator_session_id,'runtime_boot_id':g.context.expected_runtime_boot_id,
        'controller_session':g.context.expected_controller_session,'operation_type':k,'mode':mode,
        'input_timeout_s':(c.max_input_age_ms or 250)/1000,'duration_s':c.max_operation_duration_ms/1000 if c.max_operation_duration_ms else None,'profile_id':g.operation.policy_profile_id,
        'payload_kind':g.input.stream_payload_kind if mode=='STREAM' else s.payload_kind,
        'constraints':{'required_clearance_m':c.required_clearance_m,'cartesian_path':c.path_mode=='CARTESIAN_PATH'}}
    if mode=='INLINE':
        if k=='DIRECT':
            elapsed=0;result['points']=[]
            for p in s.direct_steps:
                if not p.duration_ms:raise ValueError('Positive direct step duration required')
                elapsed+=p.duration_ms/1000
                result['points'].append({**direct_body(p),'time_s':elapsed})
        elif k=='TRAJECTORY':
            if s.cartesian_steps:
                if s.joint_trajectory.points:raise ValueError('Two trajectory bodies populated')
                result['payload_kind']='CARTESIAN_TARGET'
                result['cartesian']=[{'frame_id':p.frame_id,'position':[p.position.x,p.position.y,p.position.z],
                    'orientation_mode':p.orientation_mode,'orientation':[p.orientation.x,p.orientation.y,p.orientation.z,p.orientation.w],
                    'duration_s':p.duration_ms/1000} for p in s.cartesian_steps]
            else:result['points']=trajectory_points(s.joint_trajectory)
        else:
            if not 1<=len(s.model_steps)<=16:raise ValueError('Expected 1..16 model steps')
            result['model_steps']=[{'instruction':m.instruction,'duration_s':m.max_duration_ms/1000 if m.max_duration_ms else None} for m in s.model_steps]
            result['instruction']=s.model_steps[0].instruction
    return result
def frame_to_dict(m):
    if m.schema_version not in (1,2):raise ValueError('Unsupported stream schema')
    body={}
    if m.frame_kind=='COMMAND':
        if m.payload_kind=='DIRECT_JOINT_TARGET':body=direct_body(m.direct_target)
        elif m.payload_kind=='JOINT_TRAJECTORY':body={'points':trajectory_points(m.joint_trajectory)}
        elif m.payload_kind=='MODEL_TASK':body={'instruction':m.model_task.instruction}
        else:raise ValueError('Unsupported payload')
    elif m.payload_kind not in ('','NONE'):raise ValueError('Non-command payload kind')
    return {'frame_kind':m.frame_kind,'runtime_boot_id':m.runtime_boot_id,'controller_session':m.controller_session,
            'operation_id':m.operation_id,'operation_epoch':m.operation_epoch,'principal':m.producer_instance_id,
            'window_id':m.window_id,'sequence':m.sequence,'semantic_revision':m.semantic_revision,'input_id':m.input_id,'body':body}
def goal_from_dict(d,principal):
    g=ExecuteOperation.Goal();c=g.context;c.schema_version=1;c.request_id=d.get('request_id','');c.arm_id=d['arm_id']
    c.expected_runtime_boot_id=d['runtime_boot_id'];c.expected_controller_session=d['controller_session'];c.operator_session_id=principal
    g.operation.operation_type=d['operation_type'];g.operation.policy_profile_id=d.get('profile_id','')
    duration=d.get('duration_s')
    g.operation.requested_constraints.max_operation_duration_ms=0 if duration is None else int(duration*1000)
    g.input.mode=d['mode'];g.input.producer_role='R06'
    kind={'DIRECT':'DIRECT_JOINT_TARGET','TRAJECTORY':'JOINT_TRAJECTORY','MODEL':'MODEL_TASK'}[d['operation_type']]
    if d['mode']=='STREAM':g.input.stream_topic='/soarm/lerobot/command_stream';g.input.stream_payload_kind=kind
    else:
        g.input.inline_sequence.payload_kind=kind
        if d['operation_type']=='DIRECT':
            previous=0
            for p in d['points']:
                m=DirectJointStep();m.input_id=p.get('input_id','');m.joint_names=list(p['positions']);m.positions_rad=list(p['positions'].values())
                m.gripper_target.unit='percent';m.gripper_target.value=float(p.get('gripper',50))
                m.duration_ms=int((p['time_s']-previous)*1000);previous=p['time_s'];g.input.inline_sequence.direct_steps.append(m)
        elif d['operation_type']=='MODEL':
            from soarm_interfaces.msg import ModelTaskStep
            steps=d.get('model_steps') or [{'instruction':d.get('instruction',''),'duration_s':duration}]
            g.input.inline_sequence.model_steps=[ModelTaskStep(instruction=step.get('instruction',''),max_duration_ms=0 if step.get('duration_s') is None else int(step['duration_s']*1000)) for step in steps]
        else:
            from trajectory_msgs.msg import JointTrajectoryPoint
            if not 1<=len(d.get('points',[]))<=256:raise ValueError('Expected 1..256 trajectory points')
            g.input.inline_sequence.joint_trajectory.joint_names=list(d['points'][0]['positions'])
            for p in d['points']:
                m=JointTrajectoryPoint();m.positions=[p['positions'][n] for n in g.input.inline_sequence.joint_trajectory.joint_names]
                m.time_from_start.sec=int(p['time_s']);m.time_from_start.nanosec=int(p['time_s']%1*1e9)
                g.input.inline_sequence.joint_trajectory.points.append(m)
    return g
def stream_from_dict(d,principal):
    m=OperationInput();m.schema_version=2
    for k in ('arm_id','frame_kind','operation_id','runtime_boot_id','controller_session','operation_epoch','window_id','sequence','semantic_revision','input_id'):
        if k in d:setattr(m,k,d[k])
    m.producer_instance_id=principal
    if m.frame_kind=='COMMAND':
        m.payload_kind=d.get('payload_kind','DIRECT_JOINT_TARGET');b=d['body']
        if m.payload_kind in ('MODEL_TASK','JOINT_TRAJECTORY') and (type(d.get('semantic_revision'))!=int or not 0<d['semantic_revision']<2**64):
            raise ValueError('SEMANTIC_REVISION_REQUIRED: upgrade sparse producer')
        if m.payload_kind=='DIRECT_JOINT_TARGET':
            m.direct_target.joint_names=list(b['positions']);m.direct_target.positions_rad=[float(x) for x in b['positions'].values()]
            m.direct_target.gripper_target.unit=b.get('gripper_unit','percent');m.direct_target.gripper_target.value=float(b['gripper'])
        elif m.payload_kind=='MODEL_TASK':m.model_task.instruction=b['instruction']
        elif m.payload_kind=='JOINT_TRAJECTORY':
            from trajectory_msgs.msg import JointTrajectoryPoint
            if not 1<=len(b.get('points',[]))<=256:raise ValueError('Expected 1..256 trajectory points')
            m.joint_trajectory.joint_names=list(b['points'][0]['positions'])
            for point in b['points']:
                p=JointTrajectoryPoint();p.positions=[float(point['positions'][n]) for n in m.joint_trajectory.joint_names]
                p.time_from_start.sec=int(point['time_s']);p.time_from_start.nanosec=int(point['time_s']%1*1e9);m.joint_trajectory.points.append(p)
        else:raise ValueError('Unsupported stream payload')
    else:m.payload_kind='NONE'
    return m
