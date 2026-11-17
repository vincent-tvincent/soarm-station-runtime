"""Incremental manual tasks on LeRobot's persistent Placo kinematics model."""
import math
import numpy as np


class JogKinematics:
    def __init__(self, model):
        from lerobot.model.kinematics import RobotKinematics

        self.backend = RobotKinematics(str(model.config['urdf']),
                                       target_frame_name=model.tcp, joint_names=list(model.mapping))
        self.robot = self.backend.robot
        self.solver = self.backend.solver
        self.solver.enable_joint_limits(False)  # Servo calibration owns absolute angles.
        self.solver.enable_velocity_limits(True)
        for name, speed in zip(model.mapping, model.velocity):
            self.robot.set_velocity_limit(name, float(speed))
        for name in set(self.robot.joint_names()) - set(model.mapping):
            self.solver.mask_dof(name)
        # Penalize unnecessary joint excursion. At a straight elbow an undamped
        # minimum-error task can alternate elbow branches on successive ticks.
        self.regularization = self.solver.add_regularization_task(2e-5)
        wrist = model.settings.get('wrist_roll_joint', 'wrist_roll')
        self.wrist = model.mapping[model.names.index(wrist)] if wrist in model.names else None
        self.mapping = model.mapping
        self.operator_rotation = model.operator_rotation
        self.frame = self.backend.tip_frame
        self.orientation = self.frame.orientation()
        self.position = self.frame.position()
        self.position.configure('TCP translation', 'soft', 1.0)
        self.line = self.solver.add_position_task(model.tcp, np.zeros(3))
        self.line.mask.set_axises('yz', 'custom')
        self.length = model.conditioning_length_m
        self.joint_columns = [self.robot.get_joint_v_offset(name) for name in self.mapping]
        self.fixed_roll_columns = [c for c,n in zip(self.joint_columns,self.mapping) if n != self.wrist]

    def advance(self, q, translation, angles, dt, position_only, anchor):
        pose = self.backend.forward_kinematics(np.rad2deg(q))
        tool = pose[:3, :3] @ self.operator_rotation
        z = tool[:, 2]
        horizontal = math.hypot(z[0], z[1])
        if horizontal > 1e-8:
            pitch_axis = np.array([z[1], -z[0], 0.]) / horizontal
        else:
            # At the pole, pick a horizontal axis from the tool itself.
            pitch_axis = tool[:, 0].copy()
            pitch_axis[2] = 0.
            pitch_axis /= np.linalg.norm(pitch_axis)
        tool_y = np.cross(z, pitch_axis)
        # Pitch and axial spin are the two operator orientation controls. Yaw
        # remains free. No orientation rows are used in position-only mode.
        rolling = angles[1] != 0.
        if self.wrist:
            if rolling:
                self.solver.unmask_dof(self.wrist)
            else:
                self.solver.mask_dof(self.wrist)
        self.orientation.mask.set_axises('xz' if rolling else 'x', 'custom')
        rotation_axes = np.stack((pitch_axis, tool_y, z))
        self.orientation.mask.R_custom_world = rotation_axes
        self.orientation.configure('Tool tilt and roll','soft', 0. if position_only else 1.)

        xyz = pose[:3, 3]
        desired = pose.copy()
        desired[:3, 3] = xyz + translation
        elevation = math.atan2(z[2], horizontal)
        tilt_target = elevation
        if anchor is not None:
            # Retain the line through the start of a held translation. Advance
            # along it only from achieved progress, never bank unreachable travel.
            desired[:3, 3] += anchor.cross_track(xyz, translation)
            if position_only:
                anchor.tilt = None
            else:
                tilt_target = anchor.elevation(elevation, angles[0])
        tilt_change = float(np.clip(tilt_target + angles[0], -math.pi/2, math.pi/2) - elevation)
        from .manual_control import _rotation
        desired[:3, :3] = _rotation(pitch_axis, tilt_change) @ _rotation(z, angles[1]) @ pose[:3, :3]
        distance = float(np.linalg.norm(translation))
        if distance:
            along = translation/distance
            normal = np.cross(along, np.eye(3)[np.argmin(np.abs(along))])
            normal /= np.linalg.norm(normal)
            self.line.mask.R_custom_world = np.stack((along, normal, np.cross(along, normal)))
        # A held jog tracks a line in task space. Give transverse error its own
        # weight so a locally impossible direction tapers instead of sliding.
        self.line.target_world = desired[:3, 3]
        line_weight = 12. if distance else 0.
        self.line.configure('Cross-track position', 'soft', line_weight)
        correction = np.linalg.norm(desired[:3,3] - xyz)
        # Larger corrections require stronger damping to stay on the same elbow
        # branch as its lever arm vanishes. The scale is in model metres.
        columns = self.joint_columns if rolling else self.fixed_roll_columns
        orientation_axes = rotation_axes[[0, 2] if rolling else [0]]
        def task_matrix():
            jacobian = self.robot.frame_jacobian(self.backend.target_frame_name, 'local_world_aligned')[:, columns]
            if position_only:
                return jacobian[:3]
            return np.vstack((jacobian[:3], self.length * orientation_axes @ jacobian[3:]))

        if not columns:
            return q.copy(), pose, pose, desired
        task = task_matrix()
        smallest = float(np.linalg.svd(task, compute_uv=False)[-1])
        knee = max(self.length * .02, 1e-9)
        near_singularity = (1 / (1 + (smallest/knee)**2))**2
        self.regularization.configure('Joint motion', 'soft', 1e-7)
        self.frame.T_world_frame = desired
        self.solver.dt = dt
        # A single differential step enforces one period's velocity allowance.
        # Use the LeRobot-owned solver directly to preserve our partial-axis tasks.
        self.solver.solve(True)
        self.robot.update_kinematics()
        if near_singularity > .01:
            trial = task_matrix()
            farther = np.linalg.svd(trial, compute_uv=False)[-1] > smallest
            same_branch = np.linalg.slogdet(trial @ task.T)[0] > 0
            if not (farther and same_branch):
                # Approaching or crossing a singular branch, replay this one
                # period from the original seed with stronger joint damping.
                self.backend.forward_kinematics(np.rad2deg(q))
                damping = 2e-5 + near_singularity * self.length * correction * (1 + line_weight)
                self.regularization.configure('Joint motion', 'soft', damping)
                self.solver.solve(True)
                self.robot.update_kinematics()
        result = np.array([self.robot.get_joint(name) for name in self.mapping])
        achieved = self.robot.get_T_world_frame(self.backend.target_frame_name)
        if not np.all(np.isfinite(result)) or not np.all(np.isfinite(achieved)):
            raise ValueError('Nonfinite LeRobot/Placo jog result')
        return result, pose, achieved, desired


class JogAnchor:
    """Session-local hold coordinates; independent HTTP requests have no history."""
    def __init__(self):
        self.direction = None
        self.origin = None
        self.tilt = None

    def cross_track(self, xyz, translation):
        length = np.linalg.norm(translation)
        direction = translation / length if length else np.zeros(3)
        if self.direction is None or not np.allclose(direction, self.direction, atol=1e-6):
            self.origin = xyz.copy()
        self.direction = direction
        error = self.origin - xyz
        return error - direction * (direction @ error)

    def elevation(self, current, commanded_delta):
        if commanded_delta:
            self.tilt = None
            return current
        if self.tilt is None:
            self.tilt = current
        return self.tilt
