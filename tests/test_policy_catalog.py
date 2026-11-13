"""Hardware-free source ownership, checkpoint inventory and identity checks."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import yaml

from soarm_station.policy_catalog import PolicyCatalog
from soarm_station.policy_common import PolicyError


class PolicyCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.config = self.base / 'sources.yaml'
        self.cache = self.base / 'cache.sqlite3'
        self.root = self.base / 'models'
        self.root.mkdir()
        self.catalogs = []

    def tearDown(self):
        for catalog in self.catalogs:
            catalog.close()
        self.temp.cleanup()

    def source_file(self, roots=None, **extra):
        if roots is None:
            roots = [{'id': 'local', 'label': 'Local models', 'path': 'models', 'enabled': True, 'allow_upload': True}]
        self.config.write_text(yaml.safe_dump({'schema_version': 1, 'roots': roots, **extra}))

    def open(self):
        catalog = PolicyCatalog(self.config, self.cache)
        self.catalogs.append(catalog)
        self.wait_all(catalog)
        return catalog

    @staticmethod
    def wait(catalog, job):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = catalog.scan_status(job['id'])
            if result['state'] in ('complete', 'failed'):
                return result
            time.sleep(.005)
        raise AssertionError('Scan did not finish')

    def wait_all(self, catalog):
        for job in list(catalog._jobs.values()):
            self.wait(catalog, job)

    def checkpoint(self, name='act-demo', *, category='act', processors=True, sidecar=None, parent=None):
        path = (parent or self.root) / name
        path.mkdir(parents=True)
        (path / 'config.json').write_text(json.dumps({'type': category, 'input_features': {
            'observation.state': {'type': 'STATE', 'shape': [6]},
            'observation.images.wrist': {'type': 'VISUAL', 'shape': [3, 480, 640]},
        }, 'output_features': {'action': {'type': 'ACTION', 'shape': [6]}}}))
        (path / 'model.safetensors').write_bytes(b'not-deserialized-model-data')
        if processors:
            (path / 'policy_preprocessor.json').write_text(json.dumps({'steps': [{'state_file': 'normalization.safetensors'}]}))
            (path / 'policy_postprocessor.json').write_text(json.dumps({'steps': []}))
            (path / 'normalization.safetensors').write_bytes(b'not-deserialized-processor-data')
        if sidecar is not None:
            (path / 'station-policy.yaml').write_text(yaml.safe_dump(sidecar))
        return path

    def test_missing_startup_config_is_empty_and_explicit_reload_fails(self):
        catalog = self.open()
        self.assertEqual(catalog.models()['models'], [])
        self.assertEqual(catalog.sources()['text'], '')
        with self.assertRaises(PolicyError) as raised:
            catalog.reload()
        self.assertEqual(raised.exception.code, 'INVALID_SOURCES')
        self.assertFalse(self.config.exists())

    def test_atomic_candidate_failure_preserves_loaded_roots_and_revision(self):
        self.source_file()
        catalog = self.open()
        before = catalog.sources()
        self.config.write_text('schema_version: 1\nroots:\n - id: replacement\n   path: elsewhere\n - id: invalid\n   path: anything\n   enabled: maybe\n')
        with self.assertRaises(PolicyError):
            catalog.reload(before['revision'])
        after = catalog.sources()
        # Availability is live: writing the edited config can change free disk
        # space even though the loaded configuration was correctly retained.
        for snapshot in (before, after):
            for root in snapshot['roots']:
                root.pop('free_bytes', None)
        self.assertEqual(after, before)
        self.assertEqual(after['roots'][0]['id'], 'local')
        self.source_file()
        with self.assertRaises(PolicyError) as raised:
            catalog.reload('sha256:stale')
        self.assertEqual(raised.exception.status, 409)

    def test_duplicate_yaml_keys_and_non_boolean_flags_rejected(self):
        self.source_file()
        catalog = self.open()
        before = catalog.sources()['revision']
        self.config.write_text('schema_version: 1\nroots: []\nroots: []\n')
        with self.assertRaises(PolicyError):
            catalog.reload()
        self.assertEqual(catalog.sources()['revision'], before)
        self.source_file([{'id': 'local', 'path': 'models', 'allow_upload': 1}])
        with self.assertRaises(PolicyError):
            catalog.reload()

    def test_offline_root_is_preserved_and_inspector_returns_live_eligibility(self):
        self.source_file([{'id': 'offline', 'path': 'not-mounted', 'allow_upload': True}])
        catalog = self.open()
        root = catalog.source('offline')
        self.assertEqual(root['state'], 'offline')
        self.assertFalse(root['online'])
        self.assertTrue(root['allow_upload'])
        self.assertIsNone(root['identity'])
        self.assertIsNone(root['free_bytes'])
        (self.base / 'not-mounted').mkdir()
        root = catalog.source('offline')
        self.assertTrue(root['online'])
        self.assertTrue(root['writable'])
        self.assertGreater(root['free_bytes'], 0)
        self.assertIn('inode', root['identity'])

    def test_initial_and_changed_roots_are_scanned_but_unchanged_reload_is_quiet(self):
        self.checkpoint()
        self.source_file()
        catalog = self.open()
        self.assertEqual(catalog.models()['total'], 1)
        response = catalog.reload()
        self.assertEqual(response['changed_source_ids'], [])
        self.assertIsNone(response['scan_job'])
        other = self.base / 'other-models'
        other.mkdir()
        self.checkpoint('other', parent=other)
        self.source_file([{'id': 'local', 'path': str(other)}])
        response = catalog.reload()
        self.assertEqual(response['changed_source_ids'], ['local'])
        self.assertIsNotNone(response['scan_job'])
        self.assertEqual(self.wait(catalog, response['scan_job'])['state'], 'complete')
        self.assertEqual([item['name'] for item in catalog.models()['models'] if item['selectable']], ['other'])
        self.assertTrue(any(item['state'] == 'source_changed' for item in catalog.models()['models']))

    def test_checkpoint_public_shape_unknown_metadata_and_full_resolution(self):
        path = self.checkpoint()
        self.source_file()
        catalog = self.open()
        envelope = catalog.models(q='ACT')
        model = envelope['models'][0]
        self.assertEqual(model['category'], 'act')
        self.assertEqual(model['format'], 'lerobot_checkpoint')
        self.assertEqual(model['state'], 'available')
        self.assertTrue(model['selectable'])
        self.assertIsNone(model['action_fps'])
        self.assertIsNone(model['language_conditioned'])
        self.assertEqual(model['state_features'], [])
        self.assertEqual(model['camera_inputs'], [{'key': 'observation.images.wrist', 'role': None,
                                                   'masked': False, 'shape': [3, 480, 640]}])
        self.assertTrue(any(issue['code'] == 'UNKNOWN_STATE_FEATURES' for issue in model['issues']))
        self.assertRegex(model['artifact_revision'], r'^sha256:[a-f0-9]{64}$')
        self.assertNotIn('config', model)
        self.assertNotIn('ready', model)
        descriptor = catalog.resolve({key: model[key] for key in ('source_id', 'relative_path', 'artifact_revision')})
        self.assertEqual(descriptor['checkpoint_path'], str(path))
        self.assertEqual(descriptor['root'], str(path))
        self.assertEqual(descriptor['config']['type'], 'act')
        self.assertEqual(descriptor['inventory'], descriptor['files'])
        self.assertEqual(catalog.get_model(model['id']), model)

    def test_missing_processors_and_state_companions_are_incomplete(self):
        path = self.checkpoint(processors=False)
        self.source_file()
        catalog = self.open()
        package = catalog.inspect_package(path)
        self.assertFalse(package['complete'])
        self.assertEqual(package['state'], 'incomplete')
        self.assertIn('policy_preprocessor.json', package['missing_files'])
        (path / 'policy_preprocessor.json').write_text(json.dumps({'steps': [{'state_file': 'missing.safetensors'}]}))
        (path / 'policy_postprocessor.json').write_text('{}')
        package = catalog.inspect_package(path)
        self.assertIn('missing.safetensors', package['missing_files'])

    def test_complete_unknown_type_is_unverified_instead_of_incomplete(self):
        path = self.checkpoint(category='new_architecture')
        self.source_file()
        catalog = self.open()
        package = catalog.inspect_package(path)
        self.assertTrue(package['complete'])
        self.assertIsNone(package['supported'])
        self.assertTrue(package['selectable'])
        self.assertEqual(package['state'], 'unverified')
        self.assertEqual(package['category'], 'new_architecture')
        self.assertIsNotNone(package['artifact_revision'])

    def test_registration_declares_mappings_and_never_reinterprets_category(self):
        trained = {'state_features': [{'name': 'joint.pos', 'joint': 'joint', 'unit': 'degree'}],
                   'action_features': [{'name': 'joint.pos', 'joint': 'joint', 'unit': 'degree'}],
                   'action_fps': 30, 'language_conditioned': False, 'calibration_revision': 'measured-v2',
                   'camera_inputs': [{'key': 'observation.images.wrist', 'role': 'wrist', 'masked': True,
                                      'shape': [3, 480, 640]}]}
        path = self.checkpoint(sidecar={'schema_version': 1, 'name': 'Registered', 'category': 'pi0', 'contract': trained})
        self.source_file()
        catalog = self.open()
        package = catalog.inspect_package(path)
        self.assertEqual(package['category'], 'act')
        self.assertEqual(package['name'], 'Registered')
        self.assertEqual(package['state_features'], trained['state_features'])
        self.assertEqual(package['action_fps'], 30)
        self.assertIs(package['language_conditioned'], False)
        self.assertEqual(package['camera_inputs'][0]['role'], 'wrist')
        self.assertTrue(package['camera_inputs'][0]['masked'])
        self.assertFalse(any(issue['code'].startswith('UNKNOWN_') for issue in package['issues']))

    def test_exported_bundle_validates_manifest_and_preserves_metadata(self):
        bundle = self.root / 'bundle'
        checkpoint = self.checkpoint('pretrained_model', parent=bundle)
        manifest = {'schema_version': 1, 'lerobot_version': '0.7.0', 'name': 'Exported',
                    'state_features': [{'name': 'a', 'joint': 'a', 'unit': 'degree'}],
                    'action_features': [{'name': 'a', 'joint': 'a', 'unit': 'degree'}],
                    'cameras': {'observation.images.wrist': 'arm1_wrist'}, 'action_fps': 20,
                    'calibration_revision': 'hash', 'language_conditioned': False,
                    'files': {path.relative_to(bundle).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                              for path in checkpoint.iterdir()}}
        (bundle / 'manifest.json').write_text(json.dumps(manifest))
        self.source_file()
        catalog = self.open()
        model = catalog.models()['models'][0]
        self.assertEqual(model['format'], 'station_bundle')
        self.assertEqual(model['relative_path'], 'bundle')
        self.assertEqual(model['name'], 'Exported')
        self.assertEqual(catalog.resolve(model['id'])['checkpoint_path'], str(checkpoint))
        (checkpoint / 'model.safetensors').write_bytes(b'corrupted')
        package = catalog.inspect_package(bundle)
        self.assertTrue(package['complete'])
        self.assertEqual(package['state'], 'incompatible')
        self.assertFalse(package['compatible'])
        self.assertTrue(any(issue['code'] == 'MANIFEST_DIGEST_MISMATCH' for issue in package['issues']))

    def test_hash_identity_catches_same_size_mtime_preserving_edits_and_reuses_cache(self):
        path = self.checkpoint()
        self.source_file()
        catalog = self.open()
        model = catalog.models()['models'][0]
        before_count = catalog._db.execute('SELECT COUNT(*) FROM hashes').fetchone()[0]
        first = catalog.inspect_package(path)
        self.assertEqual(catalog._db.execute('SELECT COUNT(*) FROM hashes').fetchone()[0], before_count)
        weights = path / 'model.safetensors'
        before = weights.stat()
        weights.write_bytes(b'X' * before.st_size)
        os.utime(weights, ns=(before.st_atime_ns, before.st_mtime_ns))
        second = catalog.inspect_package(path)
        self.assertNotEqual(first['artifact_revision'], second['artifact_revision'])
        with self.assertRaises(PolicyError) as raised:
            catalog.resolve(model['id'])
        self.assertEqual(raised.exception.code, 'ARTIFACT_REVISION_CONFLICT')
        self.assertEqual(self.wait(catalog, catalog.scan())['state'], 'complete')
        new_model = catalog.models()['models'][0]
        self.assertEqual(new_model['id'], model['id'])
        self.assertNotEqual(new_model['artifact_revision'], model['artifact_revision'])

    def test_package_changing_during_inventory_is_unselectable(self):
        path = self.checkpoint()
        self.source_file()
        catalog = self.open()
        original = catalog._inventory
        calls = []
        def changing(root):
            calls.append(root)
            if len(calls) == 2:
                (path / 'arriving-file').write_bytes(b'copy in progress')
            return original(root)
        with patch.object(catalog, '_inventory', changing):
            package = catalog.inspect_package(path)
        self.assertEqual(package['state'], 'copying')
        self.assertFalse(package['selectable'])
        self.assertFalse(package['complete'])
        self.assertIsNone(package['artifact_revision'])

    def test_staging_and_symlink_escapes_are_excluded(self):
        staged = self.root / '.station-uploads'
        staged.mkdir()
        self.checkpoint(parent=staged)
        external = self.base / 'external'
        external.mkdir()
        self.checkpoint(parent=external)
        (self.root / 'escape').symlink_to(external, target_is_directory=True)
        self.source_file()
        catalog = self.open()
        self.assertEqual(catalog.models()['total'], 0)
        path = self.checkpoint('with-symlink')
        (path / 'secret').symlink_to(self.config)
        package = catalog.inspect_package(path)
        self.assertEqual(package['state'], 'invalid')
        self.assertFalse(package['complete'])
        self.assertIn('symlink', package['reason'])

    def test_overlap_deduplicates_physical_checkpoint_and_retains_reference_alias(self):
        path = self.checkpoint()
        self.source_file([{'id': 'parent', 'path': str(self.root)}, {'id': 'direct', 'path': str(path)}])
        catalog = self.open()
        self.assertEqual(catalog.models()['total'], 1)
        alias = catalog.models(source_id='direct')['models'][0]
        self.assertEqual(alias['relative_path'], '.')
        self.assertEqual(catalog.resolve({key: alias[key] for key in ('source_id', 'relative_path', 'artifact_revision')})['id'], alias['id'])
        self.assertEqual(len(catalog.models()['groups']), 2)

    def test_missing_and_offline_records_retain_ids_and_artifact_revision(self):
        path = self.checkpoint()
        self.source_file()
        catalog = self.open()
        before = catalog.models()['models'][0]
        moved_root = self.base / 'disconnected'
        self.root.rename(moved_root)
        self.assertEqual(catalog.get_model(before['id'])['state'], 'offline')
        self.assertEqual(self.wait(catalog, catalog.scan())['state'], 'complete')
        unavailable = catalog.get_model(before['id'])
        self.assertEqual(unavailable['artifact_revision'], before['artifact_revision'])
        self.assertFalse(unavailable['selectable'])
        moved_root.rename(self.root)
        path.rename(self.root / 'hidden-model')
        self.assertEqual(self.wait(catalog, catalog.scan())['state'], 'complete')
        missing = catalog.get_model(before['id'])
        self.assertEqual(missing['state'], 'missing')
        self.assertEqual(missing['artifact_revision'], before['artifact_revision'])
        catalog.close()
        restored = self.open()
        self.assertEqual(restored.get_model(before['id'])['artifact_revision'], before['artifact_revision'])

    def test_repointing_configured_symlink_changes_source_identity(self):
        self.checkpoint()
        other = self.base / 'replacement'
        other.mkdir()
        link = self.base / 'root-link'
        link.symlink_to(self.root, target_is_directory=True)
        self.source_file([{'id': 'local', 'path': str(link), 'allow_upload': True}])
        catalog = self.open()
        before = catalog.source('local')
        model = catalog.models()['models'][0]
        link.unlink()
        link.symlink_to(other, target_is_directory=True)
        after = catalog.source('local')
        self.assertNotEqual(before['identity'], after['identity'])
        self.assertEqual(after['resolved_path'], str(other))
        self.assertEqual(catalog.get_model(model['id'])['state'], 'source_changed')
        with self.assertRaises(PolicyError):
            catalog.resolve(model['id'])

    def test_depth_limit_job_fails_and_preserves_previous_complete_catalog(self):
        self.checkpoint()
        self.source_file()
        catalog = self.open()
        previous = catalog.models()['models'][0]
        deeper = self.root / 'deep'
        deeper.mkdir()
        (deeper / 'child').mkdir()
        with patch('soarm_station.policy_catalog.MAX_DEPTH', 0):
            result = self.wait(catalog, catalog.scan())
        self.assertEqual(result['state'], 'failed')
        self.assertTrue(result['errors'])
        self.assertEqual(catalog.get_model(previous['id'])['artifact_revision'], previous['artifact_revision'])

    def test_registration_cannot_override_known_checkpoint_contract(self):
        path = self.checkpoint(sidecar={'contract': {'language_conditioned': True, 'action_fps': 10}})
        config_path = path / 'config.json'
        config = json.loads(config_path.read_text())
        config.update(language_conditioned=False, fps=30)
        config_path.write_text(json.dumps(config))
        self.source_file()
        catalog = self.open()
        descriptor = catalog.inspect_package(path)
        self.assertIs(descriptor['language_conditioned'], False)
        self.assertEqual(descriptor['action_fps'], 30)
        self.assertFalse(descriptor['compatible'])
        self.assertTrue(any(issue['code'] == 'REGISTRATION_CONFLICT' for issue in descriptor['issues']))

    def test_camera_registration_cannot_override_config_shape(self):
        path = self.checkpoint(sidecar={'contract': {'camera_inputs': [
            {'key': 'observation.images.wrist', 'shape': [3, 64, 64]}]}})
        self.source_file()
        catalog = self.open()
        descriptor = catalog.inspect_package(path)
        self.assertEqual(descriptor['camera_inputs'][0]['shape'], [3, 480, 640])
        self.assertFalse(descriptor['compatible'])
        self.assertFalse(descriptor['selectable'])


if __name__ == '__main__':
    unittest.main()
