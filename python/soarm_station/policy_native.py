"""Native preparation and DESCRIBE-only remote inspection, never motor dispatch.

Call these blocking helpers on the existing bounded native work queue. Importing
this module does not import LeRobot, torch, a checkpoint, or any transport.
"""
from __future__ import annotations

from copy import deepcopy
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat

from .policy import ModelOutcome, NativePolicy
from .policy_common import PolicyError, relative_path, revision

_MAX_FILES = 10000
_MAX_DEPTH = 12
_MAX_METADATA = 2 * 1024 * 1024
_MAX_PIN_BYTES = 32 * 1024 * 1024
_EXCLUDED = frozenset({'.station-uploads', '.git', '__pycache__'})


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _inventory(root):
    files, directories = {}, [(root, 0)]
    entries = 0
    while directories:
        path, depth = directories.pop()
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise PolicyError('ARTIFACT_ESCAPE', 'Artifact directory contains a symlink or escapes its root', 409)
        with os.scandir(path) as listing:
            for entry in listing:
                entries += 1
                if entries > 50000:
                    raise PolicyError('ARTIFACT_LIMIT', 'Artifact inventory exceeds its entry limit', 413)
                if entry.name in _EXCLUDED:
                    continue
                if entry.is_symlink():
                    raise PolicyError('ARTIFACT_ESCAPE', 'Artifact inventory contains a symlink', 409)
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    if depth >= _MAX_DEPTH:
                        raise PolicyError('ARTIFACT_LIMIT', 'Artifact inventory exceeds its depth limit', 413)
                    directories.append((Path(entry.path), depth + 1))
                elif stat.S_ISREG(info.st_mode):
                    files[Path(entry.path).relative_to(root).as_posix()] = _stamp(info)
                    if len(files) > _MAX_FILES:
                        raise PolicyError('ARTIFACT_LIMIT', 'Artifact inventory exceeds its file limit', 413)
                else:
                    raise PolicyError('ARTIFACT_SPECIAL_FILE', 'Artifact contains a special file', 409)
    return files


def _read_metadata(path, *, yaml_file=False):
    with path.open('rb') as stream:
        data = stream.read(_MAX_METADATA + 1)
    if len(data) > _MAX_METADATA:
        raise PolicyError('ARTIFACT_METADATA_LIMIT', 'Artifact metadata exceeds its size limit', 413)
    if yaml_file:
        import yaml
        value = yaml.safe_load(data)
    else:
        value = json.loads(data)
    if not isinstance(value, dict):
        raise PolicyError('INVALID_ARTIFACT_METADATA', 'Artifact metadata must contain a mapping')
    json.dumps(value, allow_nan=False)
    return value


def _pinned_inventory(artifact):
    """Expand only a startup-owned compact pin, never a GUI-selected file."""
    value = artifact.get('inventory_path')
    if not isinstance(value, str) or not value or len(value) > 4096 or '\x00' in value:
        raise PolicyError('INVALID_ARTIFACT_PIN', 'A startup-owned artifact inventory pin is required')
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        raise PolicyError('INVALID_ARTIFACT_PIN', 'Artifact inventory pin must be a real absolute file')
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise PolicyError('INVALID_ARTIFACT_PIN', 'Artifact inventory pin must be a regular file')
            if before.st_size > _MAX_PIN_BYTES:
                raise PolicyError('ARTIFACT_PIN_LIMIT', 'Artifact inventory pin exceeds its size limit', 413)
            data = stream.read(_MAX_PIN_BYTES + 1)
            if len(data) > _MAX_PIN_BYTES:
                raise PolicyError('ARTIFACT_PIN_LIMIT', 'Artifact inventory pin exceeds its size limit', 413)
            if _stamp(before) != _stamp(os.fstat(stream.fileno())):
                raise PolicyError('ARTIFACT_PIN_CONFLICT', 'Artifact inventory pin changed while being read', 409)
        if _stamp(path.stat(follow_symlinks=False)) != _stamp(before):
            raise PolicyError('ARTIFACT_PIN_CONFLICT', 'Artifact inventory pin was replaced while being read', 409)
        pinned = json.loads(data)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PolicyError('INVALID_ARTIFACT_PIN', 'Artifact inventory pin is unreadable or invalid') from error
    if not isinstance(pinned, dict) or any(pinned.get(key) != artifact.get(key)
                                         for key in ('root', 'checkpoint_path', 'artifact_revision')):
        raise PolicyError('ARTIFACT_PIN_CONFLICT', 'Artifact inventory pin differs from the compact selected reference', 409)
    return pinned.get('files')


