"""Actual hardware-free manager orchestration with file-owned configuration."""
from copy import deepcopy
import json
import time

import pytest
import yaml

from soarm_station.policy_common import PolicyError
from soarm_station.policy_manager import PolicyManager


CALIBRATION = 'fixture-calibration-v1'


def robot_resources():
    return {'arms': [{'id': 'arm1', 'backend': 'mock', 'calibration_revision': CALIBRATION,
                      'joints': [{'name': 'shoulder_pan', 'min_rad': -1., 'max_rad': 1., 'velocity_rad_s': 1.}],
                      'gripper': {'name': 'gripper'}}],
            'cameras': [{'id': 'forward', 'backend': 'mock'}]}


def checkpoint_files():
    """Complete metadata plus deliberately non-deserializable fixture bytes."""
    features = [{'name': 'arm1/shoulder_pan.pos', 'joint': 'shoulder_pan', 'unit': 'degree'},
                {'name': 'arm1/gripper.pos', 'joint': 'gripper', 'unit': 'percent'}]
    contract = {'state_features': deepcopy(features), 'action_features': deepcopy(features),
                'action_fps': 20, 'language_conditioned': False, 'calibration_revision': CALIBRATION,
                'camera_inputs': [{'key': 'observation.images.scene', 'shape': [3, 480, 640], 'role': 'scene', 'masked': False}]}
    config = {'type': 'act', 'input_features': {'observation.state': {'type': 'STATE', 'shape': [2]},
              'observation.images.scene': {'type': 'VISUAL', 'shape': [3, 480, 640]}},
              'output_features': {'action': {'type': 'ACTION', 'shape': [2]}}}
    return {'config.json': json.dumps(config).encode(),
            'model.safetensors': b'fixture bytes; no model deserialization is permitted',
            'policy_preprocessor.json': b'{"steps": []}',
            'policy_postprocessor.json': b'{"steps": []}',
            'station-policy.yaml': yaml.safe_dump({'schema_version': 1, 'name': 'Fixture ACT', 'contract': contract}).encode()}


def write_checkpoint(path):
    path.mkdir(parents=True, exist_ok=True)
    for name, data in checkpoint_files().items():
        (path / name).write_bytes(data)
    return path


def write_sources(path, roots=None):
    roots = roots if roots is not None else [{'id': 'ssd', 'label': 'Fixture SSD', 'path': 'models',
                                            'enabled': True, 'allow_upload': True}]
    path.write_text(yaml.safe_dump({'schema_version': 1, 'roots': roots}))


def wait_scan(manager, job):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = manager.catalog.scan_status(job['id'])
        if result['state'] in ('complete', 'failed'):
            assert result['state'] == 'complete', result
            return result
        time.sleep(.005)
    raise AssertionError('Catalog scan did not finish within five seconds')


def build_manager(base, *, alias=False):
    base.mkdir(parents=True, exist_ok=True)
    write_checkpoint(base / 'models' / 'demo')
    write_sources(base / 'model-sources.yaml')
    settings = {'source_config': 'model-sources.yaml', 'cache_db': 'cache/policies.sqlite3',
                'profile_store' if alias else 'profiles_dir': 'canonical-profiles'}
    config = {'_path': str(base / 'runtime.yaml'), 'policy_management': settings}
    manager = PolicyManager(config, resources=robot_resources(), bridge=None)
    wait_scan(manager, manager.catalog.scan())
    return manager


@pytest.fixture
def manager(tmp_path):
    manager = build_manager(tmp_path)
    try:
        yield manager
    finally:
        manager.close()


def profile_document(manager, *, profile_id='cup_act', unresolved=False, remote=False):
    document = {'schema_version': 1, 'id': profile_id, 'name': 'Cup placement',
                'execution': {'kind': 'local', 'device': 'cpu'},
                'bindings': {'arm': 'arm1', 'cameras': {'observation.images.scene': 'forward'}},
                'extensions': {'operator': {'note': 'preserved'}}}
    if remote:
        document['execution'] = {'kind': 'remote', 'endpoint': 'tcp/fixture.invalid:7447',
                                 'deployment': 'cup-remote', 'expected_artifact': 'sha256:' + 'b' * 64}
    else:
        model = manager.catalog.models()['models'][0]
        document['model'] = {key: model[key] for key in ('source_id', 'relative_path', 'artifact_revision')}
        if unresolved:
            document['model']['relative_path'] = 'not-copied-yet'
    return document


def test_startup_paths_are_single_authority_and_profile_store_alias_works(tmp_path):
    for alias in (False, True):
        base = tmp_path / str(alias)
        manager = build_manager(base, alias=alias)
        try:
            assert manager.source_config == base / 'model-sources.yaml'
            assert manager.profiles_dir == base / 'canonical-profiles'
            assert manager.cache_db == base / 'cache/policies.sqlite3'
            sources = manager.sources()
            assert sources['schema_version'] == 1
            assert sources['profile_store'] == str(manager.profiles_dir)
            assert sources['roots'][0]['resolved_path'] == str(base / 'models')
            saved = manager.store.save({'document': profile_document(manager)}, create=True)
            before = sources['revision']
            write_sources(manager.source_config, [{'id': 'ssd', 'path': 'models', 'allow_upload': False}])
            reloaded = manager.reload_sources({'expected_revision': before})
            assert reloaded['profile_store'] == str(manager.profiles_dir)
            assert manager.store.get(saved['id'])['revision'] == saved['revision']
            assert not (base / 'policy-profiles').exists()
        finally:
            manager.close()


