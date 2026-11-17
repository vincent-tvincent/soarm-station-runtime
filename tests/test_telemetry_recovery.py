import json
import os
from pathlib import Path
import sqlite3
import stat

import pytest

from soarm_station.journal import Journal
from soarm_station.telemetry_storage import (RecoveringJournal,StorageUnavailable,
    StorageWaiting,resume_monitoring_schedule,validate_storage)


class Clock:
    value=0.
    def __call__(self):return self.value


def sample(stamp=100):
    return {'device_id':'device','runtime_boot_id':'boot','scheduled_ns':stamp,
        'arm_id':'arm','valid':True,'positions':{'joint':.1}}


def setup_storage(tmp_path):
    mount=tmp_path/'ssd';mount.mkdir()
    config={'database':str(mount/'history/data.sqlite3'),'monitoring':{'storage_mount':str(mount)}}
    online=[False];identity=[1];clock=Clock();opened=[]
    def validate(_):
        if not online[0]:raise StorageWaiting('Required telemetry storage mount is not mounted')
        return mount.stat().st_dev,identity[0]
    def factory(*args,**kwargs):
        opened.append(args[0]);return Journal(*args,**kwargs)
    storage=RecoveringJournal(config,lambda journal:None,factory=factory,validator=validate,clock=clock)
    return storage,mount,online,identity,clock,opened


def test_missing_storage_waits_without_journal_or_fallback_and_retries_bounded(tmp_path):
    storage,mount,online,_,clock,opened=setup_storage(tmp_path)
    try:
        for _ in range(50):assert not storage.poll()
        assert storage.status()['state']=='waiting' and not opened
        assert not (mount/'history').exists()
        online[0]=True;clock.value=4.9
        assert not storage.poll() and not opened
        clock.value=5.;assert storage.poll() and len(opened)==1
        assert storage.status()['available'] and storage.status()['state']=='ready'
    finally:storage.close()


def test_loss_after_readiness_closes_session_before_any_database_call(tmp_path):
    storage,mount,online,_,clock,opened=setup_storage(tmp_path)
    try:
        online[0]=True;assert storage.poll()
        storage.call('append',[sample()],110)
        assert storage.call('counts')['local_committed']==1
        old=storage.journal;online[0]=False
        with pytest.raises(StorageUnavailable):storage.call('append',[sample(110)],120)
        with pytest.raises(sqlite3.ProgrammingError):old.counts()
        assert storage.status()['counts']['local_committed']==1
        assert not storage.status()['available']
        online[0]=True;clock.value+=5.
        assert storage.poll() and storage.call('counts')['local_committed']==1
        assert len(opened)==2
    finally:storage.close()


def test_mount_identity_change_reopens_and_old_upload_ack_cannot_touch_new_session(tmp_path):
    storage,_,online,identity,clock,_=setup_storage(tmp_path)
    try:
        online[0]=True;assert storage.poll()
        storage.call('append',[sample()],110)
        generation,pending=storage.pending()
        identity[0]+=1
        assert not storage.poll() and storage.status()['state']=='waiting'
        clock.value+=5.;assert storage.poll()
        with pytest.raises(StorageUnavailable,match='changed during upload'):
            storage.call('acknowledge',pending,generation=generation)
        assert storage.call('counts')['remote_committed']==0
        assert storage.status()['available']
    finally:storage.close()


def test_io_fault_is_reported_and_recovers_without_process_restart(tmp_path):
    storage,_,online,_,clock,_=setup_storage(tmp_path)
    try:
        online[0]=True;assert storage.poll()
        def failed(*args):raise sqlite3.OperationalError('disk I/O error')
        storage.journal.append=failed
        with pytest.raises(StorageUnavailable,match='disk I/O error'):
            storage.call('append',[sample()],110)
        assert storage.status()['state']=='fault' and storage.journal is None
        clock.value+=5.;assert storage.poll()
        assert storage.call('append',[sample()],110)[0]['sequence']==1
    finally:storage.close()


def test_database_initialization_failure_closes_partial_connection(tmp_path,monkeypatch):
    closed=[]
    class FailedDatabase:
        def execute(self,*args):raise sqlite3.OperationalError('disk I/O error during open')
        def close(self):closed.append(True)
    monkeypatch.setattr('soarm_station.journal.sqlite3.connect',lambda *args,**kwargs:FailedDatabase())
    with pytest.raises(sqlite3.OperationalError,match='during open'):
        Journal(tmp_path/'history.sqlite3')
    assert closed==[True]


def test_upload_accessor_never_initializes_storage_on_its_background_thread(tmp_path):
    storage,_,online,_,_,opened=setup_storage(tmp_path)
    try:
        online[0]=True
        with pytest.raises(StorageUnavailable):storage.pending()
        assert not opened
        assert storage.poll()
        generation,_=storage.pending()
        assert generation==storage.generation
    finally:storage.close()


