"""Station profile schema and a single, revision-checked YAML file store.

This module never imports a model, contacts an inference host, or touches devices.
Raw YAML is authoritative. Structured edits overlay supported optional fields on
the current round-trip tree; explicit null removes an optional schema field.
Raw edits can remove any field, including arbitrary extension members.
"""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import uuid

from ruamel.yaml import YAML
from ruamel.yaml.events import AliasEvent, MappingEndEvent, MappingStartEvent, SequenceEndEvent, SequenceStartEvent
from ruamel.yaml.error import YAMLError

from .policy_common import PolicyError, atomic_write, identifier, relative_path, revision


MAX_PROFILE_BYTES = 64 * 1024
_MAX_DEPTH = 32
_MAX_NODES = 8192
_STANDARD_TAGS = {'tag:yaml.org,2002:' + name for name in ('map', 'seq', 'str', 'int', 'float', 'bool', 'null')}
_TOP_KEYS = {'schema_version', 'id', 'name', 'execution', 'model', 'bindings', 'contract', 'settings', 'extensions'}
_SETTINGS = {'startup_timeout_s', 'timeout_s', 'action_source_max_age_s', 'max_image_age_ms'}
_OPTIONAL = {
    (): {'contract', 'settings', 'extensions'},
    ('execution',): {'host_label', 'connection_profile', 'instance'},
    ('model',): {'artifact_revision'},
    ('bindings',): {'state', 'actions'},
    ('contract',): {'state_features', 'action_features', 'action_fps', 'calibration_revision', 'language_conditioned', 'camera_inputs'},
    ('settings',): _SETTINGS,
}


def _error(code, message, path=None, **extra):
    item = {'code': code, 'message': message}
    if path is not None:
        item['path'] = path
    item.update(extra)
    return item


def _validation(errors=None, warnings=None):
    errors = list(errors or [])
    return {'valid': not errors, 'ready': False, 'errors': errors, 'warnings': list(warnings or []),
            'category': None, 'resolved_model': None}


def _invalid(message, *, path=None, line=None, column=None, code='INVALID_PROFILE'):
    item = _error(code, message, path)
    if line is not None:
        item['line'] = line
    if column is not None:
        item['column'] = column
    return PolicyError(code, message, details={'errors': [item]})


def _yaml():
    parser = YAML(typ='rt')
    parser.preserve_quotes = True
    parser.allow_duplicate_keys = False
    parser.default_flow_style = False
    parser.width = 100
    return parser


