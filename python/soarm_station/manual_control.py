"""Device-free manual FK/IK target generation. Authority stays with DIRECT.

FK and display geometry use NumPy. Manual jogging uses LeRobot/Placo;
this module never opens a device, publishes a command, or grants permission.
"""
from __future__ import annotations

import copy
import hashlib
import math
import threading
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np


SPEEDS = (
    (0.015, math.radians(10), 10.),
    (0.040, math.radians(25), 30.),
    (0.080, math.radians(45), 60.),
    (0.120, math.radians(70), 90.),
    (0.160, math.radians(100), 120.),
)
KEYS = {'x': ['KeyK', 'KeyJ'], 'y': ['KeyL', 'KeyH'], 'z': ['KeyF', 'KeyD'],
        'elevation': ['KeyI', 'KeyU'], 'roll': ['KeyG', 'KeyS'],
        'gripper': ['BracketLeft', 'BracketRight']}
ALIASES = {**{c.lower(): 'Key'+c.upper() for c in 'jklhfdiugs'},
           '[': 'BracketLeft', ']': 'BracketRight', 'Shift': 'ShiftLeft'}


class ManualUnavailable(ValueError):
    """The configured model or requested preset is unavailable."""


def _finite(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float, np.number)) or not math.isfinite(value):
        raise ValueError(label+' must be finite')
    return float(value)


def _vector(value, size, label):
    a = np.asarray(value, dtype=float)
    if a.shape != (size,) or not np.all(np.isfinite(a)):
        raise ValueError(label+f' must contain {size} finite numbers')
    return a


def _wrap(value):
    return (value+math.pi) % (2*math.pi)-math.pi


def _rotation(axis, angle):
    x, y, z = axis
    k = np.array([[0., -z, y], [z, 0., -x], [-y, x, 0.]])
    return np.eye(3)+math.sin(angle)*k+(1-math.cos(angle))*(k@k)


def _rpy(values):
    r, p, y = values
    return _rotation(np.array([0., 0., 1.]), y) @ _rotation(np.array([0., 1., 0.]), p) @ _rotation(np.array([1., 0., 0.]), r)


def _origin(element):
    origin = element.find('origin')
    result = np.eye(4)
    if origin is not None:
        result[:3, 3] = _vector([float(x) for x in origin.get('xyz', '0 0 0').split()], 3, 'URDF xyz')
        result[:3, :3] = _rpy(_vector([float(x) for x in origin.get('rpy', '0 0 0').split()], 3, 'URDF rpy'))
    return result


def _semantics(rotation, reference=None):
    z, x = rotation[:, 2], rotation[:, 0]
    candidates = ([reference] if reference is not None else []) + list(np.eye(3))
    for axis in candidates:
        zero = axis-np.dot(axis, z)*z
        if np.linalg.norm(zero) > 1e-8:
            zero = zero/np.linalg.norm(zero)
            break
    elevation = math.asin(float(np.clip(z[2], -1., 1.)))
    roll = math.atan2(float(np.dot(np.cross(zero, x), z)), float(np.dot(zero, x)))
    return np.array([elevation, roll]), math.atan2(float(z[1]), float(z[0]))


def keyboard_rates(keys, level=3, shift=False, yaw_rad=0., position_only=False):
    """Map browser KeyboardEvent.code (or letter aliases) to explicit SI rates."""
    if type(level) is not int or not 1 <= level <= 5:
        raise ValueError('level must be an integer in [1,5]')
    if type(shift) is not bool or type(position_only) is not bool:
        raise ValueError('shift and position_only must be booleans')
    if not isinstance(keys, (list, tuple, set, frozenset)) or len(keys) > 32 or any(not isinstance(k, str) for k in keys):
        raise ValueError('keys must be a bounded list of keyboard codes')
    held = {ALIASES.get(k, k) for k in keys}
    effective = 5 if shift or held.intersection({'ShiftLeft', 'ShiftRight'}) else level
    linear, angular, jaw = SPEEDS[effective-1]
    delta = lambda name: int(KEYS[name][1] in held)-int(KEYS[name][0] in held)
    direction = np.array([delta(k) for k in ('x', 'y', 'z')], dtype=float)
    length = np.linalg.norm(direction)
    if length > 1: direction /= length
    direction = _rotation(np.array([0., 0., 1.]), _finite(yaw_rad, 'yaw_rad')) @ direction
    return {'translation_m_s': (direction*linear).tolist(),
            'elevation_rad_s': 0. if position_only else delta('elevation')*angular,
            'roll_rad_s': 0. if position_only else delta('roll')*angular,
            'gripper_percent_s': delta('gripper')*jaw,
            'level': effective, 'selected_level': level}


