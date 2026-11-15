"""Shared, leased ROS graph activity observation; never issues robot commands.

Subscription changes belong to the observer background thread. HTTP requests only
change leases; callbacks retain bounded counters, not serialized message bodies.
"""
import copy
import math
import threading
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Mapping


BLOCKED_TYPES = {
    'sensor_msgs/msg/Image', 'sensor_msgs/msg/CompressedImage',
    'sensor_msgs/msg/PointCloud', 'sensor_msgs/msg/PointCloud2',
    'nav_msgs/msg/OccupancyGrid', 'std_msgs/msg/ByteMultiArray',
    'std_msgs/msg/UInt8MultiArray',
}
STATUS_TYPE = 'action_msgs/msg/GoalStatusArray'
STATUS_NAMES = ('unknown', 'accepted', 'executing', 'canceling', 'succeeded', 'canceled', 'aborted')


def _number(value, integer=False):
    if isinstance(value, bool) or not isinstance(value, int if integer else (int, float)):
        return False
    return value >= 0 and (isinstance(value, int) or math.isfinite(value))


def _gid(value):
    try:
        if isinstance(value, str):
            result = bytes.fromhex(value)
        else:
            result = bytes(value)
        return result.hex() if result and len(result) <= 64 and any(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def empty_activity(state='inactive', reason='No active watch lease'):
    return {'state': state, 'sample_id': None, 'source': None, 'messages': None,
            'bytes': None, 'rate_hz': None, 'bytes_per_second': None,
            'last_message_age_ms': None, 'history': [], 'publisher_counts': [],
            'unknown_publisher_messages': None, 'reason': reason,
            'measurement_scope': None, 'payload_export': False}


class GraphWatch:
    def __init__(self, node, config=None, activity_provider=None, resolver=None,
                 qos_factory=None, status_decoder=None, clock=time.monotonic, wall=time.time_ns):
        c = config or {}
        self.node, self.clock, self.wall = node, clock, wall
        self.provider, self.resolver, self.qos_factory = activity_provider, resolver, qos_factory
        self.status_decoder = status_decoder
        self.enabled = c.get('enabled', True)
        self.max_topics = min(64, max(1, int(c.get('max_topics', 64))))
        self.max_clients = min(8, max(1, int(c.get('max_clients', 8))))
        self.lease_s = min(30., max(3., float(c.get('lease_s', 12))))
        # These are rolling observation budgets, not a whole-session cutoff.
        self.window_s = 5.
        self.max_messages = min(50000, max(1, int(c.get('max_messages_per_window', 10000))))
        self.max_bytes = min(32 * 1024 * 1024, max(1, int(c.get('max_bytes_per_window', 8 * 1024 * 1024))))
        self.max_message_bytes = 65536
        self.global_max_messages = 50000
        self.global_max_bytes = 16 * 1024 * 1024
        self.global_window = {'window': deque(), 'window_messages': 0, 'window_bytes': 0}
        self.lock, self.reconcile_lock = threading.RLock(), threading.Lock()
        self.leases, self.topics, self.records, self.subscriptions = {}, {}, {}, {}
        self.passive, self.passive_at, self.provider_error = {}, None, None
        self.cleanup_errors = {}
        self.closed, self.session, self.deadline = False, None, 0.

    def _expire_leases(self, now):
        for key in [key for key, lease in self.leases.items() if lease['end'] <= now]:
            del self.leases[key]
        self.deadline = max((lease['end'] for lease in self.leases.values()), default=0.)

    def request(self, body):
        if not isinstance(body, dict):
            raise ValueError('Watch request must be a JSON object')
        operation = body.get('operation')
        if operation not in ('acquire', 'renew', 'release'):
            raise ValueError('Watch operation must be acquire, renew or release')
        if operation == 'acquire':
            try:
                client_id = str(uuid.UUID(body.get('client_id', '')))
            except (ValueError, TypeError, AttributeError):
                raise ValueError('Watch client_id must be a UUID') from None
        elif not isinstance(body.get('lease_id'), str) or not body['lease_id'] or len(body['lease_id']) > 128:
            raise ValueError('Watch lease_id is required')
        with self.lock:
            now = self.clock()
            self._expire_leases(now)
            if self.closed or not self.enabled:
                return {'schema_version': 1, 'accepted': False, 'reason': 'Live watching is unavailable', 'code': 'unavailable'}
            if operation == 'acquire':
                key = next((key for key, lease in self.leases.items() if lease['client_id'] == client_id), None)
                if key is None:
                    if len(self.leases) >= self.max_clients:
                        return {'schema_version': 1, 'accepted': False, 'reason': 'Watch client limit reached', 'code': 'client_limit'}
                    if not self.leases:
                        self.session = uuid.uuid4().hex
                    key = uuid.uuid4().hex
                self.leases[key] = {'client_id': client_id, 'end': now + self.lease_s}
            else:
                key = body['lease_id']
                if key not in self.leases:
                    return {'schema_version': 1, 'accepted': False, 'reason': 'Watch lease expired or is unknown', 'code': 'not_found'}
                if operation == 'release':
                    del self.leases[key]
                else:
                    self.leases[key]['end'] = now + self.lease_s
            self._expire_leases(now)
            remaining = max(0., self.leases[key]['end'] - now) if key in self.leases else 0.
            return {'schema_version': 1, 'accepted': True, 'lease_id': key,
                    'expires_in_ms': math.ceil(remaining * 1000), 'active': bool(self.leases)}

    def discover(self, topics):
        with self.lock:
            self.topics = {t['name']: tuple(t.get('types', [])) for t in topics}

    def _normalize_passive(self, value):
        if not isinstance(value, dict) or not isinstance(value.get('sample_id'), str) or not value['sample_id']:
            return None
        if not _number(value.get('messages'), integer=True):
            return None
        result = empty_activity('watching', '')
        result.update(sample_id=value['sample_id'][:128], source='dashboard_subscription',
                      measurement_scope='passive_subscription', messages=value['messages'])
        for field in ('bytes', 'rate_hz', 'bytes_per_second', 'last_message_age_ms', 'unknown_publisher_messages'):
            raw = value.get(field, value.get('unattributed_messages') if field == 'unknown_publisher_messages' else None)
            result[field] = raw if _number(raw, integer=field in ('bytes', 'unknown_publisher_messages')) else None
        publishers = value.get('publisher_counts')
        for item in (publishers if isinstance(publishers, list) else [])[:64]:
            if not isinstance(item, dict):
                continue
            gid = _gid(item.get('publisher_gid'))
            if gid and _number(item.get('messages'), integer=True):
                age = item.get('last_message_age_ms')
                result['publisher_counts'].append({'publisher_gid': gid, 'messages': item['messages'],
                    'last_message_age_ms': age if _number(age) else None})
        history = value.get('history')
        for item in (history if isinstance(history, list) else [])[-20:]:
            if (not isinstance(item, dict) or not _number(item.get('at_ns'), integer=True)
                    or not _number(item.get('messages'), integer=True)
                    or (item.get('bytes') is not None and not _number(item['bytes'], integer=True))):
                continue
            result['history'].append({'at_ns': item['at_ns'], 'messages': item['messages'], 'bytes': item.get('bytes')})
        # No arbitrary fields from the provider are exported as payload contents.
        return result

    def _read_passive(self):
        if self.provider is None:
            return {}
        try:
            raw = self.provider()
            if not isinstance(raw, dict):
                raise TypeError('Activity provider must return a topic map')
            values = {}
            for name, value in raw.items():
                if isinstance(name, str) and name.startswith('/'):
                    normalized = self._normalize_passive(value)
                    if normalized is not None:
                        values[name] = normalized
            with self.lock:
                self.passive, self.passive_at, self.provider_error = values, self.clock(), None
            return values
        except Exception as exc:
            # Retain known ownership on a temporary provider error. Never create
            # a duplicate subscription because its counter snapshot failed.
            with self.lock:
                self.provider_error = type(exc).__name__
                values = copy.deepcopy(self.passive)
                elapsed_ms = max(0., self.clock() - self.passive_at) * 1000 if self.passive_at is not None else 0.
            for value in values.values():
                value['state'], value['reason'] = 'unavailable', 'Passive activity provider unavailable'
                if value['last_message_age_ms'] is not None:
                    value['last_message_age_ms'] += elapsed_ms
                for publisher in value['publisher_counts']:
                    if publisher['last_message_age_ms'] is not None:
                        publisher['last_message_age_ms'] += elapsed_ms
            return values

    def _new_record(self, name, types):
        return {'sample_id': uuid.uuid4().hex, 'session': self.session, 'types': types,
                'state': 'watching', 'reason': '', 'start': self.clock(), 'started_at_ns': self.wall(),
                'messages': 0, 'bytes': 0, 'last': None, 'history': OrderedDict(),
                'window': deque(), 'window_messages': 0, 'window_bytes': 0,
                'publishers': OrderedDict(), 'unknown_publisher_messages': 0, 'retry_at': 0., 'subscribed': False}

    def _resolve(self, kind):
        if self.resolver is not None:
            return self.resolver(kind)
        # Hidden action feedback uses generated action-internal message classes,
        # not necessarily a name exported by the package's msg/action module.
        if '/action/' in kind and kind.endswith('_FeedbackMessage'):
            from rosidl_runtime_py.utilities import get_action
            return get_action(kind[:-len('_FeedbackMessage')]).Impl.FeedbackMessage
        from rosidl_runtime_py.utilities import get_message
        return get_message(kind)

    def _prune_window(self, record, now):
        while record['window'] and (record['window'][0][0] + 1) * .5 <= now - self.window_s:
            _, messages, size = record['window'].popleft()
            record['window_messages'] -= messages
            record['window_bytes'] -= size

    def _count_window(self, record, now, size):
        self._prune_window(record, now)
        bucket = math.floor(now / .5)
        if not record['window'] or record['window'][-1][0] != bucket:
            record['window'].append([bucket, 0, 0])
        record['window'][-1][1] += 1
        record['window'][-1][2] += size
        record['window_messages'] += 1
        record['window_bytes'] += size

    def _receive(self, name, record, data, info=None):
        size, now = len(data), self.clock()
        with self.lock:
            if (self.closed or now >= self.deadline or self.records.get(name) is not record
                    or record['session'] != self.session or record['state'] != 'watching'):
                return
            record['messages'] += 1
            record['bytes'] += size
            record['last'] = now
            bucket = math.floor(now / .5)
            self._count_window(record, now, size)
            self._count_window(self.global_window, now, size)
            history = record['history'].setdefault(bucket, {'at_ns': self.wall(), 'messages': 0, 'bytes': 0})
            history['messages'] += 1
            history['bytes'] += size
            while len(record['history']) > 20:
                record['history'].popitem(last=False)
            gid = _gid(info.get('publisher_gid') if isinstance(info, Mapping) else getattr(info, 'publisher_gid', None))
            if gid and (gid in record['publishers'] or len(record['publishers']) < 64):
                publisher = record['publishers'].setdefault(gid, {'messages': 0, 'last': None})
                publisher['messages'] += 1
                publisher['last'] = now
            else:
                record['unknown_publisher_messages'] += 1
            if size > self.max_message_bytes:
                record['state'], record['reason'] = 'blocked', 'Message exceeds 64 KiB observation budget'
            elif (self.global_window['window_messages'] > self.global_max_messages
                  or self.global_window['window_bytes'] > self.global_max_bytes):
                record['state'], record['reason'] = 'throttled', 'Global rolling raw-watch message/byte observation budget reached'
                record['retry_at'] = now + self.window_s + .5
            elif record['window_messages'] > self.max_messages or record['window_bytes'] > self.max_bytes:
                record['state'], record['reason'] = 'throttled', 'Rolling message/byte observation budget reached'
                record['retry_at'] = now + self.window_s + .5
            decode = record['types'] == (STATUS_TYPE,) and name.endswith('/_action/status') and size <= self.max_message_bytes
        if decode:
            self._decode_status(name, record, data)

    def _decode_status(self, name, record, data):
        try:
            if self.status_decoder is None:
                from rclpy.serialization import deserialize_message
                message = deserialize_message(data, record['message_type'])
            else:
                message = self.status_decoder(data)
            statuses = message.status_list
            counts = dict.fromkeys(STATUS_NAMES, 0)
            for item in statuses[:512]:
                code = item.status
                counts[STATUS_NAMES[code] if isinstance(code, int) and 0 <= code < len(STATUS_NAMES) else 'unknown'] += 1
            summary = {'counts': counts, 'observed_goals': min(len(statuses), 512), 'truncated': len(statuses) > 512}
            with self.lock:
                if self.records.get(name) is record:
                    record['status_summary'] = summary
                    record.pop('status_decode_error', None)
        except Exception as exc:
            with self.lock:
                if self.records.get(name) is record:
                    record['status_decode_error'] = type(exc).__name__

    def _destroy(self, name, handle):
        try:
            if self.node.destroy_subscription(handle) is False:
                raise RuntimeError('Subscription destruction did not succeed')
        except Exception as exc:
            with self.lock:
                self.cleanup_errors[name] = type(exc).__name__
            return
        with self.lock:
            if self.subscriptions.get(name) is handle:
                del self.subscriptions[name]
            self.cleanup_errors.pop(name, None)

    def reconcile(self):
        """Called only by the observer thread (or deterministic offline tests)."""
        with self.reconcile_lock:
            passive = self._read_passive()
            with self.lock:
                now = self.clock()
                self._expire_leases(now)
                active = self.enabled and not self.closed and bool(self.leases)
                topics = dict(self.topics) if active else {}
                remove = []
                for name, handle in self.subscriptions.items():
                    record = self.records[name]
                    if (name not in topics or name in passive or record['types'] != topics[name]
                            or record['session'] != self.session or record['state'] != 'watching'):
                        if record['state'] == 'watching':
                            record['state'], record['reason'] = 'inactive', 'Watch coverage changed or leases expired'
                        remove.append((name, handle))
            for name, handle in remove:
                self._destroy(name, handle)
            # Action metadata is small and should not be starved by alphabetical
            # ordinary-topic order when the configured topic ceiling is reached.
            for name, types in sorted(topics.items(), key=lambda item: ('/_action/' not in item[0], item[0])):
                if name in passive:
                    continue
                with self.lock:
                    if self.closed or self.clock() >= self.deadline:
                        break
                    record = self.records.get(name)
                    if name in self.subscriptions:
                        continue
                    if record is None or record['session'] != self.session or record['types'] != types:
                        record = self._new_record(name, types)
                        self.records[name] = record
                    elif record['state'] == 'blocked' or self.clock() < record['retry_at']:
                        continue
                    reason = None
                    if len(types) != 1:
                        reason = ('unavailable', 'A single discoverable message type is required')
                    elif types[0] in BLOCKED_TYPES or types[0].startswith(('audio_msgs/', 'audio_common_msgs/')):
                        reason = ('blocked', 'High-bandwidth type requires an existing passive activity source')
                    elif len(self.subscriptions) >= self.max_topics:
                        reason = ('limited', 'Live topic subscription limit reached')
                    if reason:
                        record['state'], record['reason'] = reason
                        continue
                try:
                    message_type = self._resolve(types[0])
                    if self.qos_factory is None:
                        from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
                        qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT, durability=DurabilityPolicy.VOLATILE)
                    else:
                        qos = self.qos_factory()
                    # Both arguments are required: rclpy can classify an
                    # optional second argument as a message-only callback and
                    # then omit MessageInfo (and every publisher GID).
                    def callback_for(topic_name, topic_record):
                        def receive(data, message_info):
                            self._receive(topic_name, topic_record, data, message_info)
                        return receive
                    with self.lock:
                        record['state'], record['reason'], record['message_type'] = 'watching', '', message_type
                    handle = self.node.create_subscription(message_type, name, callback_for(name, record), qos, raw=True)
                    with self.lock:
                        self.subscriptions[name] = handle
                        record['subscribed'] = True
                except Exception as exc:
                    with self.lock:
                        record['state'], record['reason'] = 'unavailable', 'Cannot subscribe to this type: ' + type(exc).__name__
                        record['retry_at'] = self.clock() + 5.
            with self.lock:
                for name in list(self.records):
                    if name not in self.topics and name not in self.subscriptions:
                        del self.records[name]

    def _snapshot_record(self, name, record, now):
        if not record['subscribed']:
            return empty_activity(record['state'], record['reason'])
        self._prune_window(record, now)
        elapsed = min(self.window_s, max(0., now - record['start']))
        result = empty_activity(record['state'], record['reason'])
        result.update(sample_id=record['sample_id'], source='ros_subscription',
                      measurement_scope='local_observer_subscription', messages=record['messages'], bytes=record['bytes'],
                      rate_hz=record['window_messages'] / elapsed if elapsed else None,
                      bytes_per_second=record['window_bytes'] / elapsed if elapsed else None,
                      last_message_age_ms=(now - record['last']) * 1000 if record['last'] is not None else None,
                      history=copy.deepcopy(list(record['history'].values())),
                      publisher_counts=[{'publisher_gid': gid, 'messages': value['messages'],
                                         'last_message_age_ms': (now - value['last']) * 1000}
                                        for gid, value in record['publishers'].items()],
                      unknown_publisher_messages=record['unknown_publisher_messages'])
        if name in self.cleanup_errors:
            result.update(state='cleanup_pending', cleanup_error=self.cleanup_errors[name])
        if 'status_summary' in record:
            result['status_summary'] = copy.deepcopy(record['status_summary'])
        if 'status_decode_error' in record:
            result['status_decode_error'] = record['status_decode_error']
        return result

    def snapshot(self):
        passive = self._read_passive()
        with self.lock:
            now = self.clock()
            self._expire_leases(now)
            self._prune_window(self.global_window, now)
            active = self.enabled and not self.closed and bool(self.leases)
            values = {}
            for name in self.topics:
                if not active:
                    values[name] = empty_activity()
                elif name in passive:
                    values[name] = copy.deepcopy(passive[name])
                elif name in self.records:
                    values[name] = self._snapshot_record(name, self.records[name], now)
                else:
                    values[name] = empty_activity('unavailable', 'Waiting for observer reconciliation')
            coverage = {}
            for value in values.values():
                coverage[value['state']] = coverage.get(value['state'], 0) + 1
            errors = [{'topic': name, 'operation': 'cleanup', 'error': error} for name, error in self.cleanup_errors.items()]
            if self.provider_error:
                errors.append({'operation': 'passive_provider', 'error': self.provider_error})
            status = {'enabled': self.enabled, 'active': active, 'lease_count': len(self.leases),
                      'max_clients': self.max_clients, 'max_topics': self.max_topics, 'lease_s': self.lease_s,
                      'subscriptions': len(self.subscriptions), 'passive_topics': sum(name in passive for name in self.topics) if active else 0,
                      'cleanup_pending': len(self.cleanup_errors), 'coverage': coverage, 'errors': errors,
                      'window_s': self.window_s, 'max_messages_per_window': self.max_messages,
                      'max_bytes_per_window': self.max_bytes, 'max_message_bytes': self.max_message_bytes,
                      'global_max_messages_per_window': self.global_max_messages,
                      'global_max_bytes_per_window': self.global_max_bytes,
                      'global_window_messages': self.global_window['window_messages'],
                      'global_window_bytes': self.global_window['window_bytes'],
                      'payload_export': False}
            return status, values

    def close(self):
        with self.lock:
            self.closed = True
            self.leases.clear()
            self.deadline = 0.
        self.reconcile()