def verify_artifact(artifact, *, _with_inventory=False):
    """Verify the pinned, contained inventory and return the local checkpoint path.

    The inventory has the same exclusions as catalog discovery. Every included
    file, including optional metadata/processor state, must match exactly.
    """
    if not isinstance(artifact, dict):
        raise PolicyError('INVALID_ARTIFACT', 'Managed preparation requires a pinned artifact descriptor')
    root_value, checkpoint_value = artifact.get('root'), artifact.get('checkpoint_path')
    if not isinstance(root_value, str) or not isinstance(checkpoint_value, str):
        raise PolicyError('INVALID_ARTIFACT', 'Artifact root and checkpoint_path are required')
    root = Path(root_value)
    checkpoint = Path(checkpoint_value)
    if not root.is_absolute() or not checkpoint.is_absolute() or root.is_symlink() or checkpoint.is_symlink():
        raise PolicyError('ARTIFACT_ESCAPE', 'Artifact paths must be real absolute directories', 409)
    root = root.resolve(strict=True)
    checkpoint = checkpoint.resolve(strict=True)
    if not root.is_dir() or not checkpoint.is_dir() or not checkpoint.is_relative_to(root):
        raise PolicyError('ARTIFACT_ESCAPE', 'Checkpoint path must remain inside its artifact root', 409)
    expected = artifact.get('files')
    if expected is None:
        expected = _pinned_inventory(artifact)
    if not isinstance(expected, list) or not expected or len(expected) > _MAX_FILES:
        raise PolicyError('INVALID_ARTIFACT', 'A bounded nonempty artifact file inventory is required')
    normalized, names = [], set()
    for item in expected:
        if not isinstance(item, dict):
            raise PolicyError('INVALID_ARTIFACT', 'Artifact inventory entries must be mappings')
        name = relative_path(item.get('path'))
        if name in names or name == '.':
            raise PolicyError('INVALID_ARTIFACT', 'Artifact inventory contains a duplicate or invalid file path')
        names.add(name)
        size, digest = item.get('size'), item.get('sha256')
        if type(size) is not int or size < 0 or not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest):
            raise PolicyError('INVALID_ARTIFACT', 'Artifact inventory requires exact byte sizes and SHA256 hashes')
        normalized.append({'path': name, 'size': size, 'sha256': digest})
    normalized.sort(key=lambda item: item['path'])
    if revision(normalized) != artifact.get('artifact_revision'):
        raise PolicyError('ARTIFACT_REVISION_CONFLICT', 'Pinned artifact inventory revision is invalid', 409)
    try:
        before = _inventory(root)
        if set(before) != names:
            raise PolicyError('ARTIFACT_INVENTORY_CONFLICT', 'Artifact file inventory changed after selection', 409)
        for item in normalized:
            path = root / item['path']
            expected_stamp = before[item['path']]
            if expected_stamp[2] != item['size']:
                raise PolicyError('ARTIFACT_DIGEST_MISMATCH', 'Artifact file size changed after selection', 409)
            digest = hashlib.sha256()
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(fd, 'rb') as stream:
                if _stamp(os.fstat(stream.fileno())) != expected_stamp:
                    raise PolicyError('ARTIFACT_CHANGING', 'Artifact changed while being verified', 409)
                while data := stream.read(1024 * 1024):
                    digest.update(data)
                if _stamp(os.fstat(stream.fileno())) != expected_stamp:
                    raise PolicyError('ARTIFACT_CHANGING', 'Artifact changed while being verified', 409)
            if digest.hexdigest() != item['sha256']:
                raise PolicyError('ARTIFACT_DIGEST_MISMATCH', 'Artifact file digest changed after selection', 409)
        if before != _inventory(root):
            raise PolicyError('ARTIFACT_CHANGING', 'Artifact inventory changed while being verified', 409)
        config_relative = (checkpoint / 'config.json').relative_to(root).as_posix()
        if config_relative not in names:
            raise PolicyError('INVALID_ARTIFACT', 'Pinned checkpoint config.json is missing')
        config = _read_metadata(checkpoint / 'config.json')
        if not isinstance(config.get('type'), str) or not config['type']:
            raise PolicyError('INVALID_ARTIFACT', 'Checkpoint config.type is required')
        return (checkpoint, before) if _with_inventory else checkpoint
    except OSError as error:
        raise PolicyError('ARTIFACT_UNAVAILABLE', 'Artifact became unavailable during verification', 409) from error


