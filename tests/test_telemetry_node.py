import json
import types

import pytest

try:
    from soarm_station_system.telemetry_node import TelemetryNode,configure_monitoring_schedule
    from soarm_station.journal import Journal
    ROS_AVAILABLE=True
except ImportError:ROS_AVAILABLE=False
pytestmark=pytest.mark.skipif(not ROS_AVAILABLE,reason='Sourced ROS environment required')


def sample(stamp):
    return {'device_id':'test','runtime_boot_id':'boot','scheduled_ns':stamp,'arm_id':'arm1','valid':True,'positions':{'joint':.1}}


def test_first_schedule_initializes_without_inventing_a_cadence_change(tmp_path):
    journal=Journal(tmp_path/'history.sqlite3',preserve_unacked=False,retention_days=30)
    try:
        next_ns,event=configure_monitoring_schedule(journal,10_000_000_000,100_000_000_000,'run','boot')
        assert next_ns==100_000_000_000 and event is None
        assert journal.meta('next_ns')==next_ns and journal.meta('period_ns')==10_000_000_000
        assert journal.events()==[] and journal.counts()['local_committed']==0
    finally:journal.close()


def test_startup_maintains_before_metadata_and_passes_bounded_local_defaults(monkeypatch):
    import soarm_station_system.telemetry_node as module
    calls=[];options={}
    class FakeJournal:
        def __init__(self,path,limit_mb,**kwargs):calls.append('open');options.update(limit_mb=limit_mb,**kwargs)
        def maintain(self,stamp):calls.append('maintain');return {'deleted_samples':0}
        def meta(self,key,default=None):calls.append('meta');return default
        def set_meta(self,key,value):calls.append('set_meta')
        def schedule_info(self):return {'next_ns':None,'period_ns':None,'has_samples':False}
        def configure_schedule(self,period_ns,next_ns,event=None):calls.append('configure');return {'event':event}
        def close(self):calls.append('close')
    monkeypatch.setattr(module,'Journal',FakeJournal)
    monkeypatch.setattr(module.BaseNode,'__init__',lambda self,name:setattr(self,'config',{'database':'unused','arms':[]}))
    monkeypatch.setattr(TelemetryNode,'create_publisher',lambda *args,**kwargs:types.SimpleNamespace(publish=lambda message:None))
    for method in ('create_subscription','create_service','create_timer'):
        monkeypatch.setattr(TelemetryNode,method,lambda *args,**kwargs:None)
    node=TelemetryNode()
    try:
        assert calls[:2]==['open','maintain']
        assert options=={'limit_mb':512,'retention_days':30,'preserve_unacked':False,'event_limit':10000}
        assert node.period_ns==10_000_000_000 and not node.collector_enabled and node.error==''
    finally:
        node.stop.set();node.uploader.join(timeout=1);node.journal.close()


def test_missing_required_mount_prevents_journal_construction(tmp_path,monkeypatch):
    import soarm_station_system.telemetry_node as module
    mount=tmp_path/'ssd'
    config={'database':str(mount/'history/telemetry.sqlite3'),'arms':[],
        'monitoring':{'storage_mount':str(mount)}}
    monkeypatch.setattr(module.BaseNode,'__init__',lambda self,name:setattr(self,'config',config))
    monkeypatch.setattr('soarm_station.config.os.path.ismount',lambda path:False)
    def open_journal(*args,**kwargs):pytest.fail('Journal must not open or create fallback directories')
    monkeypatch.setattr(module,'Journal',open_journal)
    with pytest.raises(OSError,match='storage mount is not mounted'):TelemetryNode()
    assert not mount.exists()


def test_legacy_one_second_history_transitions_with_exact_gap_event_and_no_fake_rows(tmp_path):
    from rosidl_runtime_py.set_message import set_message_fields
    from rclpy.serialization import serialize_message
    from soarm_station_interfaces.msg import Event
    journal=Journal(tmp_path/'history.sqlite3',preserve_unacked=False,retention_days=30)
    try:
        journal.append([sample(100_000_000_000)],101_000_000_000)
        journal.append([sample(101_000_000_000)],102_000_000_000)
        before=journal.history()
        assert journal.meta('period_ns') is None
        next_ns,event=configure_monitoring_schedule(journal,10_000_000_000,105_250_000_000,'run','boot')
        assert next_ns==105_250_000_000
        assert journal.history()==before and journal.counts()['local_committed']==2
        assert event['code']=='MONITORING_PERIOD_CHANGED'
        detail=json.loads(event['detail'])
        assert detail=={'old_period_s':1.,'new_period_s':10.,'effective_from_ns':next_ns,
            'previous_next_ns':102_000_000_000,'missed_old_slots_per_arm':4,
            'gap_start_ns':102_000_000_000,'gap_end_exclusive_ns':next_ns}
        assert len(event['detail'])<=512 and journal.events()[0]==event
        message=Event();set_message_fields(message,event);serialize_message(message)
        assert journal.meta('period_ns')==10_000_000_000 and journal.meta('next_ns')==next_ns
    finally:journal.close()


