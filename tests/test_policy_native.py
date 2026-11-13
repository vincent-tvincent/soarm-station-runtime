"""Native adapters exercised with fake SDKs: no model, transport, or devices."""
from copy import deepcopy
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from soarm_station.policy import NativePolicy
from soarm_station.policy_common import PolicyError, revision
from soarm_station.policy_native import inspect_remote, load_managed_local, verify_artifact


class _Mode(Enum):
    CHUNK = 'chunk'


def _module(name, **attributes):
    module = ModuleType(name)
    module.__dict__.update(attributes)
    return module


class PolicyNativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve() / 'checkpoint'
        self.root.mkdir()
        self.config = {'type': 'custom_installed_type', 'input_features': {
            'observation.state': {'type': 'STATE', 'shape': [2]},
            'observation.images.forward': {'type': 'VISUAL', 'shape': [3, 240, 320]}},
            'output_features': {'action': {'type': 'ACTION', 'shape': [2]}}}
        (self.root / 'config.json').write_text(json.dumps(self.config))
        (self.root / 'model.safetensors').write_bytes(b'not loaded by test')
        (self.root / 'policy_preprocessor.json').write_text('{}')
        (self.root / 'policy_postprocessor.json').write_text('{}')
        self.features = [{'name': 'trained.shoulder', 'joint': 'runtime_shoulder', 'unit': 'degree'},
                         {'name': 'trained.gripper', 'joint': 'gripper', 'unit': 'percent'}]
        self.events = []

    def tearDown(self):
        self.temp.cleanup()

    def artifact(self):
        files = [{'path': path.relative_to(self.root).as_posix(), 'size': path.stat().st_size,
                  'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                 for path in sorted(self.root.rglob('*')) if path.is_file()]
        return {'root': str(self.root), 'checkpoint_path': str(self.root),
                'files': files, 'artifact_revision': revision(files)}

    def profile(self):
        return {'arm_id': 'arm1', 'state_features': deepcopy(self.features), 'action_features': deepcopy(self.features),
                'cameras': {'observation.images.forward': 'new_runtime_camera'}, 'action_fps': 30,
                'calibration_revision': 'measured', 'language_conditioned': False, 'device': 'auto',
                '_artifact': self.artifact()}

    def sdk(self, *, cuda=False, mps=False, fail_processors=False, admitted=False):
        events = self.events
        root = self.root
        class Config:
            @classmethod
            def from_pretrained(cls, path):
                events.append(('config', path))
                return SimpleNamespace(**json.loads((path / 'config.json').read_text()))
        class Policy:
            @classmethod
            def from_pretrained(cls, path, config):
                events.append(('weights', path, config.type))
                return cls()
            def to(self, device):
                events.append(('device', device))
                return self
            def eval(self):
                events.append('eval')
                return self
            def count_queued_actions(self):
                return 1
        def factory(category):
            events.append(('category', category))
            return Policy
        def processors(config, **kwargs):
            events.append(('processors', kwargs))
            if fail_processors:
                raise RuntimeError('processor state incompatible')
            return object(), object()
        class Engine:
            failed = False
            def __init__(self, policy, pre, post, dataset, ordered, **kwargs):
                events.append(('engine', ordered, kwargs))
                self.task = kwargs['task']
                self.runtime = SimpleNamespace(generation=1)
            def reset(self):
                events.append('reset')
            def start(self):
                events.append('start')
            def pause(self):
                events.append('pause')
            def resume(self):
                events.append('resume')
            def set_task(self, task):
                self.task = task
                events.append(('task', task))
            def stop(self):
                events.append('stop')
            def dispatch_allowed(self):
                return True
            def get_action(self, obs):
                raise AssertionError('Preparation must never infer')
        class Interpolator:
            def __init__(self, multiplier):
                self.multiplier = multiplier
            def reset(self):
                events.append('interpolator_reset')
        class RemoteConfig:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)
                events.append(('remote_config', deepcopy(kwargs)))
        raw = {'deployment': 'deployed', 'instance_id': 'instance-v1', 'artifact_identity': 'remote-artifact',
               'semantics': 'trained-degrees-percent', 'ready': True, 'available': True,
               'session_status': 'idle', 'software': {'lerobot_version': '0.7.0', 'token': 'DO_NOT_EXPOSE'},
               'session': {'id': 'PRIVATE_SESSION'}, 'auth': 'DO_NOT_EXPOSE', 'failure': 'PRIVATE_FAILURE',
               'capabilities': {'modes': [_Mode.CHUNK], 'prediction_steps': 16, 'execution_steps': 16,
                   'action_interval': 1 / 30, 'language': False, 'token': 'DO_NOT_EXPOSE',
                   'features': [{'name': 'observation.state', 'kind': 'tensor', 'dtype': 'float32', 'shape': [2],
                                 'names': ['trained.shoulder', 'trained.gripper'], 'semantics': 'degrees_percent', 'secret': 'DO_NOT_EXPOSE'},
                                {'name': 'observation.images.forward', 'kind': 'rgb', 'dtype': 'uint8', 'shape': [240, 320, 3]}],
                   'action_feature': {'name': 'action', 'kind': 'tensor', 'dtype': 'float32', 'shape': [2],
                                      'names': ['trained.shoulder', 'trained.gripper'], 'semantics': 'degrees_percent'}}}
        class Remote:
            @classmethod
            def connect(cls, config):
                events.append('describe')
                client = cls()
                client.descriptor = deepcopy(raw)
                client.capabilities = SimpleNamespace(action_interval=1 / 30)
                client.session_id = 'UNEXPECTED' if admitted else ''
                client.transport = SimpleNamespace(close=lambda: events.append('transport_close'))
                return client
            def close(self):
                if self.session_id:
                    raise AssertionError('Do not close admitted client: that sends control')
                events.append('discovery_close')
            def admit(self, **kwargs):
                raise AssertionError('Inspection must not admit')
            def control(self, **kwargs):
                raise AssertionError('Inspection must not control')
            def infer(self, **kwargs):
                raise AssertionError('Inspection must not infer')
        return {
            'lerobot': _module('lerobot'),
            'lerobot.configs': _module('lerobot.configs'),
            'lerobot.configs.policies': _module('lerobot.configs.policies', PreTrainedConfig=Config),
            'lerobot.policies': _module('lerobot.policies'),
            'lerobot.policies.factory': _module('lerobot.policies.factory', get_policy_class=factory, make_pre_post_processors=processors),
            'lerobot.inference': _module('lerobot.inference', SyncInferenceEngine=Engine, RemoteInferenceConfig=RemoteConfig),
            'lerobot.remote_inference': _module('lerobot.remote_inference', RemoteClient=Remote),
            'lerobot.utils': _module('lerobot.utils'),
            'lerobot.utils.import_utils': _module('lerobot.utils.import_utils', register_third_party_plugins=lambda: events.append('register_plugins')),
            'lerobot.utils.action_interpolator': _module('lerobot.utils.action_interpolator', ActionInterpolator=Interpolator),
            'torch': _module('torch', cuda=SimpleNamespace(is_available=lambda: cuda),
                             backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: mps))),
        }

    def execution(self):
        return {'kind': 'remote', 'endpoint': 'tcp/remote:7447', 'deployment': 'deployed', 'expected_artifact': 'remote-artifact'}

    def test_verify_exact_pinned_inventory_and_revision(self):
        self.assertEqual(verify_artifact(self.artifact()), self.root)
        artifact = self.artifact()
        artifact['artifact_revision'] = 'sha256:' + '0' * 64
        with self.assertRaises(PolicyError) as raised:
            verify_artifact(artifact)
        self.assertEqual(raised.exception.code, 'ARTIFACT_REVISION_CONFLICT')

    def test_compact_inventory_pin_roundtrip_and_load(self):
        artifact = self.artifact()
        pin = self.root.parent / 'private-inventory.json'
        pin.write_text(json.dumps(artifact))
        compact = {key: artifact[key] for key in ('root', 'checkpoint_path', 'artifact_revision')}
        compact['inventory_path'] = str(pin)
        self.assertEqual(verify_artifact(compact), self.root)
        self.assertNotIn('files', compact)
        profile = self.profile()
        profile['_artifact'] = compact
        with patch.dict(sys.modules, self.sdk()):
            session = load_managed_local(profile)
            self.assertNotIn('files', session.profile['_artifact'])
            self.assertEqual(session.preparation_details['artifact_revision'], compact['artifact_revision'])
            session.close()

    def test_compact_inventory_pin_rejects_mismatch_tamper_and_symlink(self):
        artifact = self.artifact()
        pin = self.root.parent / 'private-inventory.json'
        compact = {key: artifact[key] for key in ('root', 'checkpoint_path', 'artifact_revision')}
        compact['inventory_path'] = str(pin)
        for key in ('root', 'checkpoint_path', 'artifact_revision'):
            changed = deepcopy(artifact)
            changed[key] += '-tampered'
            pin.write_text(json.dumps(changed))
            with self.subTest(key=key), self.assertRaises(PolicyError) as raised:
                verify_artifact(compact)
            self.assertEqual(raised.exception.code, 'ARTIFACT_PIN_CONFLICT')
        changed = deepcopy(artifact)
        changed['files'][0]['sha256'] = '0' * 64
        pin.write_text(json.dumps(changed))
        with self.assertRaises(PolicyError) as raised:
            verify_artifact(compact)
        self.assertEqual(raised.exception.code, 'ARTIFACT_REVISION_CONFLICT')
        pin.write_text(json.dumps(artifact))
        linked = self.root.parent / 'linked-inventory.json'
        linked.symlink_to(pin)
        compact['inventory_path'] = str(linked)
        with self.assertRaises(PolicyError) as raised:
            verify_artifact(compact)
        self.assertEqual(raised.exception.code, 'INVALID_ARTIFACT_PIN')

    def test_installed_plugins_register_before_config_and_class_lookup(self):
        with patch.dict(sys.modules, self.sdk()):
            session = load_managed_local(self.profile())
            session.close()
        registered = self.events.index('register_plugins')
        configured = next(index for index, event in enumerate(self.events) if isinstance(event, tuple) and event[0] == 'config')
        classified = next(index for index, event in enumerate(self.events) if isinstance(event, tuple) and event[0] == 'category')
        self.assertLess(registered, configured)
        self.assertLess(registered, classified)

    def test_verify_rejects_hash_and_size_changes_and_unlisted_files(self):
        artifact = self.artifact()
        (self.root / 'model.safetensors').write_bytes(b'X' * len(b'not loaded by test'))
        with self.assertRaises(PolicyError) as raised:
            verify_artifact(artifact)
        self.assertEqual(raised.exception.code, 'ARTIFACT_DIGEST_MISMATCH')
        artifact = self.artifact()
        (self.root / 'new-file').write_bytes(b'added')
        with self.assertRaises(PolicyError) as raised:
            verify_artifact(artifact)
        self.assertEqual(raised.exception.code, 'ARTIFACT_INVENTORY_CONFLICT')

    def test_verify_rejects_escape_symlinks_and_traversal(self):
        artifact = self.artifact()
        outside = self.root.parent / 'outside'
        outside.mkdir()
        artifact['checkpoint_path'] = str(outside)
        with self.assertRaises(PolicyError):
            verify_artifact(artifact)
        artifact = self.artifact()
        (self.root / 'escape').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(PolicyError) as raised:
            verify_artifact(artifact)
        self.assertEqual(raised.exception.code, 'ARTIFACT_ESCAPE')
        (self.root / 'escape').unlink()
        artifact = self.artifact()
        artifact['files'][0]['path'] = '../config.json'
        with self.assertRaises(PolicyError):
            verify_artifact(artifact)

    def test_standard_checkpoint_loads_saved_processors_and_stays_paused(self):
        before_env = dict(os.environ)
        with patch.dict(sys.modules, self.sdk()):
            session = load_managed_local(self.profile())
            self.assertIsInstance(session, NativePolicy)
            self.assertEqual(session.preparation_details['category'], 'custom_installed_type')
            self.assertEqual(session.preparation_details['device'], 'cpu')
            self.assertTrue(session.preparation_details['weights_loaded'])
            self.assertTrue(session.preparation_details['processors_loaded'])
            self.assertFalse(session.preparation_details['warmup_performed'])
            self.assertFalse(session.preparation_details['inference_test_performed'])
            self.assertTrue(session.preparation_details['paused'])
            self.assertEqual(session.gate().state, 'WAIT')
            self.assertEqual(session.poll({}, 1).state, 'WAIT')
            self.assertLess(self.events.index('reset'), self.events.index('start'))
            self.assertLess(self.events.index('start'), self.events.index('pause'))
            self.assertNotIn('resume', self.events)
            session.reset_task('', ('authorized-operation', 1))
            self.assertFalse(session.preparation_details['paused'])
            self.assertEqual(session.gate().state, 'ACTION')
            session.close()
        self.assertEqual(dict(os.environ), before_env)

    def test_auto_device_uses_actual_cuda_then_mps(self):
        for cuda, mps, expected in ((True, True, 'cuda'), (False, True, 'mps'), (False, False, 'cpu')):
            with self.subTest(expected=expected), patch.dict(sys.modules, self.sdk(cuda=cuda, mps=mps)):
                session = load_managed_local(self.profile())
                self.assertEqual(session.preparation_details['device'], expected)
                session.close()
        profile = self.profile()
        profile['device'] = 'cuda'
        with patch.dict(sys.modules, self.sdk()), self.assertRaises(PolicyError) as raised:
            load_managed_local(profile)
        self.assertEqual(raised.exception.code, 'DEVICE_UNAVAILABLE')

    def test_bad_dimensions_feature_order_and_camera_keys_fail_before_weight_load(self):
        cases = [('state_features', self.features[:1]), ('action_features', self.features[:1]),
                 ('cameras', {'observation.images.guessed': 'new_runtime_camera'})]
        for key, value in cases:
            self.events.clear()
            profile = self.profile()
            profile[key] = value
            with self.subTest(key=key), patch.dict(sys.modules, self.sdk()), self.assertRaises(PolicyError):
                load_managed_local(profile)
            self.assertFalse(any(isinstance(event, tuple) and event[0] == 'weights' for event in self.events))

    def test_saved_contract_preserves_order_units_but_allows_runtime_camera_and_joint_ids(self):
        trained = deepcopy(self.features)
        trained[0]['joint'] = 'old_training_arm_joint'
        manifest = {'schema_version': 1, 'lerobot_version': '0.7.0', 'state_features': trained,
                    'action_features': trained, 'cameras': {'observation.images.forward': 'old_training_camera'},
                    'action_fps': 30, 'language_conditioned': False, 'calibration_revision': 'measured', 'arm_id': 'old_training_arm'}
        (self.root / 'manifest.json').write_text(json.dumps(manifest))
        profile = self.profile()
        with patch.dict(sys.modules, self.sdk()):
            session = load_managed_local(profile)
            self.assertEqual(session.profile['cameras']['observation.images.forward'], 'new_runtime_camera')
            session.close()
        profile['state_features'] = list(reversed(profile['state_features']))
        with patch.dict(sys.modules, self.sdk()), self.assertRaises(PolicyError) as raised:
            load_managed_local(profile)
        self.assertEqual(raised.exception.code, 'MODEL_FEATURE_ORDER_MISMATCH')

    def test_corrupt_saved_processors_raise_honest_native_error(self):
        with patch.dict(sys.modules, self.sdk(fail_processors=True)), self.assertRaisesRegex(RuntimeError, 'processor state incompatible'):
            load_managed_local(self.profile())
        self.assertNotIn('start', self.events)

    def test_changed_artifact_during_load_is_retired_before_return(self):
        sdk = self.sdk()
        original = sdk['lerobot.policies.factory'].make_pre_post_processors
        def processors(*args, **kwargs):
            (self.root / 'model.safetensors').write_bytes(b'replaced while loading')
            return original(*args, **kwargs)
        sdk['lerobot.policies.factory'].make_pre_post_processors = processors
        with patch.dict(sys.modules, sdk), self.assertRaises(PolicyError) as raised:
            load_managed_local(self.profile())
        self.assertEqual(raised.exception.code, 'ARTIFACT_CHANGING')
        self.assertIn('stop', self.events)

    def test_two_real_views_and_pinned_empty_slot_prepare_without_a_fake_binding(self):
        for kind in ('smolvla', 'pi05'):
            with self.subTest(kind=kind):
                config = deepcopy(self.config)
                config.update(type=kind, empty_cameras=1)
                config['input_features']['observation.images.wrist'] = {'type': 'VISUAL', 'shape': [3, 240, 320]}
                config['input_features']['observation.images.empty_camera_0'] = {'type': 'VISUAL', 'shape': [3, 240, 320]}
                (self.root / 'config.json').write_text(json.dumps(config))
                profile = self.profile()
                profile['cameras']['observation.images.wrist'] = 'wrist_runtime_camera'
                profile['_masked_camera_inputs'] = ['observation.images.empty_camera_0']
                with patch.dict(sys.modules, self.sdk()):
                    session = load_managed_local(profile)
                    self.assertEqual(set(session.profile['cameras']), {'observation.images.forward', 'observation.images.wrist'})
                    self.assertEqual(session.profile['_masked_camera_inputs'], ['observation.images.empty_camera_0'])
                    self.assertTrue(session.preparation_details['paused'])
                    session.close()

    def test_native_generated_empty_slot_is_pinned_even_when_not_serialized(self):
        config = deepcopy(self.config)
        config.update(type='smolvla', empty_cameras=1)
        (self.root / 'config.json').write_text(json.dumps(config))
        profile = self.profile()
        profile['_masked_camera_inputs'] = ['observation.images.empty_camera_0']
        with patch.dict(sys.modules, self.sdk()):
            session = load_managed_local(profile)
            session.close()

    def test_missing_required_view_cannot_be_reclassified_by_compiled_or_gui_mask(self):
        config = deepcopy(self.config)
        config.update(type='smolvla', empty_cameras=1)
        (self.root / 'config.json').write_text(json.dumps(config))
        profile = self.profile()
        profile['cameras'] = {}
        profile['_masked_camera_inputs'] = ['observation.images.forward', 'observation.images.empty_camera_0']
        with patch.dict(sys.modules, self.sdk()), self.assertRaises(PolicyError) as raised:
            load_managed_local(profile)
        self.assertEqual(raised.exception.code, 'MODEL_MASK_CONTRACT_MISMATCH')
        profile['_masked_camera_inputs'] = ['observation.images.empty_camera_0']
        with patch.dict(sys.modules, self.sdk()), self.assertRaises(PolicyError) as raised:
            load_managed_local(profile)
        self.assertEqual(raised.exception.code, 'MODEL_CAMERA_KEYS_MISMATCH')

    def test_pi05_explicit_registration_mask_is_allowed_but_smol_budget_is_enforced(self):
        config = deepcopy(self.config)
        config.update(type='pi05', empty_cameras=0)
        key = 'observation.images.training_masked_slot'
        config['input_features'][key] = {'type': 'VISUAL', 'shape': [3, 240, 320]}
        (self.root / 'config.json').write_text(json.dumps(config))
        (self.root / 'station-policy.yaml').write_text(json.dumps({'contract': {'camera_inputs': [{'key': key, 'masked': True}]}}))
        profile = self.profile()
        profile['_masked_camera_inputs'] = [key]
        with patch.dict(sys.modules, self.sdk()):
            session = load_managed_local(profile)
            session.close()
        config['type'] = 'smolvla'
        (self.root / 'config.json').write_text(json.dumps(config))
        profile['_artifact'] = self.artifact()
        with patch.dict(sys.modules, self.sdk()), self.assertRaises(PolicyError) as raised:
            load_managed_local(profile)
        self.assertEqual(raised.exception.code, 'MODEL_MASK_UNSUPPORTED')

    def test_masked_slot_rejects_real_camera_binding_and_all_masked_inputs(self):
        config = deepcopy(self.config)
        config.update(type='pi05', empty_cameras=1)
        (self.root / 'config.json').write_text(json.dumps(config))
        profile = self.profile()
        profile['_masked_camera_inputs'] = ['observation.images.empty_camera_0']
        profile['cameras']['observation.images.empty_camera_0'] = 'invented_camera'
        with patch.dict(sys.modules, self.sdk()), self.assertRaises(PolicyError) as raised:
            load_managed_local(profile)
        self.assertEqual(raised.exception.code, 'MODEL_MASK_BINDING_CONFLICT')
        config['empty_cameras'] = 0
        (self.root / 'config.json').write_text(json.dumps(config))
        (self.root / 'station-policy.yaml').write_text(json.dumps({'contract': {'camera_inputs': [{'key': 'observation.images.forward', 'masked': True}]}}))
        profile = self.profile()
        profile['_masked_camera_inputs'] = ['observation.images.forward']
        profile['cameras'] = {}
        with patch.dict(sys.modules, self.sdk()), self.assertRaises(PolicyError) as raised:
            load_managed_local(profile)
        self.assertEqual(raised.exception.code, 'MODEL_MASK_CONTRACT_INVALID')

    def test_remote_inspection_is_describe_only_and_filters_private_fields(self):
        with patch.dict(sys.modules, self.sdk()):
            result = inspect_remote(self.execution())
        self.assertIs(result['admitted'], False)
        self.assertEqual(result['descriptor']['artifact_identity'], 'remote-artifact')
        self.assertEqual(result['descriptor']['capabilities']['features'][0]['names'], ['trained.shoulder', 'trained.gripper'])
        self.assertEqual(result['descriptor']['capabilities']['modes'], ['chunk'])
        self.assertEqual(result['descriptor']['semantics'], 'trained-degrees-percent')
        serialized = json.dumps(result)
        for secret in ('DO_NOT_EXPOSE', 'PRIVATE_SESSION', 'PRIVATE_FAILURE'):
            self.assertNotIn(secret, serialized)
        self.assertEqual([event for event in self.events if isinstance(event, str)], ['describe', 'discovery_close'])
        config = next(event[1] for event in self.events if isinstance(event, tuple) and event[0] == 'remote_config')
        self.assertEqual(config['semantics'], 'station-discovery-only')
        self.assertEqual(config['expected_artifact'], 'remote-artifact')

    def test_remote_connection_credentials_only_from_startup_owned_mapping(self):
        execution = self.execution()
        execution['connection_profile'] = 'secure_lab'
        with patch.dict(sys.modules, self.sdk()):
            with self.assertRaises(PolicyError):
                inspect_remote(execution)
            with self.assertRaises(PolicyError):
                inspect_remote(execution, {'zenoh_config_path': '/arbitrary/from/gui'})
            result = inspect_remote(execution, {'handshake_timeout_s': 2},
                                    {'zenoh_config_path': '/startup/authorized.json5', 'zenoh_mode': 'client'})
        self.assertFalse(result['admitted'])
        config = next(event[1] for event in self.events if isinstance(event, tuple) and event[0] == 'remote_config')
        self.assertEqual(config['zenoh_config_path'], '/startup/authorized.json5')
        self.assertEqual(config['handshake_timeout_s'], 2)
        self.assertNotIn('/startup/authorized.json5', json.dumps(result))

    def test_unexpected_admission_never_sends_close_control(self):
        with patch.dict(sys.modules, self.sdk(admitted=True)), self.assertRaises(PolicyError) as raised:
            inspect_remote(self.execution())
        self.assertEqual(raised.exception.code, 'REMOTE_DISCOVERY_ADMITTED')
        self.assertIn('transport_close', self.events)
        self.assertNotIn('discovery_close', self.events)


if __name__ == '__main__':
    unittest.main()
