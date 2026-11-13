"""File-owned, hardware-free policy discovery and content identity.

Discovery never imports checkpoint Python, torch, LeRobot, or processor code. A
catalog selection is a reference to files; native preparation is a separate step.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import threading
import time

import yaml

from .policy_common import PolicyError, identifier, relative_path, revision

MAX_DEPTH = 12
MAX_SCAN_ENTRIES = 50000
MAX_PACKAGE_FILES = 10000
MAX_METADATA_BYTES = 2 * 1024 * 1024
SUPPORTED_TYPES = frozenset({
    'act', 'diffusion', 'vqbet', 'tdmpc', 'pi0', 'pi0fast', 'pi0_fast',
    'pi05', 'smolvla', 'groot', 'xvla', 'wall_x', 'sarm',
})
IGNORED_DIRECTORIES = frozenset({'.station-uploads', '.git', '__pycache__'})
PROCESSOR_PAIRS = (
    ('policy_preprocessor.json', 'policy_postprocessor.json'),
    ('preprocessor.json', 'postprocessor.json'),
)


class _UniqueLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError('Duplicate YAML key: ' + str(key))
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def _load_yaml(text):
    return yaml.load(text, Loader=_UniqueLoader)


def _fingerprint(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _issue(code, message, path=None):
    result = {'code': code, 'message': message}
    if path is not None:
        result['path'] = path
    return result


class _UnstablePackage(ValueError):
    pass


class PolicyCatalog:
    def __init__(self, source_config: Path, cache_db: Path, *, on_event=None):
        self.source_config = Path(source_config).absolute()
        self.cache_db = Path(cache_db)
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='policy-catalog')
        self._closed = False
        self._on_event = on_event
        self._jobs = {}
        self._roots = []
        self._text = ''
        self._revision = revision(b'')
        self._profile_store = str(self.source_config.parent / 'policy-profiles')
        self._records = {}
        self._generation = 0
        self._load_error = None
        self.cache_db.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.cache_db), check_same_thread=False)
        self._db.execute('PRAGMA journal_mode=WAL')
        self._db.execute('CREATE TABLE IF NOT EXISTS hashes (fingerprint TEXT PRIMARY KEY, sha256 TEXT NOT NULL)')
        self._db.execute('CREATE TABLE IF NOT EXISTS models (id TEXT PRIMARY KEY, document TEXT NOT NULL)')
        for key, document in self._db.execute('SELECT id, document FROM models'):
            try:
                record = json.loads(document)
                if record.get('id') == key:
                    self._records[key] = record
            except (ValueError, TypeError):
                pass
        try:
            if self.source_config.exists():
                self.reload()
        except PolicyError as error:
            # An invalid optional catalog file must not take down the robot UI.
            # No candidate was adopted; reload can recover after a host-file edit.
            self._load_error = error.response()

    def _emit(self, event):
        if self._on_event is not None:
            try:
                self._on_event(deepcopy(event))
            except Exception:
                # Observers cannot invalidate durable catalog work.
                pass

    def _ensure_open(self):
        if self._closed:
            raise PolicyError('CATALOG_CLOSED', 'Policy catalog is closed', 503)

    def _candidate(self):
        try:
            with self.source_config.open('rb') as stream:
                data = stream.read(MAX_METADATA_BYTES + 1)
            if len(data) > MAX_METADATA_BYTES:
                raise ValueError('Source configuration exceeds metadata size limit')
            text = data.decode('utf-8')
            document = _load_yaml(text)
            if not isinstance(document, dict) or type(document.get('schema_version')) is not int or document.get('schema_version') != 1:
                raise ValueError('Source configuration requires schema_version: 1')
            if not isinstance(document.get('roots'), list) or len(document['roots']) > 128:
                raise ValueError('roots must be a list with at most 128 sources')
            if set(document)-{'schema_version','roots'}:raise ValueError('Source config accepts only schema_version and roots; profile store is startup-owned')
            roots, seen = [], set()
            for item in document['roots']:
                if not isinstance(item, dict):
                    raise ValueError('Each root must be a mapping')
                key = identifier(item.get('id'), 'Source ID')
                if key in seen:
                    raise ValueError('Duplicate source ID: ' + key)
                seen.add(key)
                path = item.get('path')
                if not isinstance(path, str) or not path.strip() or '\x00' in path or len(path) > 4096:
                    raise ValueError('Source path must be a nonempty host path')
                if not isinstance(item.get('label', key), str) or len(item.get('label', key)) > 256:
                    raise ValueError('Source label must be a bounded string')
                for flag in ('enabled', 'allow_upload'):
                    if type(item.get(flag, flag == 'enabled')) is not bool:
                        raise ValueError(flag + ' must be boolean')
                resolved = Path(path).expanduser()
                if not resolved.is_absolute():
                    resolved = self.source_config.parent / resolved
                roots.append({'id': key, 'label': item.get('label', key), 'path': path,
                              'resolved_path': str(resolved.resolve(strict=False)), '_configured_path': str(resolved),
                              'enabled': item.get('enabled', True), 'allow_upload': item.get('allow_upload', False)})
            return data, text, roots, self._profile_store
        except PolicyError:
            raise
        except (OSError, UnicodeError, yaml.YAMLError, ValueError, TypeError, RuntimeError) as error:
            raise PolicyError('INVALID_SOURCES', 'Source configuration was not adopted: ' + str(error),
                              details={'config_path': str(self.source_config), 'loaded_revision': self._revision}) from error

    def reload(self, expected_revision=None):
        with self._lock:
            self._ensure_open()
            if expected_revision is not None and expected_revision != self._revision:
                raise PolicyError('REVISION_CONFLICT', 'Loaded source revision changed', 409,
                                  {'revision': self._revision})
            data, text, roots, store = self._candidate()
            before = {item['id']: item for item in self._roots}
            after = {item['id']: item for item in roots}
            changed = [key for key in list(after) + [key for key in before if key not in after]
                       if before.get(key) != after.get(key)]
            self._text, self._revision, self._roots, self._profile_store = text, revision(data), roots, store
            self._generation += 1
            self._load_error = None
            job = self.scan([key for key in changed if key in after]) if any(key in after for key in changed) else None
            result = self.sources()
            result.update(changed_source_ids=changed, scan_job=job)
        self._emit({'kind': 'policy_sources', 'revision': self._revision})
        return result

    @staticmethod
    def _root_state(root):
        result = deepcopy(root)
        path = Path(root.get('_configured_path', root['resolved_path']))
        result.pop('_configured_path', None)
        result['resolved_path'] = root['resolved_path']
        result.update(online=False, writable=False, free_bytes=None, identity=None)
        try:
            result['resolved_path'] = str(path.resolve(strict=False))
            info = path.stat()
            if not stat.S_ISDIR(info.st_mode):
                raise NotADirectoryError(str(path))
            # Include the canonical path: mounting/repointing a source is not an upload continuation.
            result.update(online=True, identity={'device': info.st_dev, 'inode': info.st_ino,
                                                'resolved_path': str(path.resolve())},
                          writable=os.access(path, os.W_OK | os.X_OK), free_bytes=shutil.disk_usage(path).free)
            result.update(state='online' if root['enabled'] else 'disabled',
                          reason=None if root['enabled'] else 'Source is disabled')
        except (OSError, RuntimeError) as error:
            result.update(state='offline' if root['enabled'] else 'disabled',
                          reason='Source is offline: ' + str(error) if root['enabled'] else 'Source is disabled')
        return result

    def sources(self):
        with self._lock:
            return {'schema_version': 1, 'revision': self._revision, 'config_path': str(self.source_config),
                    'profile_store': self._profile_store, 'text': self._text,
                    'roots': [self._root_state(root) for root in self._roots],
                    'changed_source_ids': [], 'scan_job': None,'config_error':deepcopy(self._load_error)}

    def source(self, source_id):
        with self._lock:
            for root in self._roots:
                if root['id'] == source_id:
                    return self._root_state(root)
        raise PolicyError('SOURCE_NOT_FOUND', 'Unknown policy source', 404)

    def scan(self, source_ids=None):
        with self._lock:
            self._ensure_open()
            known = {item['id']: deepcopy(item) for item in self._roots}
            if source_ids is None:
                source_ids = list(known)
            if not isinstance(source_ids, list) or any(not isinstance(key, str) for key in source_ids):
                raise PolicyError('INVALID_SOURCES', 'source_ids must be a list of source IDs')
            if len(set(source_ids)) != len(source_ids):
                raise PolicyError('INVALID_SOURCES', 'source_ids must not contain duplicates')
            unknown = set(source_ids) - set(known)
            if unknown:
                raise PolicyError('SOURCE_NOT_FOUND', 'Unknown policy sources', 404, {'source_ids': sorted(unknown)})
            key = 'scan-' + os.urandom(12).hex()
            job = {'id': key, 'kind': 'scan', 'state': 'queued', 'phase': 'queued',
                   'progress': {'done': 0, 'total': len(source_ids)}, 'errors': [],
                   'started_at_ns': None, 'finished_at_ns': None}
            self._jobs[key] = job
            self._executor.submit(self._run_scan, key, [known[source_id] for source_id in source_ids])
            return deepcopy(job)

    def scan_status(self, job_id):
        with self._lock:
            if job_id not in self._jobs:
                raise PolicyError('SCAN_NOT_FOUND', 'Unknown policy scan', 404)
            return deepcopy(self._jobs[job_id])

    def _run_scan(self, job_id, roots):
        with self._lock:
            job = self._jobs[job_id]
            job.update(state='running', phase='discovering', started_at_ns=time.time_ns())
        try:
            for root in roots:
                status = self._root_state(root)
                found = None
                errors = []
                if status['state'] == 'online':
                    try:
                        found = self._discover(root, status['identity'])
                    except (OSError, ValueError, PolicyError) as error:
                        errors.append(_issue('SCAN_FAILED', str(error), root['id']))
                with self._lock:
                    current = next((item for item in self._roots if item['id'] == root['id']), None)
                    # A reload cannot be overwritten by the previous root's in-flight scan.
                    if current == root:
                        if found is not None:
                            new_ids = {item['id'] for item in found}
                            for key, old in self._records.items():
                                if old['source_id'] == root['id'] and key not in new_ids:
                                    old.update(state='missing', selectable=False, reason='Artifact is missing from its source')
                            self._records.update({item['id']: item for item in found})
                            self._persist_records()
                        elif status['state'] != 'online':
                            # Preserve descriptors and strong revisions while the drive is disconnected.
                            for item in self._records.values():
                                if item['source_id'] == root['id']:
                                    item.update(state=status['state'], selectable=False, reason=status['reason'])
                            self._persist_records()
                    else:
                        errors.append(_issue('SOURCE_CHANGED', 'Source changed while scan was running', root['id']))
                    job['errors'].extend(errors)
                    job['progress']['done'] += 1
            with self._lock:
                job.update(state='failed' if job['errors'] else 'complete', phase='complete',
                           finished_at_ns=time.time_ns(), result={'revision': self._catalog_revision(),
                                                                 'total': len(self._public_records())})
        except Exception as error:
            with self._lock:
                job['errors'].append(_issue('SCAN_FAILED', str(error)))
                job.update(state='failed', phase='failed', finished_at_ns=time.time_ns())
        self._emit({'kind': 'policy_scan', 'job': self.scan_status(job_id)})

    def _persist_records(self):
        self._db.executemany('INSERT OR REPLACE INTO models(id, document) VALUES(?, ?)',
                             [(key, json.dumps(value, sort_keys=True, allow_nan=False))
                              for key, value in self._records.items()])
        self._db.commit()

    @staticmethod
    def _is_package(path, names):
        return 'manifest.json' in names or 'config.json' in names or ('pretrained_model' in names and (path / 'pretrained_model' / 'config.json').is_file())

    def _discover(self, root, identity):
        base = Path(self._root_state(root)['resolved_path'])
        stack = [(base, 0)]
        visited = 0
        records = []
        while stack:
            path, depth = stack.pop()
            if path.is_symlink() or not path.resolve().is_relative_to(base):
                continue
            with os.scandir(path) as iterator:
                entries = []
                for entry in iterator:
                    visited += 1
                    if visited > MAX_SCAN_ENTRIES:
                        raise ValueError('Source exceeds bounded scan entry limit')
                    entries.append(entry)
            entries.sort(key=lambda entry: entry.name)
            safe = [entry for entry in entries if not entry.is_symlink() and entry.name not in IGNORED_DIRECTORIES]
            names = {entry.name for entry in safe}
            if self._is_package(path, names):
                package = self.inspect_package(path)
                rel = path.relative_to(base).as_posix()
                key = self._model_id(root['id'], rel)
                package.update(id=key, source_id=root['id'], relative_path=rel,
                               _source_identity=identity, _source_root=str(base))
                records.append(package)
                continue
            directories = [Path(entry.path) for entry in safe if entry.is_dir(follow_symlinks=False)]
            if directories and depth >= MAX_DEPTH:
                raise ValueError('Source exceeds bounded scan depth')
            stack.extend((directory, depth + 1) for directory in reversed(directories))
        if self._root_state(root)['identity'] != identity:
            raise ValueError('Source physical identity changed during scan')
        return records

    @staticmethod
    def _model_id(source_id, relative):
        return 'model-' + hashlib.sha256((source_id + '\x00' + relative).encode()).hexdigest()[:24]

    def _inventory(self, root):
        result, stack = {}, [(root, 0)]
        visited = 0
        while stack:
            directory, depth = stack.pop()
            if directory.is_symlink() or not directory.resolve().is_relative_to(root):
                raise ValueError('Package directory escaped its root')
            with os.scandir(directory) as iterator:
                entries = []
                for entry in iterator:
                    visited += 1
                    if visited > MAX_SCAN_ENTRIES:
                        raise ValueError('Package exceeds bounded entry count')
                    entries.append(entry)
            for entry in sorted(entries, key=lambda item: item.name):
                if entry.name in IGNORED_DIRECTORIES:
                    continue
                if entry.is_symlink():
                    raise ValueError('Package contains a symlink: ' + str(Path(entry.path).relative_to(root)))
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    if depth >= MAX_DEPTH:
                        raise ValueError('Package exceeds bounded directory depth')
                    stack.append((Path(entry.path), depth + 1))
                elif stat.S_ISREG(info.st_mode):
                    result[Path(entry.path).relative_to(root).as_posix()] = _fingerprint(info)
                    if len(result) > MAX_PACKAGE_FILES:
                        raise ValueError('Package exceeds bounded file count')
                else:
                    raise ValueError('Package contains a special file')
        return result

    def _hash_file(self, path, expected):
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or _fingerprint(info) != expected:
            raise _UnstablePackage('Artifact changed while being scanned: ' + path.name)
        key = json.dumps(expected)
        with self._lock:
            cached = self._db.execute('SELECT sha256 FROM hashes WHERE fingerprint=?', (key,)).fetchone()
        if cached:
            return cached[0]
        # O_NOFOLLOW rejects replacement by a link between inventory and open.
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        digest = hashlib.sha256()
        try:
            with os.fdopen(fd, 'rb') as stream:
                if _fingerprint(os.fstat(stream.fileno())) != expected:
                    raise _UnstablePackage('Artifact was replaced while hashing: ' + path.name)
                while data := stream.read(1024 * 1024):
                    digest.update(data)
                if _fingerprint(os.fstat(stream.fileno())) != expected:
                    raise _UnstablePackage('Artifact changed while hashing: ' + path.name)
            if _fingerprint(path.stat(follow_symlinks=False)) != expected:
                raise _UnstablePackage('Artifact was replaced while hashing: ' + path.name)
        except OSError as error:
            raise _UnstablePackage('Artifact became unavailable while hashing: ' + path.name) from error
        value = digest.hexdigest()
        with self._lock:
            self._db.execute('INSERT OR REPLACE INTO hashes(fingerprint, sha256) VALUES(?, ?)', (key, value))
            self._db.commit()
        return value

    @staticmethod
    def _metadata(root, name, inventory, *, yaml_file=False):
        if name not in inventory:
            return None
        if inventory[name][2] > MAX_METADATA_BYTES:
            raise ValueError('Metadata exceeds size limit: ' + name)
        path = root / name
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as stream:
            if _fingerprint(os.fstat(stream.fileno())) != inventory[name]:
                raise _UnstablePackage('Metadata changed while reading: ' + name)
            data = stream.read(MAX_METADATA_BYTES + 1)
            if _fingerprint(os.fstat(stream.fileno())) != inventory[name]:
                raise _UnstablePackage('Metadata changed while reading: ' + name)
        value = _load_yaml(data.decode()) if yaml_file else json.loads(data)
        # Keep the entire descriptor safe for the durable JSON cache/HTTP response.
        json.dumps(value, allow_nan=False)
        if not isinstance(value, (dict, list)):
            raise ValueError('Metadata must contain an object or list: ' + name)
        return value

    @staticmethod
    def _companion_paths(value):
        """Saved processor state references, never executable/imported content."""
        paths = []
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ('state_file', 'state_path') and isinstance(item, str):
                    paths.append(item)
                else:
                    paths.extend(PolicyCatalog._companion_paths(item))
        elif isinstance(value, list):
            for item in value:
                paths.extend(PolicyCatalog._companion_paths(item))
        return paths

    def inspect_package(self, path):
        """Return a strongly hashed descriptor, with completeness separate from support.

        Files stay in their original directory. Upload staging is only inspected;
        this routine never publishes, imports, deserializes weights, or prepares it.
        """
        root = Path(path).absolute()
        if root.is_symlink() or not root.is_dir():
            raise PolicyError('INVALID_PACKAGE', 'A real package directory is required')
        root = root.resolve()
        descriptor = {'id': None, 'name': root.name, 'category': None, 'format': None,
                      'source_id': None, 'relative_path': '.', 'resolved_path': str(root),
                      'artifact_revision': None, 'size_bytes': 0, 'state': 'incomplete',
                      'selectable': False, 'reason': None, 'issues': [], 'camera_inputs': [],
                      'state_features': [], 'action_features': [], 'action_fps': None,
                      'language_conditioned': None, 'calibration_revision': None, 'files': [],
                      'root': str(root), 'artifact_root': str(root), 'checkpoint_path': None,
                      'config': None, 'manifest': None, 'contract': {}, 'complete': False,
                      'supported': False, 'compatible': True, 'missing_files': [], 'inventory': []}
        issues = descriptor['issues']
        try:
            inventory = self._inventory(root)
            manifest = self._metadata(root, 'manifest.json', inventory)
            if manifest is not None and not isinstance(manifest, dict):
                raise ValueError('manifest.json must contain an object')
            descriptor['manifest'] = manifest
            prefix = 'pretrained_model/' if 'pretrained_model/config.json' in inventory else ''
            descriptor['format'] = 'station_bundle' if manifest is not None else 'lerobot_checkpoint'
            checkpoint = root / prefix
            descriptor['checkpoint_path'] = str(checkpoint)
            checkpoint_stat = checkpoint.stat()
            descriptor['_physical_key'] = [checkpoint_stat.st_dev, checkpoint_stat.st_ino]
            config = self._metadata(root, prefix + 'config.json', inventory)
            if config is not None and not isinstance(config, dict):
                raise ValueError('Policy config.json must contain an object')
            descriptor['config'] = config
            missing = descriptor['missing_files']
            if config is None:
                missing.append(prefix + 'config.json')
                config = {}
            category = config.get('type')
            if isinstance(category, str) and category.strip():
                descriptor['category'] = category.strip()
            descriptor['supported'] = True if descriptor['category'] in SUPPORTED_TYPES else None
            if not descriptor['category']:
                issues.append(_issue('POLICY_TYPE_REQUIRED', 'Policy config.type is missing'))
            elif descriptor['supported'] is None:
                issues.append(_issue('IMPLEMENTATION_UNVERIFIED', 'Custom policy implementation must be installed and checked during native preparation'))
            weight_names = ('model.safetensors', 'pytorch_model.bin')
            weight = next((prefix + name for name in weight_names if prefix + name in inventory and inventory[prefix + name][2] > 0), None)
            index_name = next((prefix + name for name in ('model.safetensors.index.json', 'pytorch_model.bin.index.json')
                               if prefix + name in inventory), None)
            if weight is None and index_name:
                index = self._metadata(root, index_name, inventory)
                if not isinstance(index, dict) or not isinstance(index.get('weight_map'), dict) or not index['weight_map']:
                    raise ValueError('Invalid checkpoint weight shard index')
                for name in set(index['weight_map'].values()):
                    name = prefix + relative_path(name)
                    if name not in inventory or inventory[name][2] == 0:
                        missing.append(name)
                weight = index_name
            if weight is None:
                missing.append(prefix + 'model.safetensors')
            pair = next((pair for pair in PROCESSOR_PAIRS if any(prefix + name in inventory for name in pair)), PROCESSOR_PAIRS[0])
            for processor_name in pair:
                name = prefix + processor_name
                if name not in inventory:
                    missing.append(name)
                    continue
                processor = self._metadata(root, name, inventory)
                for companion in self._companion_paths(processor):
                    companion = prefix + relative_path(companion)
                    if companion not in inventory or inventory[companion][2] == 0:
                        missing.append(companion)
            sidecar = self._metadata(root, 'station-policy.yaml', inventory, yaml_file=True) or {}
            if not isinstance(sidecar, dict):
                raise ValueError('station-policy.yaml must contain a mapping')
            if sidecar.get('schema_version', 1) != 1:
                issues.append(_issue('INCOMPATIBLE_REGISTRATION', 'Unsupported station-policy.yaml schema_version'))
                descriptor['compatible'] = False
            contract = sidecar.get('contract', sidecar)
            if not isinstance(contract, dict):
                raise ValueError('Registration contract must contain a mapping')
            trained = dict(contract)
            if manifest is not None:
                # Exported declarations take precedence over optional registration additions.
                trained.update({key: value for key, value in manifest.items() if key != 'files'})
                if type(manifest.get('schema_version')) is not int or manifest.get('schema_version') != 1 or manifest.get('lerobot_version') != '0.7.0':
                    issues.append(_issue('INCOMPATIBLE_MANIFEST', 'Bundle requires schema_version 1 and native LeRobot 0.7.0'))
                    descriptor['compatible'] = False
                if not isinstance(manifest.get('files'), dict) or not manifest['files']:
                    raise ValueError('Bundle manifest must provide a nonempty file digest mapping')
            # Registration only fills absent trained declarations. Explicit checkpoint
            # values remain authoritative, including unconditioned language metadata.
            for key in ('state_features', 'action_features', 'action_fps', 'language_conditioned', 'calibration_revision'):
                known = config.get(key)
                if key == 'action_fps' and known is None:
                    known = config.get('fps')
                if known is not None:
                    if key in contract and contract[key] != known:
                        issues.append(_issue('REGISTRATION_CONFLICT', 'Registration differs from checkpoint metadata', key))
                        descriptor['compatible'] = False
                    trained[key] = known
            descriptor['contract'] = deepcopy(trained)
            name = manifest.get('name') if manifest else None
            descriptor['name'] = name if isinstance(name, str) and name else sidecar.get('name', root.name)
            if not isinstance(descriptor['name'], str):
                descriptor['name'] = root.name
            self._trained_metadata(descriptor, config, trained)
            for name, expected in sorted(inventory.items()):
                digest = self._hash_file(root / name, expected)
                descriptor['files'].append({'path': name, 'size': expected[2], 'sha256': digest})
            if inventory != self._inventory(root):
                raise _UnstablePackage('Package inventory changed during scan; wait for the copy to finish')
            descriptor['size_bytes'] = sum(item['size'] for item in descriptor['files'])
            descriptor['inventory'] = deepcopy(descriptor['files'])
            descriptor['artifact_revision'] = revision(descriptor['files'])
            if manifest:
                actual = {item['path']: item['sha256'] for item in descriptor['files']}
                for name, digest in manifest['files'].items():
                    name = relative_path(name)
                    if name not in actual:
                        missing.append(name)
                    elif not isinstance(digest, str) or actual[name] != digest.removeprefix('sha256:').lower():
                        issues.append(_issue('MANIFEST_DIGEST_MISMATCH', 'Bundle file differs from its manifest', name))
                        descriptor['compatible'] = False
            descriptor['missing_files'] = sorted(set(missing))
            descriptor['complete'] = not descriptor['missing_files']
            for name in descriptor['missing_files']:
                issues.append(_issue('MISSING_COMPANION', 'Required checkpoint file is missing', name))
            if not descriptor['complete']:
                descriptor.update(state='incomplete', reason='Checkpoint package is incomplete')
            elif not descriptor['compatible']:
                descriptor.update(state='incompatible', reason='Checkpoint metadata or manifest is incompatible')
            elif not descriptor['category']:
                descriptor.update(state='unsupported', reason='Policy config.type is missing')
            elif descriptor['supported'] is None:
                descriptor.update(state='unverified', selectable=True, reason='Installed policy implementation is checked during preparation')
            else:
                descriptor.update(state='available', selectable=True, reason=None)
        except _UnstablePackage as error:
            descriptor.update(state='copying', reason=str(error), complete=False, selectable=False,
                              artifact_revision=None, files=[], inventory=[])
            issues.append(_issue('ARTIFACT_CHANGING', str(error)))
        except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError, PolicyError) as error:
            descriptor.update(state='invalid', reason=str(error), complete=False, selectable=False,
                              artifact_revision=None, files=[], inventory=[])
            issues.append(_issue('INVALID_PACKAGE', str(error)))
        return descriptor

    @staticmethod
    def _trained_metadata(descriptor, config, trained):
        issues = descriptor['issues']
        for key in ('state_features', 'action_features'):
            value = trained.get(key)
            if isinstance(value, list) and value and all(isinstance(item, dict) and
                    all(isinstance(item.get(field), str) and item[field] for field in ('name', 'joint', 'unit')) for item in value):
                descriptor[key] = deepcopy(value)
            else:
                issues.append(_issue('UNKNOWN_' + key.upper(), 'Trained named joint/unit mapping is not declared'))
        fps = trained.get('action_fps', config.get('fps'))
        if isinstance(fps, (int, float)) and not isinstance(fps, bool) and math.isfinite(fps) and fps > 0:
            descriptor['action_fps'] = fps
        else:
            issues.append(_issue('UNKNOWN_ACTION_FPS', 'Trained action cadence is not declared'))
        for key, kind in (('language_conditioned', bool), ('calibration_revision', str)):
            value = trained.get(key, config.get(key))
            if type(value) is kind and (kind is not str or value):
                descriptor[key] = value
            else:
                issues.append(_issue('UNKNOWN_' + key.upper(), key + ' is not declared'))
        cameras = {}
        inputs = config.get('input_features', {})
        if isinstance(inputs, dict):
            for key, value in inputs.items():
                if isinstance(value, dict) and (value.get('type') in ('VISUAL', 'visual', 'image') or key.startswith('observation.images.')):
                    shape = value.get('shape')
                    cameras[key] = {'key': key, 'role': None, 'masked': False,
                                    'shape': shape if isinstance(shape, list) and all(type(n) is int and n > 0 for n in shape) else None}
        # These virtual names are generated by the pinned native config classes,
        # not guessed from a filename or physical camera viewpoint.
        empty=config.get('empty_cameras',0)
        if config.get('type') in ('smolvla','pi05') and type(empty)==int and 0<empty<=16:
            for index in range(empty):
                key='observation.images.empty_camera_'+str(index)
                cameras[key]={'key':key,'role':None,'masked':True,'shape':cameras.get(key,{}).get('shape')}
        declarations = trained.get('camera_inputs', [])
        if isinstance(declarations, list):
            for value in declarations:
                if not isinstance(value, dict) or not isinstance(value.get('key'), str):
                    issues.append(_issue('INVALID_CAMERA_DECLARATION', 'camera_inputs require a key'))
                    continue
                key = value['key']
                item = cameras.setdefault(key, {'key': key, 'role': None, 'masked': False, 'shape': None})
                if isinstance(value.get('role'), str):
                    item['role'] = value['role']
                if type(value.get('masked')) is bool:
                    item['masked'] = value['masked']
                shape = value.get('shape')
                if item['shape'] is None and isinstance(shape, list) and all(type(n) is int and n > 0 for n in shape):
                    item['shape'] = shape
                elif shape is not None and shape != item['shape']:
                    issues.append(_issue('CAMERA_SHAPE_CONFLICT', 'Registration shape differs from checkpoint config', key))
                    descriptor['compatible'] = False
        # Exported `cameras` map declares feature-to-runtime IDs, not physical camera roles.
        exported = trained.get('cameras', {})
        if isinstance(exported, dict):
            for key in exported:
                if isinstance(key, str):
                    cameras.setdefault(key, {'key': key, 'role': None, 'masked': False, 'shape': None})
        descriptor['camera_inputs'] = list(cameras.values())
        for item in cameras.values():
            if item['role'] is None:
                issues.append(_issue('UNKNOWN_CAMERA_ROLE', 'Trained camera role is not declared', item['key']))
        if not cameras and not isinstance(inputs, dict):
            issues.append(_issue('UNKNOWN_CAMERA_INPUTS', 'Checkpoint input feature declarations are unavailable'))

    @staticmethod
    def _public(record):
        keys = ('id', 'name', 'category', 'format', 'source_id', 'relative_path', 'resolved_path',
                'artifact_revision', 'size_bytes', 'state', 'selectable', 'reason', 'issues', 'camera_inputs',
                'state_features', 'action_features', 'action_fps', 'language_conditioned', 'calibration_revision', 'files')
        return {key: deepcopy(record.get(key)) for key in keys}

    def _overlay(self, record):
        item = deepcopy(record)
        try:
            source = self.source(item['source_id'])
        except PolicyError:
            item.update(state='source_removed', selectable=False, reason='Source was removed from configuration')
            return item
        if source['state'] != 'online':
            item.update(state=source['state'], selectable=False, reason=source['reason'])
        elif source['resolved_path'] != item.get('_source_root') or source['identity'] != item.get('_source_identity'):
            item.update(state='source_changed', selectable=False, reason='Source identity changed; scan this source again')
        elif Path(item['resolved_path']).is_symlink() or not Path(item['resolved_path']).is_dir():
            item.update(state='missing', selectable=False, reason='Artifact is missing from its source')
        return item

    def _public_records(self):
        order = {root['id']: n for n, root in enumerate(self._roots)}
        records = sorted(self._records.values(), key=lambda item: (order.get(item['source_id'], 999), item['relative_path']))
        found, result = set(), []
        for record in records:
            item = self._overlay(record)
            # Overlapping roots can reference one physical checkpoint. Keep all IDs resolvable,
            # but count/display the live checkpoint once in the unfiltered catalog.
            physical = item.get('_physical_key')
            key = tuple(physical) if physical and item['state'] in ('available','unverified') else ('record', item['id'])
            if key in found:
                continue
            found.add(key)
            result.append(self._public(item))
        return result

    def _catalog_revision(self):
        return revision({'sources': self._revision, 'models': self._public_records()})

    def models(self, q='', source_id=None):
        if not isinstance(q, str) or len(q) > 1024:
            raise PolicyError('INVALID_QUERY', 'q must be a bounded string')
        with self._lock:
            if source_id is not None:
                self.source(source_id)
                records = [self._public(self._overlay(item)) for item in self._records.values() if item['source_id'] == source_id]
            else:
                records = self._public_records()
            query = q.casefold()
            records = [item for item in records if query in ' '.join(str(item.get(key) or '')
                       for key in ('name', 'category', 'relative_path', 'source_id')).casefold()]
            groups = [{'source_id': root['id'], 'label': root['label'], 'path': root['path'],
                       'state': self._root_state(root)['state'],
                       'models': [item for item in records if item['source_id'] == root['id']]}
                      for root in self._roots if source_id is None or root['id'] == source_id]
            return {'schema_version': 1, 'revision': self._catalog_revision(), 'models': records,
                    'groups': groups, 'total': len(records)}

    def get_model(self, model_id):
        with self._lock:
            if model_id not in self._records:
                raise PolicyError('MODEL_NOT_FOUND', 'Unknown policy model', 404)
            return self._public(self._overlay(self._records[model_id]))

    def resolve(self, reference, recheck=True):
        with self._lock:
            if isinstance(reference, str):
                key = reference
                expected_revision = None
            elif isinstance(reference, dict):
                source_id = reference.get('source_id')
                self.source(source_id)
                relative = '.' if reference.get('relative_path') == '.' else relative_path(reference.get('relative_path'))
                key = self._model_id(source_id, relative)
                expected_revision = reference.get('artifact_revision')
            else:
                raise PolicyError('INVALID_MODEL_REFERENCE', 'A catalog model reference is required')
            record = self._records.get(key)
            if record is None:
                raise PolicyError('MODEL_NOT_FOUND', 'Model reference is not in the catalog; scan its source', 404)
            record = self._overlay(record)
            if not record['selectable']:
                raise PolicyError('MODEL_UNAVAILABLE', record['reason'] or 'Model is not selectable', 409,
                                  {'model': self._public(record)})
            if expected_revision is not None and expected_revision != record['artifact_revision']:
                raise PolicyError('ARTIFACT_REVISION_CONFLICT', 'Artifact revision differs from the selected reference', 409,
                                  {'artifact_revision': record['artifact_revision']})
            identity = deepcopy(record.get('_source_identity'))
        if recheck:
            fresh = self.inspect_package(Path(record['root']))
            source = self.source(record['source_id'])
            if source['state'] != 'online' or source['identity'] != identity:
                raise PolicyError('SOURCE_CHANGED', 'Source became unavailable or changed identity', 409)
            if not fresh['selectable'] or fresh['artifact_revision'] != record['artifact_revision']:
                raise PolicyError('ARTIFACT_REVISION_CONFLICT', 'Artifact changed since discovery; scan before selecting it', 409,
                                  {'artifact_revision': fresh['artifact_revision'], 'state': fresh['state']})
            fresh.update({key: record[key] for key in ('id', 'source_id', 'relative_path', '_source_identity', '_source_root')})
            return fresh
        return deepcopy(record)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=False)
        with self._lock:
            self._db.close()