def _feature_contract(profile, config, artifact):
    """Validate dimensions and known trained order/units; bindings may rename devices."""
    for key in ('state_features', 'action_features'):
        features = profile.get(key)
        if not isinstance(features, list) or not features or len(features) > 256:
            raise PolicyError('MODEL_CONTRACT_MISSING', 'Explicit ordered ' + key + ' are required')
        names = []
        for item in features:
            if not isinstance(item, dict) or any(not isinstance(item.get(field), str) or not item[field]
                                                 for field in ('name', 'joint', 'unit')):
                raise PolicyError('MODEL_CONTRACT_INVALID', 'Each feature needs a name, joint and unit')
            if item['unit'] not in ('degree', 'radian', 'percent') or (item['joint'] == 'gripper' and item['unit'] != 'percent') or (item['joint'] != 'gripper' and item['unit'] == 'percent'):
                raise PolicyError('MODEL_CONTRACT_INVALID', 'Feature units are incompatible with the native adapter')
            names.append(item['name'])
        if len(names) != len(set(names)):
            raise PolicyError('MODEL_CONTRACT_INVALID', 'Feature names must be unique in declared order')
    if not any(item['joint'] == 'gripper' for item in profile['action_features']):
        raise PolicyError('MODEL_CONTRACT_INVALID', 'The native adapter requires an explicit percent gripper action')
    fps = profile.get('action_fps')
    if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not math.isfinite(fps) or fps <= 0:
        raise PolicyError('MODEL_CONTRACT_INVALID', 'A finite positive action_fps is required')
    for container, name, mappings in (('input_features', 'observation.state', 'state_features'),
                                      ('output_features', 'action', 'action_features')):
        features = config.get(container, {})
        feature = features.get(name) if isinstance(features, dict) else None
        shape = feature.get('shape') if isinstance(feature, dict) else None
        if not isinstance(shape, list) or len(shape) != 1 or type(shape[0]) is not int or shape[0] != len(profile[mappings]):
            raise PolicyError('MODEL_DIMENSION_MISMATCH', 'Checkpoint ' + name + ' shape differs from the ordered profile mapping')
        declared_names = feature.get('names')
        if declared_names is not None and declared_names != [item['name'] for item in profile[mappings]]:
            raise PolicyError('MODEL_FEATURE_ORDER_MISMATCH', 'Checkpoint feature order differs from the profile mapping')
    cameras = profile.get('cameras', {})
    if not isinstance(cameras, dict) or any(not isinstance(key, str) or not isinstance(value, str) or not value for key, value in cameras.items()):
        raise PolicyError('MODEL_CAMERA_CONTRACT_INVALID', 'Camera bindings must map trained keys to runtime camera IDs')
    root = Path(artifact['root'])
    trained = {}
    registration = root / 'station-policy.yaml'
    if registration.is_file():
        sidecar = _read_metadata(registration, yaml_file=True)
        trained = sidecar.get('contract', sidecar)
        if not isinstance(trained, dict):
            raise PolicyError('MODEL_CONTRACT_INVALID', 'Registration contract must contain a mapping')
    manifest_path = root / 'manifest.json'
    if manifest_path.is_file():
        manifest = _read_metadata(manifest_path)
        if manifest.get('schema_version') != 1 or manifest.get('lerobot_version') != '0.7.0':
            raise PolicyError('INCOMPATIBLE_MANIFEST', 'Managed exported bundle manifest is incompatible')
        trained = {**trained, **manifest}
    _camera_contract(profile, config, trained)
    for key in ('state_features', 'action_features', 'action_fps', 'calibration_revision', 'language_conditioned'):
        if config.get(key) is not None:
            trained[key] = config[key]
    if config.get('fps') is not None:
        trained['action_fps'] = config['fps']
    for key in ('state_features', 'action_features'):
        if key in trained:
            known = trained[key]
            if not isinstance(known, list) or any(not isinstance(item, dict) for item in known):
                raise PolicyError('MODEL_CONTRACT_INVALID', 'Invalid trained feature declaration')
            # Runtime joint resource names are bindings; model names, order and units are immutable.
            if [(item.get('name'), item.get('unit')) for item in known] != [(item['name'], item['unit']) for item in profile[key]]:
                raise PolicyError('MODEL_FEATURE_ORDER_MISMATCH', 'Trained feature names/order/units differ from the profile')
    for key in ('action_fps', 'calibration_revision', 'language_conditioned'):
        if key in trained and trained[key] != profile.get(key):
            raise PolicyError('MODEL_CONTRACT_MISMATCH', 'Trained ' + key + ' differs from the profile')


