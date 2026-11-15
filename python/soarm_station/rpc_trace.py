"""Bounded RPC trace counters and a read-only graph-facing Unix client.

The collector uses standard LTTng tracepoints. No service payload, process
address, request object, or response object is exported through this module.
"""
import copy
import hashlib
import math
import socket
import threading
import time
import uuid
from collections import OrderedDict, deque
from pathlib import Path

from .ipc import receive, send


RPC_PHASES = {
    'ros2:rmw_send_request': ('client', 'request_sent', 'rmw_client_handle'),
    'ros2:rmw_take_request': ('server', 'request_received', 'rmw_service_handle'),
    'ros2:rmw_send_response': ('server', 'response_sent', 'rmw_service_handle'),
    'ros2:rmw_take_response': ('client', 'response_received', 'rmw_client_handle'),
}
ROLE_PHASES = {'client': ('request_sent', 'response_received'),
               'server': ('request_received', 'response_sent')}
INIT_EVENTS = ('ros2:rcl_init', 'ros2:rcl_node_init', 'ros2:rcl_service_init',
               'ros2:rcl_client_init', 'ros2:rmw_client_init')


def rpc_trace_socket(config):
    value = config.get('rpc_trace', {}).get('socket')
    if value:
        path = Path(value).expanduser()
        return str(path if path.is_absolute() else Path(config['_path']).parent / path)
    path = Path(config['socket'])
    return str(path.with_name(path.stem + '.rpc-trace.sock'))


def unavailable_snapshot(reason):
    return {'schema_version': 1, 'source': 'lttng', 'state': 'unavailable',
            'active': False, 'trace_ready': False, 'epoch': None, 'mapping_epoch': None,
            'clock_type': 'collector_monotonic', 'measurement_mode': 'graph_watch_epochs',
            'captured_at_ns': str(time.monotonic_ns()), 'endpoints': [], 'reason': reason,
            'counts_lower_bound': True, 'loss': {'events': 0, 'packets': 0, 'unmapped': 0}}


class RpcTraceClient:
    """Persistent metadata-only UDS connection; its interest ends on EOF."""
    def __init__(self, path, timeout_s=.3):
        self.path = str(path); self.timeout_s = timeout_s
        self.sock = None; self.lock = threading.RLock(); self.wanted_active = False

    def _exchange(self, request):
        send(self.sock, request)
        response = receive(self.sock)
        if not response.get('ok'): raise RuntimeError(response.get('error', 'RPC trace source failed'))
        return response['data']

    def _connect(self):
        if self.sock is not None: return
        sock = socket.socket(socket.AF_UNIX)
        sock.settimeout(self.timeout_s)
        try: sock.connect(self.path)
        except BaseException: sock.close(); raise
        self.sock = sock
        self._exchange({'op': 'set_active', 'active': self.wanted_active})

    def set_active(self, active):
        with self.lock:
            self.wanted_active = bool(active)
            try:
                if self.sock is None: self._connect()
                else: self._exchange({'op': 'set_active', 'active': self.wanted_active})
            except (OSError, EOFError, ValueError, RuntimeError): self._disconnect()

    def snapshot(self):
        with self.lock:
            try:
                self._connect()
                return self._exchange({'op': 'snapshot'})
            except (OSError, EOFError, ValueError, RuntimeError) as exc:
                self._disconnect()
                return unavailable_snapshot('RPC trace collector unavailable: ' + type(exc).__name__)

    def _disconnect(self):
        if self.sock is not None:
            try: self.sock.close()
            finally: self.sock = None

    def close(self):
        with self.lock:
            self.wanted_active = False
            self._disconnect()