def action_activity(action_name, topics):
    status_topic, feedback_topic = action_name + '/_action/status', action_name + '/_action/feedback'
    missing = lambda: empty_activity('unavailable', 'Action metadata topic is not discovered')
    status, feedback = topics.get(status_topic, missing()), topics.get(feedback_topic, missing())
    states = [status['state'], feedback['state']]
    state = 'watching' if all(item == 'watching' for item in states) else ('partial' if 'watching' in states else 'unavailable')
    if all(item == 'inactive' for item in states):
        state = 'inactive'
    ages = [entry['last_message_age_ms'] for entry in (status, feedback) if _number(entry.get('last_message_age_ms'))]
    rates = [entry['rate_hz'] for entry in (status, feedback) if _number(entry.get('rate_hz'))]
    counts = [entry['messages'] for entry in (status, feedback) if _number(entry.get('messages'), integer=True)]
    return {'state': state, 'measurement_scope': 'action_server_publications',
            'status_topic': status_topic, 'feedback_topic': feedback_topic,
            'status': status, 'feedback': feedback, 'messages': sum(counts) if counts else None,
            'status_messages': status['messages'], 'feedback_messages': feedback['messages'],
            'last_message_age_ms': min(ages) if ages else None, 'rate_hz': sum(rates) if rates else None,
            'requests': {'state': 'unavailable', 'reason': 'Client requests/results are not observed by topic counters'},
            'payload_export': False}
