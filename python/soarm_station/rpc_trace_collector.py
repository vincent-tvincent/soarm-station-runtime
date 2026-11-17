"""Standard LTTng snapshot collector, owned temporary storage and local UDS only."""
import argparse
import gc
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import socketserver
import struct
import subprocess
import tempfile
import threading
import time
import traceback
import uuid
from collections import OrderedDict,deque

from .ipc import receive, send
from .rpc_trace import INIT_EVENTS, RPC_PHASES, RpcTraceCounters


INIT_FIELDS = {
    'ros2:rcl_init': ('context_handle',),
    'ros2:rcl_node_init': ('node_handle', 'node_name', 'node_namespace'),
    'ros2:rcl_service_init': ('node_handle', 'rmw_service_handle', 'service_name'),
    'ros2:rcl_client_init': ('node_handle', 'rmw_client_handle', 'service_name'),
    'ros2:rmw_client_init': ('rmw_client_handle', 'gid'),
}
RPC_FIELDS = {
    name: (handle, 'sequence_number') + (('taken',) if 'take_' in name else ())
    for name, (_role, _phase, handle) in RPC_PHASES.items()
}
PACKET_CONTEXT = ('timestamp_begin', 'timestamp_end', 'content_size', 'packet_size',
                  'packet_seq_num', 'events_discarded', 'cpu_id')


def process_info(pid):
    try:
        tail = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return int(tail[1]), tail[19]  # PPID and process start ticks; no arguments/environment.
    except (OSError, ValueError, IndexError): return None


class ProcessScope:
    def __init__(self, parent_pid=None):
        self.parent_pid = parent_pid
        self.parent_start = process_info(parent_pid) if parent_pid else None
        self.cache = OrderedDict()

    def __call__(self, pid):
        identity=process_info(pid)
        if identity is None:return False
        if not self.parent_pid: return True
        now = time.monotonic(); cached = self.cache.get(pid)
        if cached and now - cached[0] < 1. and cached[1]==identity[1]: return cached[2]
        current = pid; accepted = False
        for _ in range(64):
            info = process_info(current)
            if info is None: break
            if current == self.parent_pid:
                accepted = info == self.parent_start; break
            if info[0] <= 1 or info[0] == current: break
            current = info[0]
        self.cache[pid] = (now, identity[1], accepted)
        while len(self.cache) > 512: self.cache.popitem(last=False)
        return accepted

    def identity(self,pid):
        value=process_info(pid)
        return value[1] if value is not None else None