@pytest.mark.parametrize('wall_ns,expected_next,missed',[
    (99_000_000_000,100_000_000_000,0),
    (100_000_000_000,100_000_000_000,0),
    (100_000_000_001,100_000_000_001,1),
    (101_000_000_000,101_000_000_000,1),
    (101_000_000_001,101_000_000_001,2),
])
def test_period_change_gap_interval_is_exclusive_at_effective_time(wall_ns,expected_next,missed):
    committed=[]
    def configure(period,next_ns,event=None):
        committed.append((period,next_ns,event));return {'event':event}
    journal=types.SimpleNamespace(
        schedule_info=lambda:{'period_ns':1_000_000_000,'next_ns':100_000_000_000,'has_samples':True},
        configure_schedule=configure)
    next_ns,event=configure_monitoring_schedule(journal,10_000_000_000,wall_ns,'run','boot')
    assert next_ns==expected_next and json.loads(event['detail'])['missed_old_slots_per_arm']==missed
    assert len(committed)==1


def test_same_period_preserves_old_schedule_and_storage_error_is_not_ignored():
    calls=[]
    journal=types.SimpleNamespace(schedule_info=lambda:{'period_ns':10,'next_ns':20,'has_samples':True},
        configure_schedule=lambda period,next_ns,event=None:calls.append((period,next_ns,event)) or {'event':event})
    assert configure_monitoring_schedule(journal,10,100,'run','boot')==(20,None)
    assert calls==[(10,20,None)]
    def fail(*args,**kwargs):raise OSError('event budget full')
    journal.configure_schedule=fail
    with pytest.raises(OSError,match='event budget full'):
        configure_monitoring_schedule(journal,20,100,'run','boot')


@pytest.mark.parametrize('collector_enabled,expected_queue',[(False,0),(True,96)])
def test_upload_status_keeps_local_and_remote_truth_without_local_only_error(collector_enabled,expected_queue):
    from builtin_interfaces.msg import Time
    published=[];counts={'local_committed':100,'remote_committed':4,'queued':96}
    owner=types.SimpleNamespace(journal=types.SimpleNamespace(counts=lambda:counts),collector_enabled=collector_enabled,
        error='',storage_error='',maintenance_error='',
        get_clock=lambda:types.SimpleNamespace(now=lambda:types.SimpleNamespace(to_msg=lambda:Time(sec=1))),
        upload_pub=types.SimpleNamespace(publish=published.append))
    TelemetryNode.publish_upload(owner)
    assert published[0].local_committed==100 and published[0].remote_committed==4
    assert published[0].queued==expected_queue and published[0].error==''
    assert counts['queued']==96  # Presentation does not forge an acknowledgment.


def test_ten_second_sampling_does_not_fill_old_slots_with_a_current_observation(monkeypatch):
    import soarm_station_system.telemetry_node as module
    wall=30_100_000_000;source=30_095_000_000;rows=[];published=[]
    monkeypatch.setattr(module.time,'time_ns',lambda:wall)
    def append(batch,next_ns):
        committed=[{**row,'sequence':len(rows)+i+1} for i,row in enumerate(batch)]
        rows.extend(committed);return committed
    measured=types.SimpleNamespace(header=types.SimpleNamespace(stamp=types.SimpleNamespace(sec=source//10**9,nanosec=source%10**9)),
        name=['joint'],position=[.1])
    state=types.SimpleNamespace(runtime_boot_id='boot',feedback_valid=True,phase='IDLE',measured_torque='ON')
    owner=types.SimpleNamespace(config={'device_id':'test','arms':[{'id':'arm1'}],'monitoring':{'stale_ms':500}},
        cache={'arm1':(measured,0.)},states={'arm1':state},health={},period_ns=10_000_000_000,
        next_ns=10_100_000_000,run_id='run',boot='boot',storage_error='',
        journal=types.SimpleNamespace(append=append),pub=types.SimpleNamespace(publish=published.append),maintain_if_due=lambda wall:None)
    TelemetryNode.sample(owner)
    assert [row['valid'] for row in rows]==[False,False,True]
    assert [row['positions'] for row in rows]==[{}, {}, {'joint':.1}]
    assert all(row['reason']=='MISSING_STALE_OR_SCHEDULE_GAP' for row in rows[:2])
    assert len(published)==3 and owner.next_ns==40_100_000_000


def test_hourly_maintenance_runs_once_per_due_period_and_reports_errors(monkeypatch):
    import soarm_station_system.telemetry_node as module
    clock=[10.];calls=[]
    monkeypatch.setattr(module.time,'monotonic',lambda:clock[0])
    owner=types.SimpleNamespace(maintenance_due=0.,maintenance_error='',maintenance_status=None,
        journal=types.SimpleNamespace(maintain=lambda stamp:calls.append(stamp) or {'deleted_samples':0}))
    TelemetryNode.maintain_if_due(owner,100)
    clock[0]=20.;TelemetryNode.maintain_if_due(owner,200)
    assert calls==[100] and owner.maintenance_due==3610.
    clock[0]=3610.;TelemetryNode.maintain_if_due(owner,300)
    assert calls==[100,300] and owner.maintenance_status=={'deleted_samples':0}
    def fail(stamp):raise OSError('maintenance unavailable')
    owner.journal.maintain=fail;clock[0]=7210.
    TelemetryNode.maintain_if_due(owner,400)
    assert 'maintenance unavailable' in owner.maintenance_error
