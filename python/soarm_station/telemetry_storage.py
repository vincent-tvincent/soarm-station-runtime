"""Recoverable telemetry storage, independent of ROS and motor control."""
import os
import json
from uuid import uuid4
from pathlib import Path
import sqlite3
import stat
import threading
import time

from .journal import Journal


class StorageUnavailable(OSError):
    pass


class StorageWaiting(StorageUnavailable):
    pass


def validate_storage(config):
    """Read-only mount/device checks; no directory or database creation."""
    monitoring=config.get('monitoring',{})
    configured=monitoring.get('storage_mount')
    if configured is None:return None
    mount=Path(configured).expanduser().resolve()
    database=Path(config['database']).expanduser().resolve()
    if database==mount or not database.is_relative_to(mount):
        raise ValueError('Telemetry database must be under monitoring.storage_mount')
    if not os.path.ismount(mount):
        raise StorageWaiting(f'Required telemetry storage mount is not mounted: {mount}')
    device=mount.stat().st_dev
    uuid=monitoring.get('storage_uuid')
    if uuid:
        matches=[]
        for spelling in dict.fromkeys((uuid,uuid.lower(),uuid.upper())):
            try:matches.append((Path('/dev/disk/by-uuid')/spelling).stat())
            except FileNotFoundError:pass
        if not matches:
            raise StorageWaiting('Configured telemetry storage UUID is not present')
        if not any(stat.S_ISBLK(value.st_mode) and value.st_rdev==device for value in matches):
            raise StorageWaiting('Mounted telemetry storage does not match the configured UUID')
    if os.statvfs(mount).f_flag & getattr(os,'ST_RDONLY',1):
        raise StorageWaiting('Configured telemetry storage is mounted read-only')
    # A lazy unmount/remount can reuse st_dev; Linux mount IDs distinguish it.
    mount_id=None
    try:
        for line in Path('/proc/self/mountinfo').read_text().splitlines():
            fields=line.split()
            point=fields[4]
            for encoded,decoded in (('\\040',' '),('\\011','\t'),('\\012','\n'),('\\134','\\')):
                point=point.replace(encoded,decoded)
            if point==str(mount):mount_id=fields[0]
    except FileNotFoundError:pass
    return device,mount_id


class RecoveringJournal:
    """One journal, serialized with removal/reopen; no in-memory sample queue.

    All SQLite access goes through call(), including upload acknowledgement.
    Calls check the required mount before touching SQLite. Failed or removed
    sessions are closed, and opening is retried at a bounded cadence.
    """
    def __init__(self,config,initialize,*,factory=Journal,validator=validate_storage,
                 clock=time.monotonic,retry_s=5.):
        self.config=config;self.initialize=initialize;self.factory=factory
        self.validator=validator;self.clock=clock;self.retry_s=retry_s
        self.lock=threading.RLock();self.journal=None;self.identity=None
        self.state='waiting';self.reason='Telemetry storage is initializing'
        self.generation=0;self.next_retry=0.;self.closed=False
        self.mount_fd=None
        self.last_counts={'local_committed':0,'remote_committed':0,'queued':0}
        self.counts_known=False

    def _detach(self,error):
        journal=self.journal;self.journal=None;self.identity=None;self.generation+=1
        self.state='waiting' if isinstance(error,StorageWaiting) else 'fault'
        self.reason=str(error)[:512];self.next_retry=self.clock()+self.retry_s
        if journal is not None:
            try:journal.close()
            except Exception:pass
        if self.mount_fd is not None:os.close(self.mount_fd);self.mount_fd=None

    def _check(self):
        identity=self.validator(self.config)
        if self.journal is not None and identity!=self.identity:
            raise StorageWaiting('Telemetry storage mount changed; reopening its journal')
        return identity

    def poll(self):
        with self.lock:
            if self.closed:return False
            if self.journal is not None:
                try:self._check();return True
                except Exception as error:self._detach(error);return False
            if self.clock()<self.next_retry:return False
            journal=None
            try:
                identity=self._check()
                monitoring=self.config.get('monitoring',{})
                if monitoring.get('storage_mount'):
                    self.mount_fd=os.open(monitoring['storage_mount'],os.O_RDONLY|os.O_DIRECTORY)
                    if os.fstat(self.mount_fd).st_dev!=identity[0] or self.validator(self.config)!=identity:
                        raise StorageWaiting('Telemetry mount changed before opening its journal')
                journal=self.factory(self.config['database'],monitoring.get('storage_limit_mb',512),
                    retention_days=monitoring.get('retention_days',30),
                    preserve_unacked=bool(self.config.get('collector',{}).get('url')),
                    event_limit=monitoring.get('event_limit',10000))
                if self.validator(self.config)!=identity:
                    raise StorageWaiting('Telemetry storage changed while opening its journal')
                self.initialize(journal)
                counts=dict(journal.counts())
                self.journal=journal;self.identity=identity;self.generation+=1
                self.state='ready';self.reason='';self.last_counts=counts;self.counts_known=True
                return True
            except Exception as error:
                if journal is not None:
                    try:journal.close()
                    except Exception:pass
                try:self.validator(self.config)
                except StorageWaiting as missing:error=missing
                except Exception:pass
                self._detach(error);return False

    def call(self,method,*args,generation=None,**kwargs):
        with self.lock:
            if self.journal is None or self.closed:raise StorageUnavailable(self.reason)
            try:self._check()
            except Exception as error:
                self._detach(error);raise StorageUnavailable(self.reason) from error
            if generation is not None and generation!=self.generation:
                raise StorageUnavailable('Telemetry journal changed during upload; acknowledgement deferred')
            try:
                value=getattr(self.journal,method)(*args,**kwargs)
                if method=='counts':self.last_counts=dict(value);self.counts_known=True
                return value
            except (OSError,sqlite3.Error) as error:
                self._detach(error)
                raise StorageUnavailable(self.reason) from error

    def pending(self):
        with self.lock:
            value=self.call('pending')
            return self.generation,value

    def status(self):
        with self.lock:
            return {'state':self.state,'available':self.journal is not None and self.state=='ready',
                'reason':self.reason,'generation':self.generation,
                'counts':dict(self.last_counts),'counts_known':self.counts_known}

    def close(self):
        with self.lock:
            self.closed=True;self.state='waiting';self.reason='Telemetry storage stopped'
            if self.journal is not None:
                journal=self.journal;self.journal=None
                try:journal.close()
                except Exception:pass
            if self.mount_fd is not None:os.close(self.mount_fd);self.mount_fd=None


