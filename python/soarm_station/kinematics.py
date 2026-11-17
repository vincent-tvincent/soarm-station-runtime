"""LeRobot/Placo adapter. Position/strict pose endpoints, checked FK residuals."""
import math
import time
import numpy as np
def plan_cartesian(config,targets,start,gripper):
    from lerobot.model.kinematics import RobotKinematics
    if not config.get('urdf'):raise ValueError('Cartesian control requires a registered URDF/TCP')
    names=[j['name'] for j in config['joints']];mapping=config.get('urdf_joint_names',names)
    solver=RobotKinematics(config['urdf'],target_frame_name=config.get('tcp_frame','gripper_frame_link'),joint_names=mapping)
    # The servo's configured position limits are authoritative. URDF limits
    # describe the model, and must not silently constrain this solver as well.
    solver.solver.enable_joint_limits(False)
    q=np.asarray([start[n] for n in names]);result=[];elapsed=0.;deadline=time.monotonic()+config.get('ik_timeout_s',2.)
    for t in targets:
        if t['frame_id']!=config['base_frame']:raise ValueError('Cartesian frame mismatch')
        mode=t.get('orientation_mode','POSITION_ONLY') or 'POSITION_ONLY'
        if mode not in ('POSITION_ONLY','STRICT_POSE'):raise ValueError('Unsupported Cartesian orientation constraint')
        pose=np.eye(4);pose[:3,3]=np.asarray(t['position'],dtype=float)
        if not np.all(np.isfinite(pose)):raise ValueError('Nonfinite Cartesian target')
        if mode=='STRICT_POSE':
            x,y,z,w=t['orientation'];norm=math.sqrt(x*x+y*y+z*z+w*w)
            if abs(norm-1)>.001:raise ValueError('Orientation quaternion must be normalized')
            pose[:3,:3]=[[1-2*(y*y+z*z),2*(x*y-z*w),2*(x*z+y*w)],
                         [2*(x*y+z*w),1-2*(x*x+z*z),2*(y*z-x*w)],
                         [2*(x*z-y*w),2*(y*z+x*w),1-2*(x*x+y*y)]]
        found=False
        for _ in range(12):
            if time.monotonic()>deadline:raise TimeoutError('No IK solution within budget')
            candidate=np.deg2rad(solver.inverse_kinematics(np.rad2deg(q),pose,orientation_weight=0. if mode=='POSITION_ONLY' else 1.,max_iters=8))
            if not np.all(np.isfinite(candidate)):raise ValueError('Nonfinite IK result')
            actual=solver.forward_kinematics(np.rad2deg(candidate))
            p_error=np.linalg.norm(actual[:3,3]-pose[:3,3])
            angle=math.acos(float(np.clip((np.trace(actual[:3,:3].T@pose[:3,:3])-1)/2,-1,1)))
            q=candidate
            if p_error<=config.get('position_tolerance_m',.005) and (mode=='POSITION_ONLY' or angle<=config.get('orientation_tolerance_rad',.05)):
                found=True;break
        if not found:raise ValueError('IK FK residual exceeds tolerance')
        duration=t['duration_s']
        if not math.isfinite(duration) or duration<=0:raise ValueError('Positive waypoint duration required')
        elapsed+=duration
        result.append({'positions':dict(zip(names,q.tolist())),'gripper':gripper,'time_s':elapsed})
    return result