def _camera_contract(profile, config, trained):
    """Admit only pinned masked slots with a verified native missing-image path.

    SmolVLA.prepare_images and PI05._preprocess_images construct their own absent
    views and false masks. Station supplies no substitute camera image. Their
    saved normalizer skips absent observation keys in LeRobot 0.7.0.
    """
    image_keys = {key for key, value in config.get('input_features', {}).items()
                  if isinstance(value, dict) and (str(value.get('type', '')).upper() == 'VISUAL' or key.startswith('observation.images.'))}
    kind = config.get('type')
    native_masks_supported = kind in ('smolvla', 'pi05')
    count = config.get('empty_cameras', 0)
    if type(count) is not int or not 0 <= count <= 64:
        raise PolicyError('MODEL_MASK_CONTRACT_INVALID', 'Checkpoint empty_cameras must be a bounded nonnegative integer')
    generated = {f'observation.images.empty_camera_{index}' for index in range(count)} if native_masks_supported else set()
    # These exact generated slots come from native validate_features(), even when
    # an older saved config omitted them from serialized input_features.
    image_keys |= generated
    declarations = trained.get('camera_inputs', [])
    if not isinstance(declarations, list):
        raise PolicyError('MODEL_MASK_CONTRACT_INVALID', 'Pinned camera_inputs must be a list')
    flags = {}
    for declaration in declarations:
        if not isinstance(declaration, dict) or not isinstance(declaration.get('key'), str):
            raise PolicyError('MODEL_MASK_CONTRACT_INVALID', 'Pinned camera declarations require trained input keys')
        if 'masked' in declaration and type(declaration['masked']) is not bool:
            raise PolicyError('MODEL_MASK_CONTRACT_INVALID', 'Pinned camera masked flags must be boolean')
        if declaration['key'] in flags:
            raise PolicyError('MODEL_MASK_CONTRACT_INVALID', 'Pinned camera declarations contain duplicate keys')
        flags[declaration['key']] = declaration.get('masked', False)
    # Direct configuration declarations remain authoritative over registration.
    configured = config.get('camera_inputs', [])
    if not isinstance(configured, list):
        raise PolicyError('MODEL_MASK_CONTRACT_INVALID', 'Checkpoint camera_inputs must be a list')
    for declaration in configured:
        if not isinstance(declaration, dict) or not isinstance(declaration.get('key'), str) or type(declaration.get('masked', False)) is not bool:
            raise PolicyError('MODEL_MASK_CONTRACT_INVALID', 'Invalid checkpoint camera mask declaration')
        flags[declaration['key']] = declaration.get('masked', False)
    declared = {key for key, masked in flags.items() if masked}
    if declared - image_keys:
        raise PolicyError('MODEL_MASK_CONTRACT_INVALID', 'Pinned masked camera key is absent from the checkpoint configuration')
    masked = generated | declared
    supplied = profile.get('_masked_camera_inputs', [])
    if not isinstance(supplied, list) or any(not isinstance(key, str) for key in supplied) or len(supplied) != len(set(supplied)):
        raise PolicyError('MODEL_MASK_CONTRACT_INVALID', 'Compiled masked camera inputs must be unique trained keys')
    if set(supplied) != masked:
        raise PolicyError('MODEL_MASK_CONTRACT_MISMATCH', 'Compiled masked camera inputs differ from pinned artifact declarations')
    if masked and not native_masks_supported:
        raise PolicyError('MODEL_MASK_UNSUPPORTED', 'Native masked camera support has not been verified for this policy type')
    if kind == 'smolvla' and len(masked) > count:
        raise PolicyError('MODEL_MASK_UNSUPPORTED', 'Masked inputs exceed the checkpoint native empty_cameras budget')
    cameras = profile.get('cameras', {})
    if set(cameras) & masked:
        raise PolicyError('MODEL_MASK_BINDING_CONFLICT', 'A trained masked slot cannot bind a real runtime camera')
    if set(cameras) != image_keys - masked:
        raise PolicyError('MODEL_CAMERA_KEYS_MISMATCH', 'Camera bindings must preserve every required checkpoint image input key')
    if masked and not cameras:
        raise PolicyError('MODEL_MASK_CONTRACT_INVALID', 'Native masked camera support requires at least one real image input')
    exported = trained.get('cameras')
    if isinstance(exported, dict) and (set(exported) - masked != set(cameras) or set(exported) - image_keys):
        raise PolicyError('MODEL_CAMERA_KEYS_MISMATCH', 'Exported trained camera keys differ from the profile')


