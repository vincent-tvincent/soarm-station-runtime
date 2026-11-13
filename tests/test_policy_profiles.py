"""Hardware-free schema, round-trip and concurrent file-store guarantees."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import threading

import pytest

from soarm_station.policy_common import PolicyError, revision
from soarm_station.policy_profiles import MAX_PROFILE_BYTES, ProfileStore, parse_profile, validate_document


RAW = '''# Operator's original preset
schema_version: 1
id: cup_act
name: "Cup placement" # Preserve this comment
execution:
  kind: local
  device: cpu
model:
  source_id: ssd
  relative_path: cup/pretrained_model
  artifact_revision: "sha256:''' + 'a' * 64 + '''"
bindings:
  arm: arm1
  cameras:
    observation.images.scene: forward
  state: {shoulder.pos: shoulder_pan}
  actions: {shoulder.pos: shoulder_pan}
contract:
  state_features:
    - {name: shoulder.pos, joint: shoulder_pan, unit: degree}
  action_features:
    - {name: shoulder.pos, joint: shoulder_pan, unit: degree}
  action_fps: 30
  language_conditioned: false
  calibration_revision: factory-a
  camera_inputs:
    - key: observation.images.scene
      shape: [480, 640, 3]
      required: true
      masked: false
      role: scene
settings:
  startup_timeout_s: 12
  timeout_s: 5
  action_source_max_age_s: 2
  max_image_age_ms: 150
extensions:
  operator:
    note: keep me # Extension comment
    labels: [cup, slow]
'''


@pytest.fixture
def store(tmp_path):
    return ProfileStore(tmp_path / 'profiles')


def document():
    return json.loads(json.dumps(parse_profile(RAW)))


def test_raw_save_export_is_exact_and_unresolved_draft_is_saveable(store):
    saved = store.save({'text': RAW}, create=True)
    assert saved['text'] == RAW
    assert saved['revision'] == revision(RAW.encode())
    assert saved['validation']['valid'] is True
    assert saved['validation']['ready'] is False
    assert saved['validation']['resolved_model'] is None
    assert (store.directory / 'cup_act.yaml').read_text() == RAW
    assert store.export('cup_act') == (RAW, 'application/yaml; charset=utf-8')
    text, mime = store.export('cup_act', 'json')
    assert mime.startswith('application/json')
    assert json.loads(text) == saved['document']
    assert store.list()['profiles'][0]['state'] == 'draft'
    assert store.list()['profiles'][0]['can_apply'] is False


def test_structured_form_preserves_comments_advanced_fields_and_extensions(store):
    saved = store.save({'text': RAW}, create=True)
    form = document()
    form['name'] = 'A renamed preset'
    for field in ('contract', 'settings', 'extensions'):
        del form[field]
    form['model'].pop('artifact_revision')
    form['bindings'].pop('state')
    form['bindings'].pop('actions')
    edited = store.save({'document': form}, 'cup_act', saved['revision'])
    assert edited['document']['name'] == 'A renamed preset'
    assert edited['document']['contract'] == saved['document']['contract']
    assert edited['document']['settings'] == saved['document']['settings']
    assert edited['document']['extensions'] == saved['document']['extensions']
    assert edited['document']['model']['artifact_revision'] == saved['document']['model']['artifact_revision']
    assert edited['document']['bindings']['state'] == saved['document']['bindings']['state']
    for comment in ("# Operator's original preset", '# Preserve this comment', '# Extension comment'):
        assert comment in edited['text']
    assert store.get('cup_act')['text'] == edited['text']


def test_structured_optional_null_removes_and_raw_omission_removes(store):
    saved = store.save({'text': RAW}, create=True)
    form = document()
    form.update(contract=None, extensions=None)
    form['settings'] = {'startup_timeout_s': None}
    form['model']['artifact_revision'] = None
    form['bindings']['state'] = None
    edited = store.save({'document': form}, 'cup_act', saved['revision'])
    assert 'contract' not in edited['document']
    assert 'extensions' not in edited['document']
    assert 'startup_timeout_s' not in edited['document']['settings']
    assert edited['document']['settings']['timeout_s'] == 5
    assert 'artifact_revision' not in edited['document']['model']
    assert 'state' not in edited['document']['bindings']
    raw_document = deepcopy(edited['document'])
    raw_document.pop('settings')
    raw_document['bindings'] = {'arm': 'arm1', 'cameras': {}}
    edited = store.save({'text': json.dumps(raw_document)}, 'cup_act', edited['revision'])
    assert 'settings' not in edited['document']
    assert 'actions' not in edited['document']['bindings']


def test_camera_contract_advanced_declarations_survive_partial_form(store):
    saved = store.save({'text': RAW}, create=True)
    form = document()
    form['contract']['camera_inputs'] = [{'key': 'observation.images.scene'}]
    changed = store.save({'document': form}, 'cup_act', saved['revision'])
    assert changed['document']['contract']['camera_inputs'] == saved['document']['contract']['camera_inputs']


def test_reordered_camera_contract_keeps_metadata_with_its_feature_key(store):
    candidate = document()
    candidate['contract']['camera_inputs'].append({'key': 'observation.images.wrist', 'shape': [240, 320, 3], 'role': 'wrist'})
    saved = store.save({'document': candidate}, create=True)
    form = deepcopy(saved['document'])
    form['contract']['camera_inputs'] = [{'key': 'observation.images.wrist'}, {'key': 'observation.images.scene'}]
    changed = store.save({'document': form}, 'cup_act', saved['revision'])
    first, second = changed['document']['contract']['camera_inputs']
    assert first['shape'] == [240, 320, 3] and first['role'] == 'wrist'
    assert second['shape'] == [480, 640, 3] and second['role'] == 'scene'


def test_execution_switches_never_resurrect_other_variant_fields(store):
    saved = store.save({'text': RAW}, create=True)
    form = document()
    form.pop('model')
    form['execution'] = {'kind': 'remote', 'endpoint': 'tcp/host:7447',
                         'deployment': 'cup-remote', 'expected_artifact': 'sha256:' + 'b' * 64,
                         'host_label': 'Workstation', 'connection_profile': 'workstation', 'instance': 'one'}
    remote = store.save({'document': form}, 'cup_act', saved['revision'])
    assert remote['validation']['valid']
    assert 'model' not in remote['document']
    assert 'device' not in remote['document']['execution']
    local = deepcopy(remote['document'])
    local['execution'] = {'kind': 'local', 'device': 'mps'}
    local['model'] = {'source_id': 'ssd', 'relative_path': 'new/pretrained_model'}
    changed = store.save({'document': local}, 'cup_act', remote['revision'])
    assert changed['document']['execution'] == local['execution']
    assert 'artifact_revision' not in changed['document']['model']


def test_json_import_is_written_as_canonical_yaml(store):
    saved = store.save({'text': json.dumps(document())}, create=True)
    assert saved['document'] == document()
    assert saved['text'].startswith('schema_version: 1\n')
    assert not saved['text'].startswith('{')


def test_expected_revision_required_and_stale_update_returns_current_text(store):
    saved = store.save({'text': RAW}, create=True)
    with pytest.raises(PolicyError) as missing:
        store.save({'text': RAW}, 'cup_act')
    assert missing.value.code == 'REVISION_REQUIRED'
    assert missing.value.details['text'] == RAW
    updated = store.save({'text': RAW.replace('Cup placement', 'Updated')}, 'cup_act', saved['revision'])
    with pytest.raises(PolicyError) as stale:
        store.save({'text': RAW}, 'cup_act', saved['revision'])
    assert stale.value.status == 409
    assert stale.value.details['revision'] == updated['revision']
    assert stale.value.details['text'] == updated['text']
    assert store.get('cup_act')['text'] == updated['text']


def test_external_edit_conflicts_without_refresh(store):
    saved = store.save({'text': RAW}, create=True)
    external = RAW.replace('Cup placement', 'An external edit')
    (store.directory / 'cup_act.yaml').write_text(external)
    with pytest.raises(PolicyError) as conflict:
        store.save({'text': RAW}, 'cup_act', saved['revision'])
    assert conflict.value.details['text'] == external
    assert conflict.value.details['revision'] == revision(external.encode())
    assert store.get('cup_act')['text'] == external


def test_external_edit_during_validator_cannot_be_overwritten(store):
    saved = store.save({'text': RAW}, create=True)
    external = RAW.replace('Cup placement', 'Concurrent external edit')
    def validator(_document):
        (store.directory / 'cup_act.yaml').write_text(external)
        return {'valid': True, 'ready': False}
    store.validator = validator
    with pytest.raises(PolicyError) as conflict:
        store.save({'text': RAW.replace('Cup placement', 'Form edit')}, 'cup_act', saved['revision'])
    assert conflict.value.status == 409
    assert (store.directory / 'cup_act.yaml').read_text() == external


def test_independent_stores_cannot_both_replace_same_revision(store):
    saved = store.save({'text': RAW}, create=True)
    other = ProfileStore(store.directory)
    barrier = threading.Barrier(2)
    def save(candidate, label):
        barrier.wait(timeout=5)
        try:
            return candidate.save({'text': RAW.replace('Cup placement', label)}, 'cup_act', saved['revision'])
        except PolicyError as exc:
            return exc
    with ThreadPoolExecutor(max_workers=2) as executor:
        one = executor.submit(save, store, 'First')
        two = executor.submit(save, other, 'Second')
        results = [one.result(timeout=10), two.result(timeout=10)]
    assert sum(isinstance(item, dict) for item in results) == 1
    assert [item.status for item in results if isinstance(item, PolicyError)] == [409]


def test_create_never_overwrites_destination_created_outside_lock(store, monkeypatch):
    original_link = os.link
    def race(source, destination):
        Path(destination).write_text('# Created by another process\n')
        return original_link(source, destination)
    monkeypatch.setattr(os, 'link', race)
    with pytest.raises(PolicyError) as conflict:
        store.save({'text': RAW}, create=True)
    assert conflict.value.status == 409
    assert (store.directory / 'cup_act.yaml').read_text() == '# Created by another process\n'
    assert not list(store.directory.glob('*.pending'))


def test_import_preview_duplicate_requires_explicit_mode_and_compare(store):
    saved = store.save({'text': RAW}, create=True)
    preview = store.import_preview({'text': RAW})
    assert preview['valid']
    assert preview['profile']['id'] == 'cup_act'
    assert preview['conflict'] == {'revision': saved['revision'], 'text': RAW}
    with pytest.raises(PolicyError) as conflict:
        store.import_profile({'text': RAW, 'id': 'cup_act', 'mode': 'new'})
    assert conflict.value.status == 409
    with pytest.raises(PolicyError) as missing:
        store.import_profile({'text': RAW, 'id': 'cup_act', 'mode': 'replace'})
    assert missing.value.code == 'REVISION_REQUIRED'
    replacement = store.import_profile({'text': RAW.replace('Cup placement', 'Replacement'),
                                       'id': 'cup_act', 'mode': 'replace', 'expected_revision': saved['revision']})
    assert replacement['document']['name'] == 'Replacement'
    copy = store.import_profile({'text': RAW, 'id': 'cup_act_copy', 'mode': 'new'})
    assert copy['id'] == copy['document']['id'] == 'cup_act_copy'
    assert '# Preserve this comment' in copy['text']
    assert store.get('cup_act')['revision'] == replacement['revision']


def test_refresh_exposes_external_add_edit_delete_and_invalid_documents(store):
    first = store.refresh()
    (store.directory / 'cup_act.yaml').write_text(RAW)
    second = store.refresh()
    assert first['revision'] != second['revision']
    assert len(second['profiles']) == 1
    (store.directory / 'cup_act.yaml').write_text('name: [unterminated\n')
    third = store.refresh()
    assert second['revision'] != third['revision']
    assert third['profiles'][0]['state'] == 'invalid'
    assert store.get('cup_act')['document'] is None
    assert store.get('cup_act')['validation']['errors'][0]['line'] >= 1
    (store.directory / 'cup_act.yaml').unlink()
    assert store.refresh()['profiles'] == []


def test_only_canonical_yaml_files_are_profile_authority(store):
    (store.directory / 'cup_act.yml').write_text(RAW)
    (store.directory / 'cup_act.json').write_text(json.dumps(document()))
    (store.directory / 'Uppercase.yaml').write_text(RAW)
    assert store.list()['profiles'] == []
    (store.directory / 'other_id.yaml').write_text(RAW)
    listed = store.list()['profiles'][0]
    assert listed['state'] == 'invalid'
    assert store.get('other_id')['validation']['errors'][-1]['code'] == 'PROFILE_ID_MISMATCH'


@pytest.mark.parametrize('text', [
    'schema_version: 1\nname: [unterminated\n',
    'schema_version: 1\nschema_version: 1\n',
    '---\nid: first\n---\nid: second\n',
    '!!python/object/apply:os.system [echo unsafe]\n',
    '!unexpected {id: cup_act}\n',
    'extensions: &recursive [*recursive]\n',
    RAW.replace('note: keep me', 'note: .nan'),
    RAW.replace('note: keep me', 'note: .inf'),
    json.dumps(document()).replace('"action_fps": 30', '"action_fps": NaN'),
    json.dumps(document()).replace('"action_fps": 30', '"action_fps": Infinity'),
])
def test_invalid_raw_text_never_replaces_saved_profile_and_keeps_location(store, text):
    saved = store.save({'text': RAW}, create=True)
    validation = store.validate({'text': text})
    assert validation['valid'] is False
    with pytest.raises(PolicyError) as invalid:
        store.save({'text': text}, 'cup_act', saved['revision'])
    assert invalid.value.details['text'] == text
    assert invalid.value.details['validation']['errors']
    assert store.get('cup_act')['revision'] == saved['revision']


@pytest.mark.parametrize('change,path', [
    (lambda d: d.update(unknown='field'), 'unknown'),
    (lambda d: d['execution'].update(category='act'), 'execution.category'),
    (lambda d: d['model'].update(device='cuda'), 'model.device'),
    (lambda d: d['model'].update(relative_path='../escape'), 'model.relative_path'),
    (lambda d: d['settings'].update(secret_option='unknown'), 'settings.secret_option'),
    (lambda d: d['settings'].update(timeout_s=True), 'settings.timeout_s'),
    (lambda d: d['contract'].update(unknown=True), 'contract.unknown'),
    (lambda d: d['contract']['camera_inputs'][0].update(width=640), 'contract.camera_inputs[0].width'),
    (lambda d: d['contract']['action_features'][0].update(unit='degrees'), 'contract.action_features[0].unit'),
])
def test_unknown_or_invalid_schema_fields_are_not_saveable(store, change, path):
    candidate = document()
    change(candidate)
    validation = store.validate({'document': candidate})
    assert validation['valid'] is False
    assert path in [error.get('path') for error in validation['errors']]
    with pytest.raises(PolicyError):
        store.save({'document': candidate}, create=True)
    assert store.list()['profiles'] == []


def test_native_server_format_has_clear_format_explanation(store):
    native = 'deployment: cup-server\nmodel: {repo_or_path: cup, device: cpu}\nzenoh: {}\nfeatures: []\n'
    validation = store.validate({'text': native})
    assert validation['valid'] is False
    assert validation['errors'][0]['code'] == 'WRONG_PROFILE_FORMAT'
    assert 'native LeRobot policy-server' in validation['errors'][0]['message']
    preview = store.import_preview({'text': native})
    assert preview['valid'] is False
    assert preview['errors'][0]['code'] == 'WRONG_PROFILE_FORMAT'
    assert store.validate({'text': 'schema_version: 1\n' + native})['errors'][0]['code'] == 'WRONG_PROFILE_FORMAT'


def test_payload_requires_exactly_one_representation(store):
    for payload in ({}, {'text': RAW, 'document': document()}, {'text': RAW, 'unknown': True}, {'document': []}):
        assert store.validate(payload)['valid'] is False
    assert store.validate({'text': RAW})['valid'] is True
    assert store.validate({'document': document()})['valid'] is True


def test_limits_recursive_document_and_non_json_extension_types(store):
    assert store.validate({'text': ' ' * (MAX_PROFILE_BYTES + 1)})['valid'] is False
    with pytest.raises(PolicyError) as oversized:
        store.save({'text': ' ' * (MAX_PROFILE_BYTES + 1)}, create=True)
    assert oversized.value.status == 413
    recursive = document()
    recursive['extensions']['cycle'] = recursive
    assert store.validate({'document': recursive})['valid'] is False
    candidate = document()
    candidate['extensions']['unsupported'] = b'binary'
    assert store.validate({'document': candidate})['valid'] is False
    candidate['extensions']['unsupported'] = float('nan')
    assert store.validate({'document': candidate})['valid'] is False
    candidate['extensions']['unsupported'] = 10 ** 10000
    assert store.validate({'document': candidate})['valid'] is False
    candidate['extensions']['unsupported'] = '\ud800'
    assert store.validate({'document': candidate})['valid'] is False
    assert store.validate({'text': '{"value": ' + '9' * 5000 + '}'})['valid'] is False
    assert store.validate({'text': 'value: ' + '9' * 5000})['valid'] is False
    nested = {}
    parent = nested
    for _ in range(100):
        parent['child'] = {}
        parent = parent['child']
    assert store.validate({'document': nested})['valid'] is False


def test_source_root_itself_can_be_the_checkpoint(store):
    candidate = document()
    candidate['model']['relative_path'] = '.'
    assert store.validate({'document': candidate})['valid'] is True


def test_external_invalid_scalar_utf8_and_oversize_files_remain_visible(store):
    (store.directory / 'scalar.yaml').write_text('17\n')
    (store.directory / 'bad_utf8.yaml').write_bytes(b'\xff\x80')
    (store.directory / 'oversize.yaml').write_bytes(b' ' * (MAX_PROFILE_BYTES + 1))
    summaries = store.refresh()['profiles']
    assert len(summaries) == 3
    assert all(item['state'] == 'invalid' and not item['can_apply'] for item in summaries)


def test_symlinks_are_not_followed_and_cannot_be_overwritten(store, tmp_path):
    outside = tmp_path / 'outside.yaml'
    outside.write_text(RAW)
    (store.directory / 'cup_act.yaml').symlink_to(outside)
    with pytest.raises(PolicyError):
        store.get('cup_act')
    with pytest.raises(PolicyError):
        store.save({'text': RAW}, create=True)
    assert outside.read_text() == RAW
    assert store.list()['profiles'][0]['state'] == 'invalid'


def test_validator_supplies_metadata_but_never_runs_on_invalid_schema(store):
    seen = []
    def validator(candidate):
        seen.append(candidate)
        return {'valid': True, 'ready': False, 'errors': [],
                'warnings': [{'code': 'UNRESOLVED_MODEL', 'message': 'Model source is offline'}],
                'category': 'act', 'resolved_model': None}
    store.validator = validator
    saved = store.save({'text': RAW}, create=True)
    assert seen
    assert saved['validation']['valid'] and not saved['validation']['ready']
    assert saved['validation']['category'] == 'act'
    before = len(seen)
    assert not store.validate({'document': {'id': 'invalid'}})['valid']
    assert len(seen) == before


def test_failed_validator_does_not_claim_readiness(store):
    def unavailable(_candidate):
        raise RuntimeError('Private diagnostic must not be reflected')
    store.validator = unavailable
    result = store.validate({'text': RAW})
    assert result['valid'] is True and result['ready'] is False
    assert result['warnings'][0]['code'] == 'VALIDATOR_UNAVAILABLE'
    assert 'Private diagnostic' not in json.dumps(result)


def test_unresolved_readiness_errors_remain_saveable_and_survive_get(store):
    errors = [
        {'code': 'MODEL_NOT_FOUND', 'message': 'The referenced artifact is not currently available', 'path': 'model'},
        {'code': 'UNKNOWN_BINDING', 'message': 'The camera binding is unresolved', 'path': 'bindings.cameras'},
    ]
    store.validator = lambda _candidate: {'valid': True, 'ready': False, 'errors': errors, 'warnings': []}
    saved = store.save({'text': RAW}, create=True)
    reopened = store.get('cup_act')
    for detail in (saved, reopened):
        assert detail['validation']['valid'] is True
        assert detail['validation']['ready'] is False
        assert detail['validation']['errors'] == errors
        assert detail['text'] == RAW
    summary = store.list()['profiles'][0]
    assert summary['state'] == 'draft'
    assert summary['can_apply'] is False
    store.validator = lambda _candidate: {'valid': False, 'ready': False, 'errors': errors, 'warnings': []}
    with pytest.raises(PolicyError) as invalid:
        store.save({'text': RAW}, 'cup_act', saved['revision'])
    assert invalid.value.code == 'INVALID_PROFILE'


def test_missing_update_and_import_replace_cannot_create(store):
    with pytest.raises(PolicyError) as missing:
        store.save({'text': RAW}, 'cup_act', 'sha256:' + 'a' * 64)
    assert missing.value.status == 404
    with pytest.raises(PolicyError) as missing:
        store.import_profile({'text': RAW, 'id': 'cup_act', 'mode': 'replace', 'expected_revision': 'sha256:' + 'a' * 64})
    assert missing.value.status == 404
    assert store.list()['profiles'] == []