class RpcTraceCounters:
    """Exact handle/FQN mappings from init events and phase-local receipt counts."""
    def __init__(self, *, clock=time.monotonic_ns, accept_pid=None, max_endpoints=256):
        self.clock = clock; self.accept_pid = accept_pid or (lambda _pid: True)
        self.max_endpoints = min(256, max(1, max_endpoints))
        self.lock = threading.RLock(); self.mapping_epoch = uuid.uuid4().hex; self.epoch = uuid.uuid4().hex
        self.nodes = OrderedDict(); self.endpoints = OrderedDict(); self.client_gids = OrderedDict()
        self.interests = set(); self.active_since = None; self.trace_ready = False; self.initializing = True
        self.reason = 'Trace collector starting'; self.process_starts = OrderedDict()
        self.loss = {'events': 0, 'packets': 0, 'unmapped': 0}; self.last_loss = None
        self.epoch_lower_bound = False; self.mapping_limited = False; self.excluded_pids = set()
        self.window_ns = 5_000_000_000

    def set_ready(self, ready, reason=''):
        with self.lock:
            self.initializing = False; self.trace_ready = bool(ready)
            if reason or not self.epoch_lower_bound:self.reason = reason

    def set_active(self, owner, active):
        with self.lock:
            before = bool(self.interests)
            if active:
                if owner not in self.interests and len(self.interests) >= 8:
                    raise ValueError('RPC trace watcher limit reached')
                self.interests.add(owner)
            else: self.interests.discard(owner)
            if not before and self.interests:
                self.epoch = uuid.uuid4().hex; self.active_since = self.clock(); self.epoch_lower_bound = False
                self.last_loss = None
                for endpoint in self.endpoints.values(): endpoint['channels'] = self._channels()

    @staticmethod
    def _channels(): return {}

    def report_loss(self, events=0, packets=0, unmapped=0, reason='Trace records were lost'):
        with self.lock:
            for name, value in (('events', events), ('packets', packets), ('unmapped', unmapped)):
                self.loss[name] += max(0, int(value))
            self.last_loss = self.clock()
            if self.interests: self.epoch_lower_bound = True
            self.reason = reason

    def _bounded(self, mapping, limit):
        if len(mapping) > limit:
            mapping.popitem(last=False); self.mapping_limited = True

    def _retire_pid(self,pid):
        for mapping in (self.nodes,self.endpoints,self.client_gids):
            for key in list(mapping):
                if key[0]==pid:del mapping[key]
        self.process_starts.pop(pid,None)

    def _prune_retired(self):
        identity=getattr(self.accept_pid,'identity',None)
        for pid in {key[0] for mapping in (self.nodes,self.endpoints,self.client_gids) for key in mapping}:
            if (not self.accept_pid(pid) or (identity is not None and pid in self.process_starts
                    and identity(pid)!=self.process_starts[pid])):self._retire_pid(pid)

    def ingest(self, event, pid, stamp_ns, fields):
        if event not in INIT_EVENTS and event not in RPC_PHASES: return
        if not isinstance(pid, int) or pid <= 0 or not isinstance(stamp_ns, int) or stamp_ns < 0: return
        if not self.accept_pid(pid):
            with self.lock:
                if len(self.excluded_pids) < 256: self.excluded_pids.add(pid)
            return
        with self.lock:
            birth=fields.get('process_start')
            if birth is not None:
                if self.process_starts.get(pid,birth)!=birth:self._retire_pid(pid)
                self.process_starts[pid]=birth;self._bounded(self.process_starts,512)
            if event == 'ros2:rcl_init':
                # Multiple ROS contexts may share a process. Clear only when an
                # observed /proc birth token proves PID reuse, not on every init.
                return
            if event == 'ros2:rcl_node_init':
                name, namespace = fields.get('node_name'), fields.get('node_namespace')
                if not isinstance(name, str) or not name or not isinstance(namespace, str) or not namespace.startswith('/'):
                    self.report_loss(unmapped=1, reason='Node initialization has no exact FQN'); return
                self.nodes[(pid, int(fields['node_handle']))] = namespace.rstrip('/') + '/' + name
                self._bounded(self.nodes, 128)
                return
            if event == 'ros2:rmw_client_init':
                value = fields.get('gid')
                if isinstance(value, (bytes, bytearray, list, tuple)):
                    self.client_gids[(pid, int(fields['rmw_client_handle']))] = bytes(value).hex()
                    self._bounded(self.client_gids, 256)
                return
            if event in ('ros2:rcl_client_init', 'ros2:rcl_service_init'):
                self._prune_retired()
                role = 'client' if event == 'ros2:rcl_client_init' else 'server'
                field = 'rmw_client_handle' if role == 'client' else 'rmw_service_handle'
                service = fields.get('service_name')
                if not isinstance(service, str) or not service.startswith('/'):
                    self.report_loss(unmapped=1, reason='Service initialization has no exact name'); return
                handle = int(fields[field]); key = (pid, role, handle)
                identity = f'{self.mapping_epoch}:{pid}:{self.process_starts.get(pid)}:{role}:{handle}:{stamp_ns}'
                self.endpoints[key] = {'id': hashlib.sha256(identity.encode()).hexdigest()[:32],
                    'service': service, 'role': role, 'pid': pid, 'node_handle': int(fields['node_handle']),
                    'born_ns': stamp_ns, 'channels': self._channels()}
                self._bounded(self.endpoints, self.max_endpoints)
                return
            role, phase, handle_field = RPC_PHASES[event]
            if event in ('ros2:rmw_take_request', 'ros2:rmw_take_response') and not fields.get('taken'): return
            if not self.interests or not self.trace_ready or stamp_ns < self.active_since: return
            endpoint = self.endpoints.get((pid, role, int(fields[handle_field])))
            if endpoint is None:
                self.report_loss(unmapped=1, reason='RPC receipt has no captured initialization mapping'); return
            if (pid, endpoint['node_handle']) not in self.nodes:
                self.report_loss(unmapped=1, reason='RPC endpoint has no captured node mapping'); return
            channel = endpoint['channels'].setdefault(phase, {'messages': 0, 'last': None,
                'history': OrderedDict(), 'recent': deque(maxlen=1024), 'overflow_until': 0})
            channel['messages'] += 1; channel['last'] = max(channel['last'] or stamp_ns,stamp_ns)
            recent = channel['recent']; cutoff = stamp_ns - self.window_ns
            while recent and recent[0] <= cutoff: recent.popleft()
            if len(recent) == recent.maxlen: channel['overflow_until'] = stamp_ns + self.window_ns
            if recent and stamp_ns<recent[-1]:
                recent=deque(sorted([*recent,stamp_ns]),maxlen=recent.maxlen);channel['recent']=recent
            else:recent.append(stamp_ns)
            bucket = stamp_ns // 500_000_000
            channel['history'][bucket] = channel['history'].get(bucket, 0) + 1
            if bucket!=next(reversed(channel['history'])):
                channel['history']=OrderedDict(sorted(channel['history'].items()))
            while len(channel['history']) > 20: channel['history'].popitem(last=False)

    def snapshot(self):
        with self.lock:
            self._prune_retired()
            now = self.clock(); active = bool(self.interests)
            state = 'initializing' if self.initializing else 'unavailable' if not self.trace_ready else 'inactive' if not active else (
                'partial' if self.epoch_lower_bound or self.mapping_limited else 'ready')
            endpoints = []
            for (pid, role, handle), entry in self.endpoints.items():
                name = self.nodes.get((pid, entry['node_handle']))
                ready = bool(name and active and self.trace_ready)
                started = max(entry['born_ns'], self.active_since or now)
                observed = max(0., min(5., (now - started) / 1e9))
                channels = {}
                for phase in ROLE_PHASES[role]:
                    channel = entry['channels'].get(phase)
                    end=now//500_000_000;begin=max(started//500_000_000,end-19)
                    buckets=channel['history'] if channel else {}
                    history=[{'at_ns':str(max(bucket*500_000_000,started)),'messages':buckets.get(bucket,0)}
                             for bucket in range(begin,end+1)]
                    if channel:
                        while channel['recent'] and channel['recent'][0] <= now - self.window_ns: channel['recent'].popleft()
                        messages, last = channel['messages'], channel['last']
                        overflow = now < channel['overflow_until']
                        rate = len(channel['recent']) / observed if ready and observed > 0 and not overflow else None
                    else: messages, last, overflow, rate = 0, None, False, 0. if ready and observed > 0 else None
                    loss_in_window = self.last_loss is not None and now - self.last_loss < self.window_ns
                    channels[phase] = {'messages': messages if ready else None,
                        'last_event_age_ms': max(0., (now - last) / 1e6) if ready and last is not None else None,
                        'rate_hz': rate if not loss_in_window else None, 'observed_window_s': observed if ready else 0.,
                        'window_s': 5., 'history': copy.deepcopy(history) if ready else [],
                        'loss_in_window': loss_in_window, 'rate_history_overflow': overflow,
                        'counts_lower_bound': self.epoch_lower_bound}
                record = {key: entry[key] for key in ('id', 'service', 'role', 'pid')}
                record.update(node_fqn=name, mapping_ready=bool(name), ready=ready, channels=channels)
                if role == 'client': record['client_gid'] = self.client_gids.get((pid, handle))
                endpoints.append(record)
            return {'schema_version': 1, 'source': 'lttng', 'epoch': self.epoch,
                'mapping_epoch': self.mapping_epoch, 'state': state, 'active': active,
                'trace_ready': self.trace_ready, 'measurement_mode': 'graph_watch_epochs',
                'clock_type': 'collector_monotonic', 'captured_at_ns': str(now),
                'endpoints': endpoints, 'reason': self.reason, 'loss': dict(self.loss),
                'counts_lower_bound': self.epoch_lower_bound, 'mapping_limited': self.mapping_limited,
                'max_endpoints': self.max_endpoints, 'excluded_processes': len(self.excluded_pids),
                'payload_export': False}