def _device(requested, torch):
    if requested not in ('auto', 'cpu', 'cuda', 'mps'):
        raise PolicyError('INVALID_DEVICE', 'Device must be cpu, cuda, mps or auto')
    cuda = bool(getattr(torch, 'cuda', None) and torch.cuda.is_available())
    backends = getattr(torch, 'backends', None)
    mps_backend = getattr(backends, 'mps', None)
    mps = bool(mps_backend and mps_backend.is_available())
    if requested == 'auto':
        return 'cuda' if cuda else 'mps' if mps else 'cpu'
    if (requested == 'cuda' and not cuda) or (requested == 'mps' and not mps):
        raise PolicyError('DEVICE_UNAVAILABLE', 'Requested native device is not available: ' + requested, 503)
    return requested


class _ManagedLocalPolicy(NativePolicy):
    def pause(self):
        self._prepared_paused = True
        self.engine.pause()
        if hasattr(self, 'preparation_details'):
            self.preparation_details['paused'] = True

    def reset_task(self, instruction, token):
        super().reset_task(instruction, token)
        self._prepared_paused = False
        self.preparation_details['paused'] = False

    def gate(self, outcome=None):
        if self._prepared_paused:
            return ModelOutcome('WAIT', session=self, reason='Prepared model is paused pending an authorized operation')
        return super().gate(outcome)

    def poll(self, obs, capture_time):
        if self._prepared_paused:
            return self.gate()
        return super().poll(obs, capture_time)

    def _observe(self, obs, capture_time):
        if self.chunk_capture is None or self.policy.count_queued_actions() == 0:
            self.chunk_capture = capture_time

    def _action_source(self, capture_time):
        return self.chunk_capture