def test_root_reload_invalid_candidate_is_atomic_and_host_file_only(manager):
    before = manager.sources()
    manager.source_config.write_text('schema_version: 1\nroots: [{id: replacement, path: models}, {id: broken, path: missing, enabled: maybe}]\n')
    with pytest.raises(PolicyError) as invalid:
        manager.reload_sources({'expected_revision': before['revision']})
    assert invalid.value.code == 'INVALID_SOURCES'
    after = manager.sources()
    assert after['revision'] == before['revision']
    assert after['text'] == before['text']
    assert [root['id'] for root in after['roots']] == ['ssd']
    for body in ({'roots': []}, {'path': '/tmp'}, {'profile_store': 'elsewhere'}):
        with pytest.raises(PolicyError) as rejected:
            manager.reload_sources(body)
        assert rejected.value.code == 'INVALID_REQUEST'
    write_sources(manager.source_config)
    with pytest.raises(PolicyError) as stale:
        manager.reload_sources({'expected_revision': 'sha256:' + '0' * 64})
    assert stale.value.status == 409


def test_real_catalog_metadata_resolves_without_preparing_native_runtime(manager):
    catalog = manager.catalog.models(q='fixture')
    assert catalog['schema_version'] == 1 and catalog['total'] == 1
    model = catalog['models'][0]
    assert model['category'] == 'act'
    assert model['selectable'] is True
    assert model['calibration_revision'] == CALIBRATION
    assert model['camera_inputs'][0]['role'] == 'scene'
    assert model['action_fps'] == 20
    assert len(model['state_features']) == len(model['action_features']) == 2
    validation = manager.validate({'document': profile_document(manager)})
    assert validation['valid'] is True and validation['ready'] is True
    assert validation['errors'] == []
    inspected = manager.inspect_draft({'document': profile_document(manager)})
    assert inspected['admitted'] is False
    assert inspected['descriptor']['artifact_revision'] == model['artifact_revision']
    runtime = manager.runtime()
    assert runtime['state'] == 'unavailable'
    assert runtime['applied'] is None and runtime['pending'] is None
    assert runtime['capabilities']['local_prepare'] is False
    assert runtime['capabilities']['remote_inspect'] is False
    assert manager.public_runtime_profiles() == []


def test_unresolved_schema_valid_draft_saves_and_reopens_with_readiness_errors(manager):
    candidate = profile_document(manager, unresolved=True)
    validation = manager.validate({'document': candidate})
    assert validation['valid'] is True and validation['ready'] is False
    assert validation['errors']
    saved = manager.store.save({'document': candidate}, create=True)
    reopened = manager.store.get(saved['id'])
    assert reopened['validation']['valid'] is True
    assert reopened['validation']['ready'] is False
    assert reopened['validation']['errors'] == saved['validation']['errors']
    summary = manager.profiles()['profiles'][0]
    assert summary['state'] == 'draft'
    assert summary['can_apply'] is False and summary['applied'] is False


def test_binding_contract_conflicts_remain_drafts_and_never_prepare(manager):
    candidate = profile_document(manager)
    candidate['contract'] = {'action_fps': 99}
    candidate['bindings']['cameras']['observation.images.scene'] = 'not_configured'
    saved = manager.store.save({'document': candidate}, create=True)
    assert saved['validation']['valid'] and not saved['validation']['ready']
    codes = {error['code'] for error in saved['validation']['errors']}
    assert {'TRAINED_CONTRACT_CONFLICT', 'CAMERA_UNRESOLVED'} <= codes
    with pytest.raises(PolicyError) as rejected:
        manager.prepare(saved['id'], {'expected_revision': saved['revision'], 'mode': 'idle'})
    assert rejected.value.code == 'PROFILE_NOT_READY'
    assert manager.runtime()['applied'] is None


def test_bridge_absence_never_fake_prepares_or_inspects_remote(manager):
    saved = manager.store.save({'document': profile_document(manager)}, create=True)
    with pytest.raises(PolicyError) as unavailable:
        manager.prepare(saved['id'], {'expected_revision': saved['revision'], 'mode': 'idle'})
    assert unavailable.value.code == 'RUNTIME_UNAVAILABLE'
    assert unavailable.value.status == 503
    with pytest.raises(PolicyError) as stale:
        manager.prepare(saved['id'], {'expected_revision': 'stale', 'mode': 'idle'})
    assert stale.value.status == 409
    with pytest.raises(PolicyError) as unavailable:
        manager.inspect_draft({'document': profile_document(manager, remote=True)})
    assert unavailable.value.code == 'RUNTIME_UNAVAILABLE'
    remote = manager.store.save({'document': profile_document(manager, profile_id='remote_cup', remote=True)}, create=True)
    assert remote['validation']['valid'] and not remote['validation']['ready']
    with pytest.raises(PolicyError) as unavailable:
        manager.prepare(remote['id'], {'expected_revision': remote['revision'], 'mode': 'after_current'})
    assert unavailable.value.status == 503


def test_external_profile_edits_are_visible_without_a_database_copy(manager):
    saved = manager.store.save({'document': profile_document(manager)}, create=True)
    path = manager.profiles_dir / (saved['id'] + '.yaml')
    external = saved['text'].replace('Cup placement', 'Edited by operator')
    path.write_text(external)
    refreshed = manager.profiles()
    assert refreshed['profiles'][0]['name'] == 'Edited by operator'
    assert refreshed['profiles'][0]['revision'] != saved['revision']
    with pytest.raises(PolicyError) as stale:
        manager.store.save({'text': saved['text']}, saved['id'], saved['revision'])
    assert stale.value.status == 409
    assert stale.value.details['text'] == external
    manager.close()
    reopened = PolicyManager(manager.config, resources=robot_resources(), bridge=None)
    try:
        assert reopened.store.get(saved['id'])['text'] == external
        assert reopened.sources()['profile_store'] == str(manager.profiles_dir)
    finally:
        reopened.close()
