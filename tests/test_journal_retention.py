import json
import sqlite3

import pytest

from soarm_station.journal import Journal


DAY=86400*10**9
NOW=100*DAY


def sample(stamp,arm='arm',**extra):
    return {'device_id':'station','runtime_boot_id':'boot','scheduled_ns':stamp,
            'arm_id':arm,'valid':True,'positions':{'joint':.1},**extra}


def test_default_constructor_preserves_existing_history_and_counts_contract(tmp_path):
    path=tmp_path/'journal.db'
    journal=Journal(path)
    saved=journal.append([sample(1),sample(2)],3)
    journal.acknowledge([saved[0],saved[0],{**saved[1],'device_id':'another-device'}])
    journal.event({'event_id':'event','detail':'retained'})
    assert journal.counts()=={'local_committed':2,'remote_committed':1,'queued':1}
    result=journal.maintain(NOW)
    assert result['cutoff_ns'] is None and result['deleted_samples']==result['deleted_events']==0
    journal.close()
    reopened=Journal(path)
    assert reopened.counts()=={'local_committed':2,'remote_committed':1,'queued':1}
    assert reopened.meta('next_ns')==3 and len(reopened.pending())==1
    assert reopened.events()[0]['event_id']=='event'
    reopened.close()


def test_local_retention_removes_only_strictly_older_rows_and_keeps_72_hour_course_evidence(tmp_path):
    journal=Journal(tmp_path/'journal.db',512,retention_days=30,preserve_unacked=False)
    cutoff=NOW-30*DAY
    stamps=[cutoff-1,cutoff,NOW-3*DAY,NOW-52*3600*10**9,NOW]
    saved=journal.append([sample(stamp) for stamp in stamps],NOW+1)
    assert journal.counts()['local_committed']==5  # Opening/writing never prunes.
    result=journal.maintain(NOW)
    assert result['cutoff_ns']==cutoff and result['deleted_samples']==1
    assert result['counts']=={'local_committed':4,'remote_committed':0,'queued':4}
    assert [entry['scheduled_ns'] for entry in journal.pending()]==stamps[1:]
    assert journal.meta('next_ns')==NOW+1
    assert saved[0]['sequence'] not in [entry['sequence'] for entry in journal.pending()]
    journal.close()


def test_remote_unacknowledged_records_are_never_pruned_even_when_old(tmp_path):
    journal=Journal(tmp_path/'journal.db',512,retention_days=30,preserve_unacked=True)
    saved=journal.append([sample(NOW-40*DAY),sample(NOW-40*DAY+1),sample(NOW-DAY)],NOW+1)
    journal.acknowledge([saved[1],saved[2]])
    result=journal.maintain(NOW)
    assert result['deleted_samples']==1
    assert result['counts']=={'local_committed':2,'remote_committed':1,'queued':1}
    assert journal.pending()[0]['sequence']==saved[0]['sequence']
    journal.close()