class ManualControl:
    def __init__(self, arm_config):
        self.config = copy.deepcopy(arm_config)
        self.settings = self.config.get('manual_control', {})
        self.names = tuple(j['name'] for j in self.config['joints'])
        if not self.names or len(set(self.names)) != len(self.names):
            raise ValueError('Unique configured body joints required')
        self.velocity = np.array([_finite(j['velocity_rad_s'], 'velocity_rad_s') for j in self.config['joints']])
        if np.any(self.velocity <= 0):
            raise ValueError('Invalid configured joint velocity')
        mapping = self.config.get('urdf_joint_names', list(self.names))
        if isinstance(mapping, dict): mapping = [mapping[n] for n in self.names]
        if len(mapping) != len(self.names) or len(set(mapping)) != len(mapping):
            raise ValueError('URDF joint mapping must be one-to-one')
        self.mapping = tuple(mapping)
        self.frame = self.config.get('base_frame')
        self.tcp = self.config.get('tcp_frame', 'gripper_frame_link')
        self.operator_rotation = _rpy(_vector(self.settings.get('operator_tool_rpy', [0., 0., math.pi]), 3, 'operator_tool_rpy'))
        # Legacy solve_budget_ms remains accepted in configuration, but CPU timing
        # never determines geometric success. Work stops on convergence/no progress.
        self.jog_backend = None
        self._solver_lock = threading.RLock()
        self.model_error = None
        self.chain = []
        self.branch = None
        self.model_base = None
        self.model_sha256 = None
        try: self._load_model()
        except (OSError, ET.ParseError, ValueError) as exc: self.model_error = str(exc)

    def _load_model(self):
        path = self.config.get('urdf')
        if not path: raise ManualUnavailable('No configured URDF')
        data = Path(path).read_bytes()
        if len(data) > 4*1024*1024: raise ValueError('URDF exceeds 4 MiB')
        self.model_sha256 = hashlib.sha256(data).hexdigest()
        robot = ET.fromstring(data)
        links = {link.get('name') for link in robot.findall('link')}
        children = {}
        joints = {}
        for element in robot.findall('joint'):
            name, kind = element.get('name'), element.get('type')
            parent, child = element.find('parent'), element.find('child')
            if not name or name in joints or parent is None or child is None:
                raise ValueError('Invalid or duplicate URDF joint')
            parent, child = parent.get('link'), child.get('link')
            if parent not in links or child not in links or child in children:
                raise ValueError('Invalid or multiply parented URDF link')
            axis_element = element.find('axis')
            axis = _vector([float(x) for x in (axis_element.get('xyz', '1 0 0') if axis_element is not None else '1 0 0').split()], 3, 'joint axis')
            if kind != 'fixed':
                if np.linalg.norm(axis) < 1e-10: raise ValueError('Zero movable joint axis')
                axis /= np.linalg.norm(axis)
            limit = element.find('limit')
            # Limits are model/display metadata, used only for the jaw drawing.
            # Body FK/IK never treats them as actuator admission constraints.
            bounds = None
            if limit is not None:
                try:
                    low,high=float(limit.get('lower')),float(limit.get('upper'))
                    if math.isfinite(low) and math.isfinite(high) and low<high:bounds=(low,high)
                except (TypeError,ValueError):pass
            joint = {'name': name, 'kind': kind, 'parent': parent, 'child': child, 'origin': _origin(element), 'axis': axis, 'mimic': element.find('mimic') is not None, 'bounds': bounds}
            joints[name] = joint; children[child] = joint
        if self.tcp not in links: raise ManualUnavailable('Configured TCP link is absent from URDF')
        chain, visited, link = [], set(), self.tcp
        while link in children:
            if link in visited: raise ValueError('Cyclic URDF chain')
            visited.add(link); joint = children[link]; chain.append(joint); link = joint['parent']
        self.model_base = link
        chain.reverse()
        moving = [j['name'] for j in chain if j['kind'] != 'fixed']
        if set(moving) != set(self.mapping):
            raise ManualUnavailable('TCP chain movable joints do not match configured body joints')
        for joint in chain:
            if joint['kind'] not in ('fixed', 'revolute', 'continuous') or joint['mimic']:
                raise ManualUnavailable('Only fixed and independent rotational TCP-chain joints are supported')
        self.chain = chain
        # A fixed model scale, independent of pose and base placement. Use the
        # chain's translation lengths after the first actuated joint through TCP.
        first_moving = next(i for i,joint in enumerate(chain) if joint['kind'] != 'fixed')
        self.conditioning_length_m = float(sum(np.linalg.norm(joint['origin'][:3,3])
            for joint in chain[first_moving+1:]))
        jaw = joints.get(self.config.get('gripper', {}).get('urdf_joint_name', self.config.get('gripper', {}).get('name', 'gripper')))
        self.branch = jaw if jaw and jaw['name'] not in moving else None
        if not self.frame: self.frame = self.model_base

    def _q(self, positions):
        if not isinstance(positions, dict) or set(positions) != set(self.names):
            raise ValueError('Exactly the configured body joint names are required')
        result = np.array([_finite(positions[n], n) for n in self.names])
        return result

    @staticmethod
    def _gripper(value):
        value = _finite(value, 'gripper_percent')
        if not 0 <= value <= 100: raise ValueError('Gripper percentage must be in [0,100]')
        return value

    def _forward(self, q):
        if self.model_error: raise ManualUnavailable(self.model_error)
        values = dict(zip(self.mapping, q))
        transform = np.eye(4); frames = {self.model_base: transform.copy()}; origins = {}; axes = {}
        for joint in self.chain:
            transform = transform @ joint['origin']
            if joint['kind'] != 'fixed':
                origins[joint['name']] = transform[:3, 3].copy()
                axes[joint['name']] = transform[:3, :3] @ joint['axis']
                rotation = np.eye(4); rotation[:3, :3] = _rotation(joint['axis'], values[joint['name']])
                transform = transform @ rotation
            frames[joint['child']] = transform.copy()
        return transform, origins, axes, frames

    def fk(self, positions_rad, gripper_percent):
        """Report geometry for every finite named body pose."""
        q = self._q(positions_rad); g = self._gripper(gripper_percent)
        pose, origins, axes, frames = self._forward(q)
        semantics, azimuth = _semantics(pose[:3, :3] @ self.operator_rotation)
        points = []; segments = []; gripper_visual = None; previous_name = 'base'; previous = np.zeros(3)
        reverse = dict(zip(self.mapping, self.names))
        for joint in self.chain:
            if joint['kind'] == 'fixed': continue
            name = reverse[joint['name']]; point = origins[joint['name']]
            points.append({'name': name, 'urdf_name': joint['name'], 'role': 'joint', 'position_m': point.tolist(), 'axis_base': axes[joint['name']].tolist(), 'position_rad': float(q[self.names.index(name)])})
            segments.append({'from': previous_name, 'to': name, 'start_m': previous.tolist(), 'end_m': point.tolist()})
            previous_name, previous = name, point
        segments.append({'from': previous_name, 'to': 'tcp', 'start_m': previous.tolist(), 'end_m': pose[:3, 3].tolist()})
        if self.branch and self.branch['parent'] in frames:
            parent = frames[self.branch['parent']]; jaw = parent @ self.branch['origin']
            name = self.config.get('gripper', {}).get('name', 'gripper')
            points.append({'name': name, 'urdf_name': self.branch['name'], 'role': 'gripper', 'position_m': jaw[:3, 3].tolist(), 'axis_base': (jaw[:3, :3] @ self.branch['axis']).tolist(), 'position_rad': None})
            segments.append({'from': previous_name, 'to': name, 'start_m': parent[:3, 3].tolist(), 'end_m': jaw[:3, 3].tolist()})
            # Match the earlier SO-101 viewer: one fixed finger (ending at TCP),
            # one moving finger extending 65 mm along the jaw link's local -Y.
            # This is normalized opening visualization, not an aperture calibration.
            bounds = self.branch['bounds']
            if self.branch['child'] == 'moving_jaw_so101_v1_link' and bounds and bounds[1] > bounds[0]:
                angle = bounds[0] + g / 100. * (bounds[1] - bounds[0])
                tip = jaw[:3, 3] + jaw[:3, :3] @ _rotation(self.branch['axis'], angle) @ np.array([0., -.065, 0.])
                segments[-2]['role'] = 'fixed_finger'
                segments.append({'from': name, 'to': name + '_tip', 'role': 'moving_finger',
                                 'start_m': jaw[:3, 3].tolist(), 'end_m': tip.tolist()})
                gripper_visual = {'kind': 'hinged', 'opening_percent': g, 'visual_angle_rad': angle,
                                  'mapping': 'normalized_percent_to_urdf_limits', 'schematic': True}
        return {'frame_id': self.frame, 'model_base_frame': self.model_base, 'tcp_frame': self.tcp,
                'units': {'position': 'm', 'joint': 'rad', 'gripper': 'percent'},
                'position_m': pose[:3, 3].tolist(), 'rotation_matrix': pose[:3, :3].tolist(),
                'elevation_rad': float(semantics[0]), 'tool_roll_rad': float(semantics[1]), 'azimuth_rad': azimuth,
                'joints': points, 'segments': segments, 'tcp': {'position_m': pose[:3, 3].tolist(), 'frame': self.tcp}, 'gripper_percent': g,
                'gripper_visual': gripper_visual}

    def jacobian_diagnostics(self, positions):
        """Geometric diagnostics and dimensionless arm conditioning.

        Columns are [axis x (TCP - joint origin); axis], expressed in the URDF
        model base. All configured body joints participate; the separate jaw
        does not. This unweighted matrix is not the IK task residual Jacobian.
        Diagnostics do not change any solver constraint or execution gate.
        """
        q = self._q(positions)
        pose, origins, axes, _ = self._forward(q)
        jacobian = np.empty((6, len(q)))
        for column, name in enumerate(self.mapping):
            axis = axes[name]
            jacobian[:3, column] = np.cross(axis, pose[:3, 3]-origins[name])
            jacobian[3:, column] = axis
        if not np.all(np.isfinite(jacobian)):
            raise ValueError('Geometric Jacobian is nonfinite')
        try:
            singular = np.linalg.svd(jacobian, compute_uv=False)
        except np.linalg.LinAlgError as exc:
            raise ValueError('Jacobian singular-value decomposition failed') from exc
        largest = float(singular[0]) if len(singular) else 0.
        tolerance = float(np.finfo(jacobian.dtype).eps * max(jacobian.shape) * largest)
        rank = int(np.count_nonzero(singular > tolerance))
        length = self.conditioning_length_m
        conditioning = {'available': False, 'reciprocal_condition': None,
            'characteristic_length_m': length, 'matrix_kind': 'scaled_geometric',
            'scope': 'full_arm', 'reason': 'Model has no nonzero translational chain length'}
        if math.isfinite(length) and length > 0.:
            scaled = jacobian.copy()
            scaled[:3] /= length
            values = np.linalg.svd(scaled, compute_uv=False)
            # Thin SVD is intentional for the five-axis arm: do not append the
            # structurally unavailable sixth Cartesian direction as a zero.
            maximum = float(values[0]) if len(values) else 0.
            minimum = float(values[-1]) if len(values) else 0.
            cutoff = float(np.finfo(float).eps * max(scaled.shape) * maximum)
            ratio = minimum / maximum if maximum > 0. and minimum > cutoff else 0.
            conditioning.update(available=True, reciprocal_condition=float(np.clip(ratio,0.,1.)),
                singular_values=[float(value) for value in values], reason='')
        return {'matrix_kind': 'geometric', 'task_label': 'Arm Jacobian',
                'reference_frame': self.model_base, 'rows': 6, 'columns': len(q),
                'joint_names': list(self.names), 'rank': rank, 'max_rank': min(jacobian.shape),
                'singular_values': [float(value) for value in singular], 'rank_tolerance': tolerance,
                'conditioning': conditioning}

    def describe(self):
        poses = {}
        for name in ('home', 'fold'):
            try:
                self._pose(name)
                poses[name] = {'available': True}
            except ValueError as exc: poses[name] = {'available': False, 'reason': str(exc)}
        try: reference = self._roll_reference()
        except ValueError: reference = None
        return {'available': self.model_error is None, 'reason': self.model_error,
                'frame_id': self.frame, 'model_base_frame': self.model_base, 'tcp_frame': self.tcp,
                'model_sha256': self.model_sha256, 'joint_names': list(self.names),
                'keymap': copy.deepcopy(KEYS), 'default_level': 3, 'max_dt_s': .1,
                'speed_levels': [{'level': i+1, 'translation_m_s': s[0], 'orientation_rad_s': s[1], 'gripper_percent_s': s[2]} for i, s in enumerate(SPEEDS)],
                'position_only': True, 'analog_rates': True, 'poses': poses, 'presets': {name: value['available'] for name, value in poses.items()}, 'wrist_roll_reference_rad': reference,
                'roll_presets_rad': None if reference is None else {'minus_90': reference-math.pi/2, 'reference': reference, 'plus_90': reference+math.pi/2},
                'geometry_is_collision_check': False,'joint_angle_authority':'servo',
                'application_angle_limits':False}

    def prepare_jog(self):
        if self.model_error:
            raise ManualUnavailable(self.model_error)
        with self._solver_lock:
            if self.jog_backend is None:
                from .jog_kinematics import JogKinematics
                try:
                    self.jog_backend = JogKinematics(self)
                except (ImportError, RuntimeError) as exc:
                    raise ManualUnavailable(f'LeRobot jog backend unavailable: {exc}') from exc

    def step(self, positions_rad, gripper_percent, *, keys, dt_s, level=3, shift=False, position_only=False, reference_positions=None, reference_gripper=None):
        """Return one incremental target; caller rejects obsolete responses before DIRECT."""
        rates = keyboard_rates(keys, level, shift, self.settings.get('control_frame_yaw_rad', 0.), position_only)
        return self._step_rates(positions_rad, gripper_percent, rates=rates, dt_s=dt_s,
                               position_only=position_only, reference_positions=reference_positions,
                               reference_gripper=reference_gripper)

    def analog_step(self, positions_rad, gripper_percent, *, translation, elevation, roll,
                    speed_scale, gripper_target=None, dt_s, level=3,
                    reference_positions=None, reference_gripper=None):
        """Integrate normalized analog rates, independently tracking an absolute jaw target.

        Individual axes must lie in [-1,1]; translation diagonals are norm-capped
        to one without magnifying subunit input. speed_scale affects body rates,
        never the gripper. Results still require the existing DIRECT grant.
        """
        rates = self.analog_rates(translation, elevation, roll, speed_scale, level)
        if gripper_target is not None:
            gripper_target = self._gripper(gripper_target)
        return self._step_rates(positions_rad, gripper_percent, rates=rates, dt_s=dt_s,
                               position_only=False, reference_positions=reference_positions,
                               reference_gripper=reference_gripper, gripper_target=gripper_target)

    def analog_rates(self, translation, elevation, roll, speed_scale, level):
        if type(level) is not int or not 1 <= level <= 5:
            raise ValueError('level must be an integer in [1,5]')
        values = np.asarray(translation, dtype=object)
        if values.shape != (3,):
            raise ValueError('translation must contain three normalized axes')
        direction = np.array([_finite(value, 'translation axis') for value in values])
        elevation = _finite(elevation, 'elevation')
        roll = _finite(roll, 'roll')
        speed_scale = _finite(speed_scale, 'speed_scale')
        if np.any(np.abs(direction) > 1) or abs(elevation) > 1 or abs(roll) > 1:
            raise ValueError('Analog axes must be in [-1,1]')
        if not 0 <= speed_scale <= 1:
            raise ValueError('speed_scale must be in [0,1]')
        magnitude = np.linalg.norm(direction)
        if magnitude > 1:
            direction /= magnitude
        yaw = _finite(self.settings.get('control_frame_yaw_rad', 0.), 'yaw_rad')
        direction = _rotation(np.array([0., 0., 1.]), yaw) @ direction
        linear, angular, _ = SPEEDS[level-1]
        return {'translation_m_s': (direction*linear*speed_scale).tolist(),
                 'elevation_rad_s': elevation*angular*speed_scale,
                 'roll_rad_s': roll*angular*speed_scale,
                 'gripper_percent_s': 0., 'level': level}

    def jog_rates(self, intent):
        rates = self.analog_rates(intent['translation'], intent['elevation'], intent['roll'], 1., intent['level'])
        if type(intent['position_only']) is not bool:
            raise ValueError('position_only must be boolean')
        if intent['position_only']:
            rates['elevation_rad_s'] = rates['roll_rad_s'] = 0.
        jaw_rate = _finite(intent['gripper_rate'], 'gripper_rate')
        if not -1 <= jaw_rate <= 1:
            raise ValueError('Normalized gripper rate outside [-1, 1]')
        rates['gripper_percent_s'] = jaw_rate * SPEEDS[intent['level']-1][2]
        target = intent['gripper_target']
        if target is not None:
            target = self._gripper(target)
            if jaw_rate:
                raise ValueError('Choose one gripper command')
        return rates, intent['position_only'], target

    def _step_rates(self, positions_rad, gripper_percent, *, rates, dt_s, position_only,
                    reference_positions=None, reference_gripper=None, gripper_target=None, anchor=None, include_geometry=True):
        """Common bounded integration/IK; producer-specific mapping is already complete."""
        self._q(positions_rad)  # Retain measured-state validation even when an admitted reference is supplied.
        self._gripper(gripper_percent)
        q0 = self._q(reference_positions if reference_positions is not None else positions_rad)
        g0 = self._gripper(reference_gripper if reference_gripper is not None else gripper_percent)
        dt = _finite(dt_s, 'dt_s')
        if dt < 0: raise ValueError('dt_s must be nonnegative')
        dt = min(dt, .1)  # No accumulated catch-up movement after a delayed browser tick.
        translation = np.array(rates['translation_m_s'])*dt
        angles = np.array([rates['elevation_rad_s'], rates['roll_rad_s']])*dt
        gripper_velocity = self.config.get('gripper', {}).get('velocity_percent_s', 100.)
        if gripper_target is None:
            jaw_velocity = min(abs(rates['gripper_percent_s']), gripper_velocity)
            jaw_delta = math.copysign(jaw_velocity*dt, rates['gripper_percent_s']) if rates['gripper_percent_s'] else 0.
        else:
            jaw_delta = float(np.clip(gripper_target-g0, -gripper_velocity*dt, gripper_velocity*dt))
        jaw = float(np.clip(g0+jaw_delta, 0., 100.))
        result = q0
        residuals = {'position_m': 0., 'orientation_rad': 0.}
        fraction = 1.
        reason = ''
        status = 'OK' if jaw_delta else 'IDLE'
        if np.any(translation) or np.any(angles):
            with self._solver_lock:
                self.prepare_jog()
                candidate, start, reached, desired = self.jog_backend.advance(
                    q0, translation, angles, dt, position_only, anchor)
            if np.any(np.abs(candidate-q0) > self.velocity*dt+1e-8):
                raise ValueError('LeRobot jog exceeded configured joint velocity')
            before = start[:3, :3] @ self.operator_rotation
            chart = np.eye(3)[np.argmin(np.abs(before[:, 2]))]
            current, _ = _semantics(before, chart)
            actual, _ = _semantics(reached[:3, :3] @ self.operator_rotation, chart)
            goal, _ = _semantics(desired[:3, :3] @ self.operator_rotation, chart)
            active_angles = 0 if position_only else 2 if angles[1] else 1
            orientation_error = _wrap(actual[:active_angles]-goal[:active_angles])
            residuals = {'position_m': float(np.linalg.norm(reached[:3,3]-desired[:3,3])),
                         'orientation_rad': float(np.max(np.abs(orientation_error), initial=0.))}
            # Project the realized motion onto the requested task direction.
            # A nearly neutral roll axis must not veto a useful translation.
            length = self.conditioning_length_m
            request = np.r_[translation, length*angles[:active_angles]]
            actual_delta = np.r_[reached[:3,3]-start[:3,3], length*_wrap(actual[:active_angles]-current[:active_angles])]
            magnitude2 = float(request @ request)
            achieved = float(actual_delta @ request) / magnitude2 if magnitude2 else 0.
            # Sub-resolution movement cannot honestly be reported as progress.
            tiny = (np.linalg.norm(translation)*achieved <= 1e-6 and
                    np.linalg.norm(angles)*achieved <= 1e-5)
            if achieved <= 0 or (tiny and achieved < .01):
                status = 'PARTIAL' if jaw_delta else 'BLOCKED'
                fraction = 0.
                residuals['position_m'] = float(np.linalg.norm(start[:3,3]-desired[:3,3]))
                residuals['orientation_rad'] = float(np.max(np.abs(_wrap(current[:active_angles]-goal[:active_angles])), initial=0.))
                reason = 'No converged movement in the requested direction'
            else:
                status = 'OK'
                fraction = 1. if achieved >= .99 else float(achieved)
                result = candidate
        positions = dict(zip(self.names, map(float, result)))
        target = {'positions': positions, 'gripper': jaw, 'gripper_unit': 'percent'}
        response = {'status': status, 'reason': reason, 'target': target, 'residuals': residuals,
                    'step_fraction': fraction, 'dt_s': dt, 'level': rates['level']}
        if include_geometry:
            response['geometry'] = self.fk(positions, jaw)
        return response

    def _pose(self, name):
        if name not in ('home', 'fold'): raise ValueError('Unknown named pose')
        pose = self.settings.get('poses', {}).get(name)
        if not isinstance(pose, dict) or not isinstance(pose.get('positions'), dict):
            raise ManualUnavailable('No configured '+name+' pose')
        self._q(pose['positions'])
        if pose.get('gripper') not in (None, 'preserve'): self._gripper(pose['gripper'])
        return pose

    def preset(self, name, positions_rad, gripper_percent, preserve_wrist=False):
        self._q(positions_rad); jaw = self._gripper(gripper_percent)
        if type(preserve_wrist) is not bool: raise ValueError('preserve_wrist must be boolean')
        pose = self._pose(name); positions = dict(pose['positions'])
        wrist = self.settings.get('wrist_roll_joint', 'wrist_roll')
        if preserve_wrist and wrist in positions: positions[wrist] = positions_rad[wrist]
        self._q(positions)
        if pose.get('gripper') not in (None, 'preserve'): jaw = self._gripper(pose['gripper'])
        target = {'positions': positions, 'gripper': jaw, 'gripper_unit': 'percent'}
        return {'status': 'OK', 'name': name, 'target': target,
                'geometry': self.fk(positions, jaw) if self.model_error is None else None}

    def _roll_reference(self):
        value = self.settings.get('wrist_roll_reference_rad')
        if value is None:
            wrist = self.settings.get('wrist_roll_joint', 'wrist_roll')
            value = self._pose('home')['positions'].get(wrist)
        if value is None: raise ManualUnavailable('No configured wrist-roll reference')
        return _finite(value, 'wrist_roll_reference_rad')

    def roll_preset(self, name, positions_rad, gripper_percent):
        offsets = {'minus_90': -math.pi/2, 'reference': 0., 'plus_90': math.pi/2}
        if name not in offsets: raise ValueError('Unknown roll preset')
        positions = dict(positions_rad); self._q(positions)
        wrist = self.settings.get('wrist_roll_joint', 'wrist_roll')
        if wrist not in positions: raise ManualUnavailable('Configured wrist-roll joint is absent')
        positions[wrist] = self._roll_reference()+offsets[name]
        self._q(positions); jaw = self._gripper(gripper_percent)
        return {'status': 'OK', 'name': name, 'target': {'positions': positions, 'gripper': jaw, 'gripper_unit': 'percent'},
                'geometry': self.fk(positions, jaw) if self.model_error is None else None}