def load_managed_local(profile):
    """Load the pinned checkpoint/processors and return a paused native session."""
    artifact = deepcopy(profile.get('_artifact'))
    checkpoint, verified_inventory = verify_artifact(artifact, _with_inventory=True)
    config = _read_metadata(checkpoint / 'config.json')
    _feature_contract(profile, config, artifact)
    # Match the native CLI: register packages already installed in this
    # environment before configuration/class lookup. No checkpoint code is used.
    from lerobot.utils.import_utils import register_third_party_plugins
    register_third_party_plugins()
    from lerobot.configs.policies import PreTrainedConfig
    from lerobot.policies.factory import get_policy_class, make_pre_post_processors
    from lerobot.inference import SyncInferenceEngine
    import torch
    cfg = PreTrainedConfig.from_pretrained(checkpoint)
    if cfg.type != config['type']:
        raise PolicyError('MODEL_CATEGORY_MISMATCH', 'Native checkpoint category differs from its pinned config')
    cfg.device = _device(profile.get('device', 'cpu'), torch)
    session = _ManagedLocalPolicy()
    session._initialize(deepcopy(profile))
    session._prepared_paused = True
    session.policy = get_policy_class(cfg.type).from_pretrained(checkpoint, config=cfg).to(cfg.device).eval()
    engine = None
    try:
        pre, post = make_pre_post_processors(cfg, pretrained_path=checkpoint,
                                             preprocessor_overrides={'device_processor': {'device': cfg.device}})
        engine = SyncInferenceEngine(session.policy, pre, post, {}, [item['name'] for item in profile['action_features']],
                                     task='', device=cfg.device, robot_type=profile.get('robot_type', 'soarm_station_multi'))
        session.engine = engine
        engine.reset()
        engine.start()
        session.pause()
        if verified_inventory != _inventory(Path(artifact['root'])):
            raise PolicyError('ARTIFACT_CHANGING', 'Artifact changed while native weights/processors were loading', 409)
        session.preparation_details = {'weights_loaded': True, 'processors_loaded': True,
                                       'warmup_performed': False, 'inference_test_performed': False,
                                       'paused': True, 'device': cfg.device, 'category': config['type'],
                                       'artifact_revision': artifact['artifact_revision']}
        return session
    except Exception:
        if engine is not None:
            engine.stop()
        raise


def _safe_scalar(value):
    if isinstance(value, Enum):
        value = value.value
    if value is None or type(value) is bool:
        return value
    if isinstance(value, str):
        return value[:2048]
    if type(value) is int or (type(value) is float and math.isfinite(value)):
        return value
    return None


def _safe_feature(value):
    if not isinstance(value, dict):
        return None
    result = {key: _safe_scalar(value[key]) for key in ('name', 'dtype', 'kind', 'semantics') if key in value}
    shape = value.get('shape')
    if isinstance(shape, (list, tuple)) and len(shape) <= 8 and all(type(item) is int and item > 0 for item in shape):
        result['shape'] = list(shape)
    names = value.get('names')
    if isinstance(names, (list, tuple)) and len(names) <= 256 and all(isinstance(item, str) for item in names):
        result['names'] = [item[:256] for item in names]
    return result