def test_resume_commits_one_gap_event_without_synthetic_backfill(tmp_path):
    journal=Journal(tmp_path/'history.sqlite3',preserve_unacked=False)
    try:
        period=10_000_000_000
        journal.configure_schedule(period,100_000_000_000)
        journal.append([sample(100_000_000_000)],110_000_000_000)
        resumed,events=resume_monitoring_schedule(journal,period,175_000_000_000,'run','boot',
            gap_start_ns=110_000_000_000,rejected_events=3)
        assert resumed==180_000_000_000 and len(events)==1
        detail=json.loads(events[0]['detail'])
        assert detail['missed_slots_per_arm']==7
        assert detail['gap_start_ns']==110_000_000_000 and detail['gap_end_exclusive_ns']==180_000_000_000
        assert detail['event_requests_rejected']==3 and detail['representation']=='gap_event_only'
        assert journal.counts()['local_committed']==1 and journal.meta('next_ns')==resumed
        assert journal.events()==events
        again,duplicate=resume_monitoring_schedule(journal,period,175_000_000_000,'run','boot',
            gap_start_ns=110_000_000_000)
        assert again==resumed and duplicate==[] and journal.events()==events
    finally:journal.close()


def test_recovery_does_not_skip_a_current_still_fresh_schedule_slot(tmp_path):
    journal=Journal(tmp_path/'history.sqlite3')
    try:
        journal.configure_schedule(10_000_000_000,100_000_000_000)
        assert resume_monitoring_schedule(journal,10_000_000_000,100_100_000_000,'run','boot')==(100_000_000_000,[])
    finally:journal.close()


def test_short_outage_between_slots_does_not_invent_missing_measurements(tmp_path):
    journal=Journal(tmp_path/'history.sqlite3')
    try:
        journal.configure_schedule(10_000_000_000,100_000_000_000)
        assert resume_monitoring_schedule(journal,10_000_000_000,97_000_000_000,'run','boot',
            gap_start_ns=100_000_000_000)==(100_000_000_000,[])
        next_ns,events=resume_monitoring_schedule(journal,10_000_000_000,98_000_000_000,'run','boot',
            gap_start_ns=100_000_000_000,rejected_events=2)
        assert next_ns==100_000_000_000 and len(events)==1
        detail=json.loads(events[0]['detail'])
        assert detail['missed_slots_per_arm']==0 and detail['gap_start_ns'] is None
        assert detail['event_requests_rejected']==2
    finally:journal.close()


def test_gap_event_failure_does_not_advance_persistent_schedule(tmp_path):
    journal=Journal(tmp_path/'history.sqlite3',event_limit=1)
    try:
        journal.configure_schedule(10_000_000_000,100_000_000_000)
        journal.event({'event_id':'existing'})
        with pytest.raises(OSError,match='event limit'):
            resume_monitoring_schedule(journal,10_000_000_000,200_000_000_000,'run','boot')
        assert journal.meta('next_ns')==100_000_000_000
        assert journal.counts()['local_committed']==0
    finally:journal.close()


def test_uuid_requires_exact_mounted_device_before_open(tmp_path,monkeypatch):
    mount=tmp_path/'ssd';mount.mkdir()
    config={'database':str(mount/'history/db'),'monitoring':{'storage_mount':str(mount),'storage_uuid':'7EFF-9E69'}}
    original=Path.stat;device=mount.stat().st_dev;observed=[device+1]
    def stat_path(path,*args,**kwargs):
        if str(path).startswith('/dev/disk/by-uuid/'):
            return type('Stat',(),{'st_mode':stat.S_IFBLK,'st_rdev':observed[0]})()
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'stat',stat_path)
    monkeypatch.setattr(os.path,'ismount',lambda path:path==mount)
    with pytest.raises(StorageWaiting,match='does not match'):validate_storage(config)
    assert not (mount/'history').exists()
    observed[0]=device
    assert validate_storage(config)[0]==device


@pytest.mark.skipif(os.geteuid()==0,reason='Root bypasses the service-user directory permission boundary')
def test_protected_underlay_prevents_fallback_creation_during_lazy_detach_race(tmp_path):
    mount=tmp_path/'ssd';mount.mkdir();detached=tmp_path/'detached'
    config={'database':str(mount/'history/data.sqlite3'),'monitoring':{'storage_mount':str(mount)}}
    mounted=[True];clock=Clock()
    def validate(_):
        if not mounted[0]:raise StorageWaiting('Storage unmounted')
        return mount.stat().st_dev,1
    def racing_factory(*args,**kwargs):
        mount.rename(detached);mount.mkdir(mode=0)
        mounted[0]=False
        return Journal(*args,**kwargs)
    storage=RecoveringJournal(config,lambda journal:None,factory=racing_factory,validator=validate,clock=clock)
    try:
        assert not storage.poll()
        assert storage.status()['state']=='waiting'
        mount.chmod(0o700)
        assert list(mount.iterdir())==[]
        assert not (detached/'history').exists()
    finally:
        mount.chmod(0o700);storage.close()