class SnapshotReader:
    """Babeltrace decodes metadata events; packet headers detect snapshot gaps."""
    def __init__(self, counters, max_events=100000):
        self.counters = counters; self.max_events = max_events
        self.watermarks = {}; self.packets = {}

    def read_packet_sequences(self, directory):
        for metadata in directory.rglob('metadata'):
            if metadata.stat().st_size > 2 * 1024 * 1024: raise ValueError('Trace metadata exceeds bound')
            text = metadata.read_text()
            header = re.search(r'packet\.header\s*:=\s*struct\s*\{(.*?)\n\s*\};', text, re.S)
            contexts = re.findall(r'packet\.context\s*:=\s*struct\s*\{(.*?)\n\s*\};', text, re.S)
            fields = lambda block: tuple(re.findall(r'\}\s*(\w+)(?:\[\d+\])?\s*;', block))
            if (header is None or fields(header[1]) != ('magic', 'uuid', 'stream_id', 'stream_instance_id')
                    or not contexts or any(fields(block) != PACKET_CONTEXT for block in contexts)):
                raise ValueError('Unsupported CTF packet layout; loss detection unavailable')
            endian = '<' if re.search(r'byte_order\s*=\s*le\s*;', text) else '>'
            layout = struct.Struct(endian + 'I16sI7QI')
            for path in metadata.parent.iterdir():
                if not re.fullmatch(r'(init|rpc)_\d+', path.name) or not path.is_file(): continue
                key = path.parent.name + '/' + path.name
                previous, discarded = self.packets.get(key, (-1, 0))
                with path.open('rb') as stream:
                    remaining = path.stat().st_size; offset = 0; count = 0
                    while offset < remaining:
                        raw = stream.read(layout.size)
                        if len(raw) != layout.size: raise ValueError('Truncated CTF packet header')
                        magic, _uuid, _stream, _instance, _begin, _end, content, size, sequence, dropped, _cpu = layout.unpack(raw)
                        if magic != 0xc1fc1fc1 or size % 8 or not layout.size <= content // 8 <= size // 8 <= remaining - offset:
                            raise ValueError('Invalid CTF packet size')
                        if sequence > previous + 1:
                            self.counters.report_loss(packets=sequence - previous - 1, reason='Snapshot buffer overwrite observed')
                            if path.name.startswith('init_'): self.counters.mapping_limited = True
                        if dropped > discarded:
                            self.counters.report_loss(events=dropped - discarded, reason='LTTng discarded events')
                        previous = max(previous, sequence); discarded = max(discarded, dropped)
                        offset += size // 8; stream.seek(offset); count += 1
                        if count > 8192: raise ValueError('Trace packet count exceeds bound')
                self.packets[key] = (previous, discarded)

    def consume(self, directory):
        import bt2
        directory = Path(directory)
        self.read_packet_sequences(directory)
        iterator=None;exhausted=False;failure=None
        try:
            iterator=bt2.TraceCollectionMessageIterator(str(directory))
            exhausted=self._consume_iterator(bt2,iterator)
        except BaseException as error:
            # A failed iterator constructor/decoder can leave its graph in an
            # exception traceback. Clear completed frames, preserving the error
            # type, message and traceback locations, before collecting cycles.
            failure=error;self._clear_exception_frames(error)
            raise
        finally:
            # bt2 2.x exposes no close() on TraceCollectionMessageIterator. Its
            # graph retains a bound port-listener callback to the iterator; ref
            # counting alone therefore leaves source files open across captures.
            # The helper frame releases message/event/field wrappers first, then
            # one collector-local cycle collection finalizes the native graph.
            try:
                if iterator is not None and not exhausted:
                    # A partially consumed bt2 graph also retains native message
                    # memory. Finish its finite, byte-bounded snapshot using the
                    # public iterator API; discarded events never reach counters.
                    deque(iterator,maxlen=0)
            except BaseException as error:
                self._clear_exception_frames(error)
                if failure is None:raise
            finally:
                iterator=None
                gc.collect()

    @staticmethod
    def _clear_exception_frames(error):
        pending=[error];seen=set()
        while pending:
            current=pending.pop()
            if id(current) in seen:continue
            seen.add(id(current));traceback.clear_frames(current.__traceback__)
            pending.extend(e for e in (current.__cause__,current.__context__) if e is not None)

    def _consume_iterator(self,bt2,iterator):
        count = 0
        for message in iterator:
            if not isinstance(message, bt2._EventMessageConst): continue
            count += 1
            if count > self.max_events:
                self.counters.report_loss(events=1, reason='Snapshot event processing bound reached'); return False
            event = message.event
            wanted = INIT_FIELDS.get(event.name) or RPC_FIELDS.get(event.name)
            if wanted is None: continue
            clock = message.default_clock_snapshot
            if clock.clock_class.name != 'monotonic' or clock.clock_class.frequency != 1_000_000_000:
                raise ValueError('Trace clock is not the local monotonic nanosecond clock')
            stamp = int(clock.value); pid = int(event.common_context_field['vpid'])
            fields = {}
            for name in wanted:
                # The C tracepoint argument is node_namespace, but standard
                # LTTng CTF names its serialized field "namespace".
                field_name='namespace' if name=='node_namespace' and name not in event.payload_field else name
                value = event.payload_field[field_name]
                if name in ('node_name', 'node_namespace', 'service_name'): fields[name] = str(value)
                elif name == 'gid': fields[name] = bytes(int(item) for item in value)
                else: fields[name] = int(value)
            identity=process_info(pid)
            if identity is not None:fields['process_start']=identity[1]
            # Request/response pointers and contents are never selected above.
            fingerprint = (event.name, pid, tuple((name, repr(fields[name])) for name in wanted))
            key = Path(event.stream.name).parent.name + '/' + Path(event.stream.name).name
            previous, identities = self.watermarks.get(key, (-1, set()))
            if stamp < previous or (stamp == previous and fingerprint in identities): continue
            if stamp > previous: identities = set()
            if len(identities) >= 4096:
                self.counters.report_loss(events=1, reason='Trace timestamp collision bound reached'); continue
            identities.add(fingerprint); self.watermarks[key] = (stamp, identities)
            self.counters.ingest(event.name, pid, stamp, fields)
        return True