def test_legacy_schema_migrates_event_timestamps_without_deleting_or_guessing_unknown_age(tmp_path):
    path=tmp_path/'legacy.db';db=sqlite3.connect(path)
    db.executescript('''
        CREATE TABLE samples(sequence INTEGER PRIMARY KEY AUTOINCREMENT,device_id TEXT NOT NULL,
            boot_id TEXT NOT NULL,scheduled_ns INTEGER NOT NULL,arm_id TEXT NOT NULL,payload TEXT NOT NULL,
            acked INTEGER NOT NULL DEFAULT 0,UNIQUE(device_id,boot_id,sequence));
        CREATE TABLE events(event_id TEXT PRIMARY KEY,payload TEXT NOT NULL);
        CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    ''')
    old=NOW-40*DAY
    db.execute('INSERT INTO samples(device_id,boot_id,scheduled_ns,arm_id,payload) VALUES(?,?,?,?,?)',
               ('station','boot',old,'arm',json.dumps(sample(old))))
    entries=[('old_ns',{'time_ns':old}),('old_ros',{'time':{'sec':old//10**9,'nanosec':1}}),
             ('recent',{'time_ns':NOW-DAY}),('unknown',{'detail':'no timestamp'}),('zero',{'time':{'sec':0,'nanosec':0}})]
    for event_id,payload in entries:db.execute('INSERT INTO events VALUES(?,?)',(event_id,json.dumps(payload)))
    db.execute('INSERT INTO events VALUES(?,?)',('invalid_json','not-json'))
    db.commit();db.close()
    journal=Journal(path,512,retention_days=30,preserve_unacked=False)
    assert journal.counts()['local_committed']==1
    assert journal.db.execute('SELECT count(*) FROM events').fetchone()[0]==6
    ages=dict(journal.db.execute('SELECT event_id,recorded_ns FROM events'))
    assert ages['old_ns']==old and ages['old_ros']==old+1
    assert all(ages[key] is None for key in ('unknown','zero','invalid_json'))
    result=journal.maintain(NOW)
    assert result['deleted_samples']==1 and result['deleted_events']==2
    assert {row[0] for row in journal.db.execute('SELECT event_id FROM events')}=={'recent','unknown','zero','invalid_json'}
    journal.close()


def test_event_count_cap_preserves_recent_events_and_duplicate_ack_then_age_prune_frees_admission(tmp_path,monkeypatch):
    monkeypatch.setattr('soarm_station.journal.time.time_ns',lambda:NOW)
    journal=Journal(tmp_path/'journal.db',512,retention_days=30,preserve_unacked=False,event_limit=2)
    journal.event({'event_id':'old','time_ns':NOW-40*DAY})
    journal.event({'event_id':'recent','detail':'keep'})
    with pytest.raises(OSError,match='event limit'):journal.event({'event_id':'third'})
    assert len(journal.events())==2
    journal.event({'event_id':'recent','detail':'does not overwrite'})
    assert next(e for e in journal.events() if e['event_id']=='recent')['detail']=='keep'
    result=journal.maintain(NOW)
    assert result['deleted_events']==1 and result['events']==1
    journal.event({'event_id':'third'})
    assert journal.maintain(NOW)['event_limit_reached'] is True
    journal.close()


def test_sample_and_event_quota_rejections_are_atomic_and_leave_schedule_unchanged(tmp_path):
    journal=Journal(tmp_path/'journal.db',limit_mb=1)
    journal.append([sample(NOW)],NOW+1)
    before=journal.counts()
    with pytest.raises(OSError,match='storage limit'):
        journal.append([sample(NOW+1,note='x'*(2*1024*1024))],NOW+2)
    with pytest.raises(OSError,match='storage limit'):
        journal.event({'event_id':'oversized','detail':'x'*(2*1024*1024)})
    assert journal.counts()==before and journal.events()==[]
    assert journal.meta('next_ns')==NOW+1 and not journal.db.in_transaction
    assert not journal.storage()['over_limit']
    journal.close()


def test_pinned_wal_and_shm_are_counted_and_acknowledgements_do_not_bypass_quota(tmp_path):
    journal=Journal(tmp_path/'journal.db',limit_mb=8)
    saved=journal.append([sample(NOW)],NOW+1)
    journal.maintain(NOW)
    reader=sqlite3.connect(journal.path)
    reader.execute('BEGIN');reader.execute('SELECT count(*) FROM samples').fetchone()
    for offset in range(5):journal.append([sample(NOW+offset+1,note='x'*8000)],NOW+offset+2)
    usage=journal.storage()
    assert usage['total_bytes']==sum(usage[key] for key in ('database_bytes','wal_bytes','shm_bytes'))
    assert usage['wal_bytes']>0 and usage['shm_bytes']>0
    journal.limit_mb=(usage['accounted_bytes']-1)/(1024*1024)
    assert usage['database_bytes']<journal.limit_mb*1024*1024
    before=journal.counts()
    for write in (lambda:journal.append([sample(NOW+10)],NOW+11),
                  lambda:journal.event({'event_id':'blocked'}),lambda:journal.acknowledge(saved)):
        with pytest.raises(OSError,match='storage limit'):write()
    assert journal.counts()==before
    result=journal.maintain(NOW)
    assert result['checkpoint']['checkpointed_frames']<result['checkpoint']['log_frames']
    assert not result['checkpoint']['truncated']
    assert journal.db.execute('PRAGMA busy_timeout').fetchone()[0]==2000
    reader.rollback();reader.close();journal.close()


def test_expiry_and_reader_release_reclaim_wal_budget_and_allow_freelist_reuse_without_vacuum(tmp_path):
    journal=Journal(tmp_path/'journal.db',8,retention_days=30,preserve_unacked=False)
    journal.append([sample(NOW-52*3600*10**9)],NOW+1)
    journal.maintain(NOW)
    reader=sqlite3.connect(journal.path)
    reader.execute('BEGIN');reader.execute('SELECT count(*) FROM samples').fetchone()
    for offset in range(10):journal.append([sample(NOW-40*DAY+offset,note='x'*65536)],NOW+1)
    journal.limit_mb=1.6
    statements=[];journal.db.set_trace_callback(statements.append)
    first=journal.maintain(NOW)
    assert first['deleted_samples']==10 and first['counts']['local_committed']==1
    assert first['storage']['free_pages']>0 and not first['checkpoint']['truncated']
    with pytest.raises(OSError,match='storage limit'):journal.append([sample(NOW)],NOW+1)
    reader.rollback();reader.close()
    second=journal.maintain(NOW)
    assert second['checkpoint']['truncated'] and second['storage']['wal_bytes']==0
    pages=journal.db.execute('PRAGMA page_count').fetchone()[0]
    journal.append([sample(NOW)],NOW+1)
    assert journal.counts()['local_committed']==2
    assert journal.db.execute('PRAGMA page_count').fetchone()[0]<=pages
    assert not journal.storage()['over_limit']
    assert not any('VACUUM' in statement.upper() for statement in statements)
    journal.close()


def test_maintenance_is_bounded_resumable_and_cache_wal_settings_are_bounded(tmp_path):
    journal=Journal(tmp_path/'journal.db',512,retention_days=30,preserve_unacked=False)
    journal.MAINTENANCE_BATCH=2
    journal.append([sample(NOW-40*DAY+i) for i in range(5)],NOW+1)
    for expected_remaining in (3,1,0):
        result=journal.maintain(NOW)
        assert result['deleted_samples']<=2
        assert result['counts']['local_committed']==expected_remaining
        assert result['more_expired_samples']==(expected_remaining>0)
    assert journal.db.execute('PRAGMA cache_size').fetchone()[0]==-2048
    assert journal.db.execute('PRAGMA mmap_size').fetchone()[0]==0
    assert journal.db.execute('PRAGMA wal_autocheckpoint').fetchone()[0]==256
    assert journal.db.execute('PRAGMA journal_size_limit').fetchone()[0]==1048576
    journal.close()


def test_schedule_infers_distinct_legacy_cadence_and_atomically_records_explicit_transition(tmp_path):
    journal=Journal(tmp_path/'journal.db')
    assert journal.schedule_info()=={'next_ns':None,'period_ns':None,'has_samples':False}
    journal.append([sample(NOW),sample(NOW,arm='second'),sample(NOW+10**9)],NOW+2*10**9)
    assert journal.schedule_info()=={'next_ns':NOW+2*10**9,'period_ns':10**9,'has_samples':True}
    result=journal.configure_schedule(10*10**9,NOW+20*10**9,{'event_id':'cadence','time_ns':NOW,'code':'PERIOD_CHANGED'})
    assert result['event']['event_id']=='cadence'
    assert journal.schedule_info()=={'next_ns':NOW+20*10**9,'period_ns':10*10**9,'has_samples':True}
    assert journal.counts()['local_committed']==3
    assert journal.events()[0]['code']=='PERIOD_CHANGED'
    journal.close()


def test_schedule_event_rejection_rolls_back_both_meta_fields(tmp_path):
    journal=Journal(tmp_path/'journal.db',event_limit=1)
    journal.configure_schedule(10**9,NOW)
    journal.event({'event_id':'already-full','time_ns':NOW})
    with pytest.raises(OSError,match='event limit'):
        journal.configure_schedule(10*10**9,NOW+10*10**9,{'event_id':'transition','time_ns':NOW})
    assert journal.schedule_info()=={'period_ns':10**9,'next_ns':NOW,'has_samples':False}
    assert [event['event_id'] for event in journal.events()]==['already-full']
    with pytest.raises(ValueError):journal.configure_schedule(True,NOW)
    assert not journal.db.in_transaction
    journal.close()


def test_acknowledgements_use_smaller_quota_checked_batches_when_headroom_is_tight(tmp_path):
    journal=Journal(tmp_path/'journal.db',8)
    saved=journal.append([sample(NOW+offset) for offset in range(16)],NOW+17)
    journal.limit_mb=1
    journal.acknowledge(saved)
    assert journal.counts()=={'local_committed':16,'remote_committed':16,'queued':0}
    assert not journal.storage()['over_limit']
    changes=journal.db.total_changes
    journal.acknowledge(saved)
    assert journal.db.total_changes==changes
    journal.close()


def test_unchanged_startup_metadata_needs_no_write_even_above_quota(tmp_path):
    journal=Journal(tmp_path/'journal.db')
    journal.set_meta('monitoring_run_id','existing-run')
    journal.set_meta('start_ns',NOW)
    journal.configure_schedule(10*10**9,NOW+10*10**9)
    journal.limit_mb=.001
    changes=journal.db.total_changes
    journal.set_meta('monitoring_run_id','existing-run')
    journal.set_meta('start_ns',NOW)
    assert journal.configure_schedule(10*10**9,NOW+10*10**9)=={'period_ns':10*10**9,'next_ns':NOW+10*10**9,'event':None}
    assert journal.db.total_changes==changes
    with pytest.raises(OSError,match='storage limit'):journal.set_meta('monitoring_run_id','new-run')
    with pytest.raises(OSError,match='storage limit'):journal.configure_schedule(20*10**9,NOW+20*10**9)
    with pytest.raises(OSError,match='storage limit'):journal.event({'event_id':'new-data'})
    with pytest.raises(OSError,match='storage limit'):journal.append([sample(NOW)],NOW+1)
    assert journal.meta('monitoring_run_id')=='existing-run' and journal.counts()['local_committed']==0
    journal.close()