def _safe_descriptor(raw):
    if not isinstance(raw, dict):
        raise PolicyError('INVALID_REMOTE_DESCRIPTOR', 'Native discovery did not provide a descriptor mapping', 503)
    result = {key: _safe_scalar(raw[key]) for key in ('deployment', 'instance_id', 'artifact_identity',
              'semantics', 'ready', 'available', 'session_status') if key in raw}
    software = raw.get('software')
    if isinstance(software, dict) and isinstance(software.get('lerobot_version'), str):
        result['software'] = {'lerobot_version': software['lerobot_version'][:128]}
    capabilities = raw.get('capabilities')
    if isinstance(capabilities, dict):
        safe = {key: _safe_scalar(capabilities[key]) for key in ('prediction_steps', 'execution_steps',
                'action_interval', 'language', 'training_max_delay', 'rtc_horizon', 'current_observation_only',
                'retains_session_state', 'action_representation', 'model_action_dim') if key in capabilities}
        modes = capabilities.get('modes')
        if isinstance(modes, (list, tuple)) and len(modes) <= 16:
            safe['modes'] = [_safe_scalar(item) for item in modes]
        features = capabilities.get('features')
        if isinstance(features, (list, tuple)) and len(features) <= 256:
            safe['features'] = [item for feature in features if (item := _safe_feature(feature)) is not None]
        action = _safe_feature(capabilities.get('action_feature'))
        if action is not None:
            safe['action_feature'] = action
        result['capabilities'] = safe
    return result


def inspect_remote(execution, settings=None, connection=None):
    """DESCRIBE the native endpoint without session admission or control/inference.

    Only `connection` may supply the startup-owned Zenoh credentials configuration
    path. GUI settings/execution cannot supply arbitrary server or credential files.
    """
    if not isinstance(execution, dict) or execution.get('kind', 'remote') != 'remote':
        raise PolicyError('INVALID_REMOTE_EXECUTION', 'Remote execution settings are required')
    settings = settings or {}
    connection = connection or {}
    if not isinstance(settings, dict) or not isinstance(connection, dict):
        raise PolicyError('INVALID_REMOTE_EXECUTION', 'Remote settings and startup connection must be mappings')
    if 'zenoh_config_path' in execution or 'zenoh_config_path' in settings:
        raise PolicyError('INVALID_REMOTE_EXECUTION', 'Credentials paths must come from a startup-owned connection profile')
    if execution.get('connection_profile') and not connection:
        raise PolicyError('REMOTE_CONNECTION_UNAVAILABLE', 'The requested startup connection profile is unavailable', 503)
    values = {}
    for key in ('endpoint', 'deployment', 'expected_artifact'):
        value = execution.get(key)
        if not isinstance(value, str) or not value or len(value) > 2048:
            raise PolicyError('INVALID_REMOTE_EXECUTION', 'Remote ' + key + ' must be a nonempty bounded string')
        values[key] = value
    if execution.get('instance') is not None:
        if not isinstance(execution['instance'], str) or not execution['instance']:
            raise PolicyError('INVALID_REMOTE_EXECUTION', 'Remote instance must be a nonempty string')
        values['instance'] = execution['instance']
    # DESCRIBE sends no semantics/admission contract. The label only satisfies the
    # native configuration validator; advertised server semantics remain authoritative.
    values['semantics'] = connection.get('semantics', settings.get('semantics', 'station-discovery-only'))
    values['handshake_timeout_s'] = settings.get('handshake_timeout_s', connection.get('handshake_timeout_s', 10.0))
    for key in ('zenoh_config_path', 'zenoh_mode'):
        if key in connection:
            values[key] = connection[key]
    from lerobot.inference import RemoteInferenceConfig
    from lerobot.remote_inference import RemoteClient
    client = RemoteClient.connect(RemoteInferenceConfig(**values))
    try:
        if client.session_id:
            raise PolicyError('REMOTE_DISCOVERY_ADMITTED', 'Native discovery unexpectedly returned an admitted session', 503)
        # Access the actual instance attributes. No admit, control, or infer call.
        if client.capabilities is None:
            raise PolicyError('INVALID_REMOTE_DESCRIPTOR', 'Native discovery returned no capabilities', 503)
        return {'descriptor': _safe_descriptor(client.descriptor), 'admitted': False}
    finally:
        if not client.session_id:
            client.close()
        else:
            # An unexpected SDK violation must not issue a session control message.
            transport = getattr(client, 'transport', None)
            if transport is not None:
                transport.close()