class LttngSnapshotSource:
    def __init__(self, counters, *, event_filter=None, root=None, command=None):
        self.counters = counters; self.reader = SnapshotReader(counters)
        self.command = command or self._command
        if root is None:self.root=Path(tempfile.mkdtemp(prefix='soarm-rpc-trace-',dir='/tmp'))
        else:
            self.root=Path(root)
            self.root.mkdir(mode=0o700)  # Never claim or later delete an existing directory.
        os.chmod(self.root, 0o700)
        self.token = uuid.uuid4().hex; self.session = 'soarm-rpc-' + self.token
        self.event_filter = event_filter; self.created = False; self.slot = 0
        self.buffer_bytes = 0; self.max_snapshot_bytes = 16 * 1024 * 1024
        (self.root / 'owner.json').write_text(json.dumps({'session': self.session, 'pid': os.getpid(), 'token': self.token}))

    @staticmethod
    def _command(args):
        result = subprocess.run(['lttng', *args], capture_output=True, text=True, timeout=3)
        if result.returncode:
            raise RuntimeError('LTTng ' + args[0] + ': ' + result.stderr.strip()[:240])
        return result.stdout

    def start(self):
        import bt2  # Fail before creating a session when the standard reader is absent.
        _ = bt2.TraceCollectionMessageIterator
        cpus = os.cpu_count() or 1
        per_buffer = min(65536, 2 ** int(math.floor(math.log2(max(1, 8 * 1024 * 1024 // (cpus * 6))))))
        if per_buffer < 4096: raise RuntimeError('CPU count exceeds bounded trace-buffer allocation')
        self.buffer_bytes = per_buffer * cpus * 6
        # CLI creation updates the user's default session selector. All our
        # commands name the owned session explicitly; restore that selector so
        # unrelated tracing workflows retain their previous default.
        selector=Path(os.environ.get('LTTNG_HOME',str(Path.home())))/'.lttngrc'
        if selector.exists() and selector.stat().st_size>16384:raise RuntimeError('LTTng session selector exceeds supported size')
        previous=selector.read_bytes() if selector.exists() else None
        self.command(['create', self.session, '--snapshot', '--no-output']); self.created = True
        if selector.exists() and selector.stat().st_size<=16384 and self.session.encode() in selector.read_bytes():
            if previous is None:selector.unlink()
            else:selector.write_bytes(previous)
        for channel, number, events in (('init', 2, INIT_EVENTS), ('rpc', 4, tuple(RPC_PHASES))):
            self.command(['enable-channel', '--userspace', '--session', self.session, channel,
                          f'--subbuf-size={per_buffer}', f'--num-subbuf={number}', '--overwrite'])
            args = ['enable-event', '--userspace', '--session', self.session, '--channel', channel, ','.join(events)]
            if self.event_filter: args += ['--filter', self.event_filter]
            self.command(args)
            self.command(['add-context', '--userspace', '--session', self.session, '--channel', channel, '--type=vpid'])
        self.command(['start', self.session]); self.counters.set_ready(True)

    def capture(self):
        target = self.root / ('snapshot-' + str(self.slot)); self.slot = 1 - self.slot
        if target.exists(): shutil.rmtree(target)
        try:
            self.command(['snapshot', 'record', '--session', self.session,
                          f'--max-size={self.max_snapshot_bytes}', 'file://' + str(target)])
        except RuntimeError as exc:
            if 'No data' in str(exc) or 'no data' in str(exc): return
            raise
        size = sum(path.stat().st_size for path in target.rglob('*') if path.is_file())
        if size > self.max_snapshot_bytes + 2 * 1024 * 1024:
            raise RuntimeError('Snapshot disk bound exceeded')
        self.reader.consume(target)

    def close(self):
        try:
            if self.created:
                self.command(['destroy', self.session]); self.created = False
        finally:
            if not self.created and self.root.exists(): shutil.rmtree(self.root)


def cleanup_stale_owner(manifest):
    """Recover only a recorded, dead collector's exact owned session/directory."""
    if not manifest.exists():return
    value=json.loads(manifest.read_text())
    directory=Path(value.get('directory',''))
    token=value.get('token','');session=value.get('session','')
    if (not re.fullmatch(r'[a-f0-9]{32}',token) or session!='soarm-rpc-'+token
            or directory.parent!=Path('/tmp') or not directory.name.startswith('soarm-rpc-trace-')
            or type(value.get('pid'))!=int or value['pid']<=0 or directory.is_symlink()):
        raise ValueError('Unrecognized RPC trace ownership manifest')
    info=process_info(value.get('pid',0))
    if info is not None and info[1]==value.get('process_start'):
        raise RuntimeError('Recorded RPC trace collector is still alive')
    marker=directory/'owner.json'
    if directory.exists():
        owner=json.loads(marker.read_text())
        if owner.get('token')!=token or owner.get('session')!=session:
            raise ValueError('RPC trace temporary-directory ownership mismatch')
    result=subprocess.run(['lttng','destroy',session],capture_output=True,text=True,timeout=3)
    if result.returncode:
        detail=result.stderr.strip()
        # After a reboot the private manifest can outlive the per-user daemon.
        # No daemon means no session to destroy. Match the complete known error,
        # not a substring that could hide permission failures or mixed output.
        missing_daemon=detail=='Error: No session daemon is available'
        missing_session=re.fullmatch(
            rf"Error: Session (?:(?:name|{re.escape(session)}|'{re.escape(session)}') )?(?:not found|does not exist)",
            detail,re.IGNORECASE) is not None
        if result.stdout.strip() or not (missing_daemon or missing_session):
            raise RuntimeError('Cannot clean recorded RPC trace session: '+(detail or 'lttng destroy failed')[:240])
    if directory.exists():shutil.rmtree(directory)
    manifest.unlink(missing_ok=True)


class RpcTraceServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    def __init__(self, path, counters, status=None):
        self.counters = counters; self.status = status or (lambda: {})
        super().__init__(str(path), RpcTraceHandler)


class RpcTraceHandler(socketserver.BaseRequestHandler):
    def handle(self):
        owner = uuid.uuid4().hex
        try:
            while True:
                try:
                    request = receive(self.request)
                    if request.get('op') == 'set_active':
                        if type(request.get('active')) is not bool: raise ValueError('active must be boolean')
                        self.server.counters.set_active(owner, request['active']); data = {'accepted': True}
                    elif request.get('op') == 'snapshot': data = {**self.server.counters.snapshot(), **self.server.status()}
                    else: raise ValueError('Unsupported RPC trace request')
                    send(self.request, {'ok': True, 'data': data})
                except (OSError, EOFError): return
                except (ValueError, TypeError) as exc:
                    try: send(self.request, {'ok': False, 'error': str(exc)})
                    except OSError: return
        finally: self.server.counters.set_active(owner, False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--socket', required=True); parser.add_argument('--parent-pid', type=int)
    parser.add_argument('--interval', type=float, default=1.); parser.add_argument('--event-filter')
    args = parser.parse_args(); interval = min(2., max(.25, args.interval))
    path = Path(args.socket); path.parent.mkdir(parents=True, exist_ok=True)
    lock = Path(str(path) + '.lock').open('a+')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    manifest=Path(str(path)+'.owner.json')
    cleanup_stale_owner(manifest)
    path.unlink(missing_ok=True)
    stop = threading.Event(); counters = RpcTraceCounters(accept_pid=ProcessScope(args.parent_pid))
    health = {'measurement_scope': 'launch_process_tree' if args.parent_pid else 'local_user_processes',
              'last_capture_age_ms': None, 'trace_buffer_bytes': 0, 'max_trace_disk_bytes': 36 * 1024 * 1024}
    last_capture = None; source = None; source_usable=False
    def status():
        result = dict(health)
        if last_capture is not None:
            age = (time.monotonic_ns() - last_capture) / 1e6; result['last_capture_age_ms'] = age
            if age > max(6000., interval * 4000):
                result.update(state='unavailable', trace_ready=False,
                    reason=health.get('capture_error') or 'RPC trace capture is stale')
        return result
    server = RpcTraceServer(path, counters, status); os.chmod(path, 0o600)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .1}, daemon=True); thread.start()
    signal.signal(signal.SIGTERM, lambda *_: stop.set()); signal.signal(signal.SIGINT, lambda *_: stop.set())
    if args.parent_pid:
        import ctypes
        ctypes.CDLL(None).prctl(1, signal.SIGTERM)
        if os.getppid() != args.parent_pid: stop.set()
    try:
        if not stop.is_set():
            try:
                source = LttngSnapshotSource(counters, event_filter=args.event_filter)
                manifest.write_text(json.dumps({'session':source.session,'token':source.token,'directory':str(source.root),
                    'pid':os.getpid(),'process_start':process_info(os.getpid())[1]}));os.chmod(manifest,0o600)
                source.start(); source_usable=True;health['trace_buffer_bytes'] = source.buffer_bytes
            except Exception as exc:
                counters.set_ready(False, 'RPC tracing unavailable: ' + str(exc)[:240])
                if source is not None:
                    try: source.close()
                    except Exception: pass
                if source is not None and not source.created:
                    source = None;manifest.unlink(missing_ok=True)
        while not stop.wait(interval):
            if source is None or not source_usable: continue
            try:
                source.capture(); last_capture = time.monotonic_ns(); health.pop('capture_error',None);counters.set_ready(True)
            except Exception as exc:
                counters.report_loss(events=1, reason='Trace collection gap')
                health['capture_error']='RPC trace capture failed: '+str(exc)[:240]
                counters.set_ready(False, health['capture_error'])
    finally:
        counters.set_ready(False, 'RPC trace collector stopped')
        try:
            if source is not None: source.close()
        finally:
            if source is None or not source.created:manifest.unlink(missing_ok=True)
            server.shutdown(); server.server_close(); thread.join(timeout=1)
            path.unlink(missing_ok=True); lock.close()


if __name__ == '__main__': main()