def _plain(value, *, _active=None, _count=None, _depth=0, _path=''):
    """Bound and normalize the YAML/JSON data model, including extension values."""
    active = set() if _active is None else _active
    count = [0] if _count is None else _count
    count[0] += 1
    if count[0] > _MAX_NODES or _depth > _MAX_DEPTH:
        raise _invalid('Profile structure exceeds the supported size or nesting limit', path=_path)
    if value is None or type(value) is bool:
        return value
    if isinstance(value, str):
        try:
            value.encode('utf-8')
        except UnicodeError:
            raise _invalid('Profile strings must contain valid UTF-8 text', path=_path) from None
        return str(value)
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            raise _invalid('Numeric values must fit the supported finite range', path=_path)
        return int(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _invalid('Nonfinite numeric values are not allowed', path=_path)
        return float(value)
    if isinstance(value, (dict, list)):
        if id(value) in active:
            raise _invalid('Recursive YAML aliases or document references are not allowed', path=_path)
        active.add(id(value))
        try:
            options = {'_active': active, '_count': count, '_depth': _depth + 1}
            if isinstance(value, dict):
                result = {}
                for key, item in value.items():
                    if not isinstance(key, str):
                        raise _invalid('All profile mapping keys must be strings', path=_path)
                    key = str(key)
                    try:
                        key.encode('utf-8')
                    except UnicodeError:
                        raise _invalid('Profile mapping keys must contain valid UTF-8 text', path=_path) from None
                    result[key] = _plain(item, _path=(_path + '.' if _path else '') + key, **options)
                return result
            return [_plain(item, _path=f'{_path}[{index}]', **options) for index, item in enumerate(value)]
        finally:
            active.remove(id(value))
    raise _invalid('Only YAML/JSON strings, finite numbers, booleans, null, lists and mappings are allowed', path=_path)


def _json_text(text):
    """Recognize JSON, while rejecting its commonly accepted nonfinite constants."""
    if not text.lstrip().startswith(('{', '[')):
        return False
    def constant(_value):
        raise _invalid('Nonfinite numeric values are not allowed')
    try:
        value = json.loads(text, parse_constant=constant)
        _plain(value)
        return True
    except (json.JSONDecodeError, UnicodeError):
        return False
    except PolicyError:
        raise
    except (ValueError, OverflowError, RecursionError):
        raise _invalid('JSON structure or numeric values exceed the supported limits') from None


def parse_profile(text):
    """Parse bounded, safe round-trip YAML/JSON; schema validation is separate."""
    if not isinstance(text, str):
        raise _invalid('Profile text must be a UTF-8 string', path='text')
    try:
        encoded = text.encode('utf-8')
    except UnicodeError:
        raise _invalid('Profile text must be valid UTF-8', path='text') from None
    if len(encoded) > MAX_PROFILE_BYTES:
        raise PolicyError('PROFILE_TOO_LARGE', 'Profile text exceeds the 64 KiB limit', 413)
    _json_text(text)
    try:
        # Reject unsafe/custom tags before construction, and recursive aliases
        # before ruamel can collapse them to an incomplete round-trip tree.
        stack = []
        aliases = 0
        for event in _yaml().parse(text):
            tag = getattr(event, 'tag', None)
            if tag is not None and tag not in _STANDARD_TAGS:
                raise _invalid('Unsupported or unsafe YAML tags are not allowed', line=event.start_mark.line + 1)
            if isinstance(event, AliasEvent):
                aliases += 1
                if event.anchor in stack:
                    raise _invalid('Recursive YAML aliases are not allowed', line=event.start_mark.line + 1)
                if aliases > 64:
                    raise _invalid('Too many YAML aliases', line=event.start_mark.line + 1)
            elif isinstance(event, (MappingStartEvent, SequenceStartEvent)):
                stack.append(event.anchor)
                if len(stack) > _MAX_DEPTH:
                    raise _invalid('Profile structure exceeds the supported nesting limit', line=event.start_mark.line + 1)
            elif isinstance(event, (MappingEndEvent, SequenceEndEvent)):
                stack.pop()
        document = _yaml().load(text)
        _plain(document)
        return document
    except YAMLError as exc:
        mark = getattr(exc, 'problem_mark', None)
        raise _invalid('Invalid YAML syntax or duplicate mapping key',
                       line=mark.line + 1 if mark else None,
                       column=mark.column + 1 if mark else None) from None
    except (RecursionError, OverflowError):
        raise _invalid('Profile structure exceeds the supported limits') from None
    except PolicyError:
        raise
    except (ValueError, TypeError):
        raise _invalid('Invalid YAML scalar or mapping structure') from None


def _dump(document):
    stream = io.StringIO()
    _yaml().dump(document, stream)
    text = stream.getvalue()
    if len(text.encode('utf-8')) > MAX_PROFILE_BYTES:
        raise PolicyError('PROFILE_TOO_LARGE', 'Profile text exceeds the 64 KiB limit', 413)
    return text


def _line_for_path(document, path):
    current = document
    line = None
    for segment in re.findall(r'[^.\[\]]+|\[\d+\]', path or ''):
        try:
            if segment.startswith('['):
                key = int(segment[1:-1])
                if hasattr(current, 'lc'):
                    line = current.lc.item(key)[0] + 1
            else:
                key = segment
                if hasattr(current, 'lc'):
                    line = current.lc.key(key)[0] + 1
            current = current[key]
        except (KeyError, IndexError, TypeError, AttributeError):
            break
    return line


def validate_document(document):
    """Validate the Station v1 schema without resolving sources or resources."""
    try:
        value = _plain(document)
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')) > MAX_PROFILE_BYTES:
            return _validation([_error('PROFILE_TOO_LARGE', 'Profile document exceeds the 64 KiB limit')])
    except PolicyError as exc:
        return _validation((exc.details or {}).get('errors', [_error(exc.code, exc.message)]))
    errors = []
    def add(path, message, code='INVALID_FIELD'):
        item = _error(code, message, path)
        line = _line_for_path(document, path)
        if line is not None:
            item['line'] = line
        errors.append(item)
    def mapping(item, path, allowed, required=()):
        if not isinstance(item, dict):
            add(path, 'A mapping is required')
            return False
        for key in item:
            if key not in allowed:
                add((path + '.' if path else '') + key, 'Unsupported schema field', 'UNKNOWN_FIELD')
        for key in required:
            if key not in item:
                add((path + '.' if path else '') + key, 'Required field is missing', 'REQUIRED_FIELD')
        return True
    def string(item, path, limit=256, allow_null=False):
        if item is None and allow_null:
            return
        if not isinstance(item, str) or not item.strip() or len(item) > limit or any(ord(char) < 32 for char in item):
            add(path, f'A nonempty string of at most {limit} characters is required')
    def resource(item, path):
        try:
            identifier(item)
        except PolicyError:
            add(path, 'A resource ID must start with a lowercase letter and contain at most 64 letters, digits, underscores or hyphens')
    def number(item, path):
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item) or item <= 0:
            add(path, 'A positive finite number is required')
    if not mapping(value, '', _TOP_KEYS, ('schema_version', 'id', 'name', 'execution', 'bindings')):
        return _validation(errors)
    if ('schema_version' not in value or 'id' not in value) and any(key in value for key in ('deployment', 'zenoh', 'features', 'action_feature', 'semantics')):
        return _validation([_error('WRONG_PROFILE_FORMAT', 'This appears to be a native LeRobot policy-server deployment configuration. Import a Station execution profile with schema_version, id, execution and bindings instead.')])
    if type(value.get('schema_version')) is not int or value.get('schema_version') != 1:
        add('schema_version', 'Only Station profile schema_version 1 is supported')
    resource(value.get('id'), 'id')
    string(value.get('name'), 'name', 200)
    execution = value.get('execution')
    kind = execution.get('kind') if isinstance(execution, dict) else None
    if kind == 'local':
        if mapping(execution, 'execution', {'kind', 'device'}, ('kind', 'device')):
            if execution.get('device') not in ('cpu', 'cuda', 'mps', 'auto'):
                add('execution.device', 'Device must be cpu, cuda, mps or auto')
        model = value.get('model')
        if mapping(model, 'model', {'source_id', 'relative_path', 'artifact_revision'}, ('source_id', 'relative_path')):
            resource(model.get('source_id'), 'model.source_id')
            try:
                relative_path(model.get('relative_path'), allow_dot=True)
            except PolicyError:
                add('model.relative_path', 'A bounded path relative to the configured source is required')
            if 'artifact_revision' in model:
                if not isinstance(model['artifact_revision'], str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', model['artifact_revision']):
                    add('model.artifact_revision', 'Artifact revision must be a sha256 content digest')
    elif kind == 'remote':
        allowed = {'kind', 'endpoint', 'deployment', 'expected_artifact', 'host_label', 'connection_profile', 'instance'}
        if mapping(execution, 'execution', allowed, ('kind', 'endpoint', 'deployment', 'expected_artifact')):
            for key in ('endpoint', 'deployment', 'expected_artifact', 'host_label', 'connection_profile', 'instance'):
                if key in execution:
                    string(execution[key], 'execution.' + key, 2048 if key == 'endpoint' else 256)
        if 'model' in value:
            add('model', 'Remote profiles must not contain a local model reference')
    else:
        if isinstance(execution, dict):
            add('execution.kind', 'Execution kind must be local or remote')
        else:
            add('execution', 'An execution mapping is required')
    bindings = value.get('bindings')
    if mapping(bindings, 'bindings', {'arm', 'cameras', 'state', 'actions'}, ('arm', 'cameras')):
        resource(bindings.get('arm'), 'bindings.arm')
        for field in ('cameras', 'state', 'actions'):
            if field not in bindings:
                continue
            assignments = bindings[field]
            if not isinstance(assignments, dict):
                add('bindings.' + field, 'Bindings must map feature names to resource or joint names')
                continue
            if len(assignments) > 256:
                add('bindings.' + field, 'At most 256 bindings are supported')
            for feature, target in assignments.items():
                string(feature, 'bindings.' + field, 256)
                if field == 'cameras':
                    resource(target, 'bindings.' + field + '.' + feature)
                else:
                    string(target, 'bindings.' + field + '.' + feature, 128)
    if 'contract' in value:
        contract = value['contract']
        fields = {'state_features', 'action_features', 'action_fps', 'calibration_revision', 'language_conditioned', 'camera_inputs'}
        if mapping(contract, 'contract', fields):
            for field in ('state_features', 'action_features', 'camera_inputs'):
                if field not in contract:
                    continue
                items = contract[field]
                if not isinstance(items, list) or len(items) > 256:
                    add('contract.' + field, 'A list of at most 256 feature declarations is required')
                    continue
                seen = set()
                for index, item in enumerate(items):
                    path = f'contract.{field}[{index}]'
                    image = field == 'camera_inputs'
                    key = 'key' if image else 'name'
                    allowed = {'key', 'shape', 'required', 'masked', 'role'} if image else {'name', 'joint', 'unit'}
                    if not mapping(item, path, allowed, ('key',) if image else ('name', 'joint', 'unit')):
                        continue
                    string(item.get(key), path + '.' + key)
                    if isinstance(item.get(key), str):
                        if item[key] in seen:
                            add(path + '.' + key, 'Feature declarations must have unique names')
                        seen.add(item[key])
                    if image:
                        if 'shape' in item and (not isinstance(item['shape'], list) or not 1 <= len(item['shape']) <= 8 or any(type(n) is not int or not 1 <= n <= 65536 for n in item['shape'])):
                            add(path + '.shape', 'Shape must contain 1 to 8 positive integer dimensions')
                        for boolean in ('required', 'masked'):
                            if boolean in item and type(item[boolean]) is not bool:
                                add(path + '.' + boolean, 'A boolean is required')
                        if 'role' in item:
                            string(item['role'], path + '.role', 128, allow_null=True)
                    else:
                        string(item.get('joint'), path + '.joint', 128)
                        if item.get('unit') not in ('radian', 'degree', 'percent'):
                            add(path + '.unit', 'Unit must be radian, degree or percent')
            if 'action_fps' in contract:
                number(contract['action_fps'], 'contract.action_fps')
            if 'calibration_revision' in contract:
                string(contract['calibration_revision'], 'contract.calibration_revision')
            if 'language_conditioned' in contract and type(contract['language_conditioned']) is not bool:
                add('contract.language_conditioned', 'A boolean is required')
    if 'settings' in value and mapping(value['settings'], 'settings', _SETTINGS):
        for field, item in value['settings'].items():
            if field in _SETTINGS:
                number(item, 'settings.' + field)
    if 'extensions' in value and not isinstance(value['extensions'], dict):
        add('extensions', 'Extensions must be a mapping of declared data; extensions are never executed')
    return _validation(errors)


def _payload(payload, extras=()):
    if not isinstance(payload, dict):
        raise _invalid('Request must be an object containing exactly one of text or document')
    if ('text' in payload) == ('document' in payload):
        raise _invalid('Provide exactly one of text or document')
    if set(payload) - {'text', 'document', *extras}:
        raise _invalid('Unsupported request field')
    if 'text' in payload:
        text = payload['text']
        document = parse_profile(text)
        return document, text, False
    _plain(payload['document'])
    document = deepcopy(payload['document'])
    return document, None, True


def _overlay(old, new, path=()):
    """Keep comments/advanced values while allowing precise form removal by null."""
    if isinstance(old, dict) and isinstance(new, dict):
        keep = _OPTIONAL.get(path, set())
        if len(path) == 3 and path[:2] == ('contract', 'camera_inputs'):
            keep = {'shape', 'required', 'masked', 'role'}
        extensions = path[:1] == ('extensions',)
        result = deepcopy(old)
        for key in list(result):
            if key not in new and key not in keep and not extensions:
                del result[key]
        for key, value in new.items():
            optional = key in keep or (path == () and key == 'model')
            if value is None and optional and not extensions:
                result.pop(key, None)
            elif key in result:
                result[key] = _overlay(result[key], value, (*path, key))
            else:
                result[key] = deepcopy(value)
        return result
    if isinstance(old, list) and isinstance(new, list):
        result = deepcopy(old)
        def identity(item):
            if isinstance(item, dict):
                for field in ('key', 'name'):
                    if isinstance(item.get(field), str):
                        return field, item[field]
            return None
        identified = {identity(item): item for item in old if identity(item) is not None}
        merged = []
        for index, value in enumerate(new):
            item_id = identity(value)
            previous = identified.get(item_id) if item_id is not None else (old[index] if index < len(old) else None)
            merged.append(_overlay(previous, value, (*path, str(index))) if previous is not None else deepcopy(value))
        result[:] = merged
        return result
    return deepcopy(new)


def _structured_document(old, new):
    document = _overlay(old, new)
    # A form switching variants must not resurrect fields from the old variant.
    if isinstance(document, dict) and isinstance(document.get('execution'), dict):
        new_execution = new.get('execution', {}) if isinstance(new, dict) else {}
        old_execution = old.get('execution', {}) if isinstance(old, dict) else {}
        if isinstance(new_execution, dict) and new_execution.get('kind') != old_execution.get('kind'):
            document['execution'] = _overlay({}, new_execution, ('execution',))
        if document['execution'].get('kind') == 'remote' and 'model' not in new:
            document.pop('model', None)
    return document


class ProfileStore:
    """Canonical <id>.yaml files; every replacement requires a content revision."""

    def __init__(self, directory, *, validator=None):
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.validator = validator
        self._thread_lock = threading.RLock()

    @contextmanager
    def _locked(self):
        with self._thread_lock:
            path = self.directory / '.profiles.lock'
            flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
            descriptor = os.open(path, flags, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    def _path(self, profile_id):
        return self.directory / (identifier(profile_id, 'Profile ID') + '.yaml')

    def _read(self, profile_id):
        path = self._path(profile_id)
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        except FileNotFoundError:
            raise PolicyError('PROFILE_NOT_FOUND', 'The profile does not exist', 404) from None
        except OSError:
            raise PolicyError('INVALID_PROFILE_PATH', 'A profile must be a regular canonical YAML file') from None
        with os.fdopen(descriptor, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise PolicyError('INVALID_PROFILE_PATH', 'A profile must be a regular canonical YAML file')
            data = stream.read(MAX_PROFILE_BYTES + 1)
        if len(data) > MAX_PROFILE_BYTES:
            raise PolicyError('PROFILE_TOO_LARGE', 'Profile file exceeds the 64 KiB limit', 413)
        return data

    def _validate(self, document):
        result = validate_document(document)
        if not result['valid']:
            return result
        if self.validator is None:
            result['warnings'].append(_error('READINESS_UNCHECKED', 'Artifact and resource readiness has not been checked'))
            return result
        try:
            external = self.validator(_plain(document))
            if not isinstance(external, dict):
                raise TypeError('Validator must return a validation record')
            result['valid'] = result['valid'] and bool(external.get('valid', True))
            result['ready'] = result['valid'] and bool(external.get('ready', False))
            result['errors'].extend(deepcopy(external.get('errors', [])))
            result['warnings'].extend(deepcopy(external.get('warnings', [])))
            result['category'] = external.get('category')
            result['resolved_model'] = deepcopy(external.get('resolved_model'))
            if result['errors']:
                # Reference/binding errors can describe a saveable draft. Only
                # structural validation or an explicit callback valid:false
                # rejects the document; any errors still prevent readiness.
                result['ready'] = False
        except Exception:
            # Offline metadata or a validator failure must never fake readiness.
            result['ready'] = False
            result['warnings'].append(_error('VALIDATOR_UNAVAILABLE', 'Artifact and resource validation is currently unavailable'))
        return result

    def validate(self, payload):
        try:
            document, _text, _structured = _payload(payload)
            return self._validate(document)
        except PolicyError as exc:
            return _validation((exc.details or {}).get('errors', [_error(exc.code, exc.message)]))

    def _detail(self, profile_id, data):
        text = data.decode('utf-8', errors='replace')
        try:
            if text.encode('utf-8') != data:
                raise _invalid('Profile file must contain valid UTF-8')
            document = parse_profile(text)
            validation = self._validate(document)
            plain = _plain(document)
            if isinstance(plain, dict) and plain.get('id') != profile_id:
                validation['errors'].append(_error('PROFILE_ID_MISMATCH', 'The profile ID must match its canonical filename', 'id'))
                validation['valid'] = validation['ready'] = False
        except PolicyError as exc:
            document = None
            plain = None
            validation = _validation((exc.details or {}).get('errors', [_error(exc.code, exc.message)]))
        return {'schema_version': 1, 'id': profile_id,
                'name': plain.get('name', profile_id) if isinstance(plain, dict) else profile_id,
                'revision': revision(data), 'text': text, 'document': plain, 'validation': validation}

    def get(self, profile_id):
        return self._detail(profile_id, self._read(profile_id))

    @staticmethod
    def _summary(detail):
        document = detail.get('document')
        if not isinstance(document, dict):
            document = {}
        validation = detail['validation']
        execution = document.get('execution') or {}
        bindings = document.get('bindings') or {}
        cameras = bindings.get('cameras') if isinstance(bindings, dict) else None
        issues = validation['errors'] or validation['warnings']
        return {'id': detail['id'], 'name': detail['name'], 'revision': detail['revision'],
                'category': validation['category'], 'execution': execution,
                'model_reference': document.get('model'),
                'camera_summary': [{'key': key, 'camera_id': value} for key, value in sorted(cameras.items())] if isinstance(cameras, dict) else [],
                'state': 'invalid' if not validation['valid'] else ('ready' if validation['ready'] else 'draft'),
                'reason': issues[0].get('message') if issues else None,
                'can_apply': validation['valid'] and validation['ready'], 'applied': False}

    def list(self):
        profiles = []
        with self._locked():
            for path in sorted(self.directory.glob('*.yaml')):
                try:
                    identifier(path.stem)
                except PolicyError:
                    continue
                try:
                    profiles.append(self._summary(self.get(path.stem)))
                except PolicyError as exc:
                    if exc.status == 404:
                        continue
                    invalid = {'id': path.stem, 'name': path.stem, 'revision': None, 'document': None,
                               'validation': _validation([_error(exc.code, exc.message)])}
                    profiles.append(self._summary(invalid))
        return {'schema_version': 1, 'profiles': profiles,
                'revision': revision([[item['id'], item['revision'], item['state']] for item in profiles])}

    def refresh(self):
        # There is no cached second authority: external edits are always reread.
        return self.list()

    def _conflict(self, profile_id, *, required=False):
        try:
            detail = self.get(profile_id)
            details = {'revision': detail['revision'], 'text': detail['text'], 'document': detail['document']}
        except PolicyError:
            details = {'revision': None, 'text': None}
        details['id'] = profile_id
        return PolicyError('REVISION_REQUIRED' if required else 'REVISION_CONFLICT',
                           'An expected profile revision is required' if required else 'The profile changed or the destination already exists',
                           400 if required else 409, details)

    def _create(self, path, data, profile_id):
        # Link a fully flushed temporary file to create atomically without ever
        # replacing a concurrently created destination (even outside our lock).
        temporary = self.directory / ('.profile-' + uuid.uuid4().hex + '.pending')
        try:
            atomic_write(temporary, data)
            try:
                os.link(temporary, path)
            except FileExistsError:
                raise self._conflict(profile_id) from None
            directory = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def save(self, payload, profile_id=None, expected_revision=None, create=False):
        try:
            document, raw, structured = _payload(payload, ('expected_revision',))
        except PolicyError as exc:
            errors = (exc.details or {}).get('errors', [_error(exc.code, exc.message)])
            exc.details = {**(exc.details or {}), 'validation': _validation(errors),
                           'text': payload.get('text') if isinstance(payload, dict) else None}
            raise
        expected_revision = expected_revision if expected_revision is not None else payload.get('expected_revision')
        if not isinstance(document, dict):
            result = self._validate(document)
            raise PolicyError('INVALID_PROFILE', 'The profile is not schema-valid', details={'validation': result, 'text': raw})
        document_id = document.get('id')
        try:
            identifier(document_id, 'Profile ID')
        except PolicyError:
            raise PolicyError('INVALID_PROFILE', 'The profile is not schema-valid',
                              details={'validation': self._validate(document), 'text': raw}) from None
        addressed = profile_id is not None
        profile_id = document_id if profile_id is None else identifier(profile_id, 'Profile ID')
        if document_id != profile_id:
            raise PolicyError('PROFILE_ID_MISMATCH', 'Profile ID must match the requested canonical filename')
        path = self._path(profile_id)
        with self._locked():
            exists = path.exists() or path.is_symlink()
            current = None
            if exists:
                if create:
                    raise self._conflict(profile_id)
                if expected_revision is None:
                    raise self._conflict(profile_id, required=True)
                current = self._read(profile_id)
                if revision(current) != expected_revision:
                    raise self._conflict(profile_id)
                if structured:
                    try:
                        original = parse_profile(current.decode('utf-8'))
                    except (PolicyError, UnicodeError):
                        original = {}
                    document = _structured_document(original, document)
            elif addressed and not create:
                raise PolicyError('PROFILE_NOT_FOUND', 'The profile does not exist', 404)
            elif expected_revision is not None:
                raise self._conflict(profile_id)
            result = self._validate(document)
            if not result['valid']:
                raise PolicyError('INVALID_PROFILE', 'The profile is not schema-valid',
                                  details={'validation': result, 'text': raw})
            if structured or (raw is not None and _json_text(raw)):
                if raw is not None:
                    # JSON has no comments or layout to retain; emit block YAML.
                    document = _plain(document)
                text = _dump(document)
            else:
                text = raw
            data = text.encode('utf-8')
            if current is not None:
                # Catch a non-cooperating editor that changed the file while the
                # reference validator or round-trip serializer was working.
                try:
                    unchanged = self._read(profile_id) == current
                except PolicyError:
                    unchanged = False
                if not unchanged:
                    raise self._conflict(profile_id)
                atomic_write(path, data)
            else:
                self._create(path, data, profile_id)
            return self._detail(profile_id, data)

    def import_preview(self, payload):
        try:
            document, _raw, _structured = _payload(payload)
            validation = self._validate(document)
            profile = _plain(document)
        except PolicyError as exc:
            validation = _validation((exc.details or {}).get('errors', [_error(exc.code, exc.message)]))
            profile = None
        result = {'schema_version': 1, 'valid': validation['valid'], 'profile': profile,
                  'errors': validation['errors'], 'warnings': validation['warnings']}
        if isinstance(profile, dict):
            try:
                current = self.get(profile.get('id'))
                result['conflict'] = {'revision': current['revision'], 'text': current['text']}
            except PolicyError:
                pass
        return result

    def import_profile(self, payload):
        document, raw, structured = _payload(payload, ('id', 'mode', 'expected_revision'))
        profile_id = identifier(payload.get('id'), 'Import profile ID')
        mode = payload.get('mode')
        if mode not in ('new', 'replace'):
            raise PolicyError('INVALID_IMPORT_MODE', 'Import mode must be new or replace')
        if not isinstance(document, dict):
            raise PolicyError('INVALID_PROFILE', 'A Station execution profile mapping is required')
        if document.get('id') != profile_id:
            document['id'] = profile_id
            raw = _dump(document)
        if mode == 'replace':
            if not (self._path(profile_id).exists() or self._path(profile_id).is_symlink()):
                raise PolicyError('PROFILE_NOT_FOUND', 'The profile to replace does not exist', 404)
            if payload.get('expected_revision') is None:
                raise self._conflict(profile_id, required=True)
        content = {'document': document} if structured else {'text': raw}
        return self.save(content, profile_id=profile_id, expected_revision=payload.get('expected_revision'), create=mode == 'new')

    def export(self, profile_id, format='yaml'):
        if format not in ('yaml', 'json'):
            raise PolicyError('INVALID_EXPORT_FORMAT', 'Export format must be yaml or json')
        detail = self.get(profile_id)
        if format == 'yaml':
            return detail['text'], 'application/yaml; charset=utf-8'
        if not detail['validation']['valid']:
            raise PolicyError('INVALID_PROFILE', 'Invalid profiles can only be exported as their original YAML text',
                              details={'validation': detail['validation']})
        return json.dumps(detail['document'], indent=2, ensure_ascii=False, allow_nan=False) + '\n', 'application/json; charset=utf-8'