def configure_monitoring_schedule(journal,period_ns,wall_ns,run_id,boot_id):
    """Commit cadence and its evidence together; never fabricate old readings."""
    previous=journal.schedule_info()
    old_period=previous.get('period_ns');old_next=previous.get('next_ns')
    changed=old_period is not None and old_period!=period_ns
    migrating=old_period is None and previous.get('has_samples',False)
    event=None
    if changed or migrating:
        next_ns=max(wall_ns,old_next) if old_next is not None else wall_ns
        gap_start=old_next if old_next is not None and old_next<next_ns else None
        missed=(next_ns-old_next+old_period-1)//old_period if gap_start is not None and old_period else (0 if gap_start is None else None)
        detail={'old_period_s':None if old_period is None else old_period/1e9,
            'new_period_s':period_ns/1e9,'effective_from_ns':next_ns,
            'previous_next_ns':old_next,'missed_old_slots_per_arm':missed,
            'gap_start_ns':gap_start,'gap_end_exclusive_ns':next_ns if gap_start is not None else None}
        event={'event_id':uuid4().hex,'source_role':'telemetry_node','source_boot_id':boot_id,
            'arm_id':'','operation_or_run_id':run_id,'time':{'sec':wall_ns//10**9,'nanosec':wall_ns%10**9},
            'severity':'INFO','code':'MONITORING_PERIOD_CHANGED' if changed else 'MONITORING_SCHEDULE_MIGRATED',
            'detail':json.dumps(detail,separators=(',',':'),allow_nan=False)}
        if len(event['detail'])>512:raise ValueError('Cadence transition detail exceeds Event bound')
    else:next_ns=wall_ns if old_next is None else old_next
    # The journal transaction includes the event and both schedule metadata keys.
    # A storage error must not silently change cadence without its evidence.
    committed=journal.configure_schedule(period_ns,next_ns,event=event)
    return next_ns,committed.get('event')


def resume_monitoring_schedule(journal,period_ns,wall_ns,run_id,boot_id,*,gap_start_ns=None,rejected_events=0,stale_ns=500_000_000):
    """Represent unavailable time once, without a backlog of invented samples."""
    persisted_next=journal.schedule_info().get('next_ns')
    next_ns,cadence_event=configure_monitoring_schedule(journal,period_ns,wall_ns,run_id,boot_id)
    events=[cadence_event] if cadence_event else []
    if gap_start_ns is not None:
        # A previous append/recovery transaction can be durable even if a later
        # read failed. Never repeat already committed schedule coverage.
        covered_through=next_ns if cadence_event else persisted_next
        if covered_through is not None:gap_start_ns=max(gap_start_ns,covered_through)
    start=next_ns if next_ns<wall_ns-stale_ns else None
    if gap_start_ns is not None and gap_start_ns<wall_ns:
        start=min(start,gap_start_ns) if start is not None else gap_start_ns
    if (start is not None and start<wall_ns) or rejected_events:
        missed=max(0,(wall_ns-start+period_ns-1)//period_ns) if start is not None else 0
        resumed=max(next_ns,start+missed*period_ns) if start is not None else next_ns
        detail={'gap_start_ns':start,'gap_end_exclusive_ns':resumed if start is not None else None,'period_s':period_ns/1e9,
            'missed_slots_per_arm':missed,'event_requests_rejected':rejected_events,
            'representation':'gap_event_only'}
        event={'event_id':uuid4().hex,'source_role':'telemetry_node','source_boot_id':boot_id,
            'arm_id':'','operation_or_run_id':run_id,'time':{'sec':wall_ns//10**9,'nanosec':wall_ns%10**9},
            'severity':'WARN','code':'TELEMETRY_STORAGE_RECOVERED',
            'detail':json.dumps(detail,separators=(',',':'),allow_nan=False)}
        if len(event['detail'])>512:raise ValueError('Storage gap detail exceeds Event bound')
        committed=journal.configure_schedule(period_ns,resumed,event=event)
        events.append(committed['event']);next_ns=resumed
    return next_ns,events


