"""Existing ROS health topic reports capture readiness without a new publisher."""
import json
import time
from types import SimpleNamespace
import pytest
try:
    from builtin_interfaces.msg import Time
    from diagnostic_msgs.msg import DiagnosticStatus
    from soarm_station_system.runtime_node import RuntimeNode
    AVAILABLE=True
except ImportError:AVAILABLE=False
pytestmark=pytest.mark.skipif(not AVAILABLE,reason='Run with sourced ROS and Station interfaces')


def node(snapshot,age=0.):
    result=RuntimeNode.__new__(RuntimeNode);result.config={'arms':[],'cameras':[{'id':'forward'}]}
    result.snapshot={'arms':{},'cameras':{'forward':snapshot}};result.received_snapshot=time.monotonic()-age
    result.get_clock=lambda:SimpleNamespace(now=lambda:SimpleNamespace(to_msg=lambda:Time()))
    result.records=[];result.health=SimpleNamespace(publish=result.records.append)
    return result


def test_camera_health_preserves_mode_setter_diagnostics_and_omits_local_path():
    value={'id':'forward','state':'RECOVERING','ready':False,'error':'Decoded frame differs',
        'requested_mode':{'width':640,'height':480},'actual_mode':{'width':3264,'height':2448},
        'setter_results':{'width':{'accepted':False}},'restart_count':2,'age_ms':1e9,'path':'/private/frame.jpg'}
    instance=node(value);instance.publish_status();status=instance.records[-1].status[0]
    assert status.name=='camera:forward' and status.hardware_id=='camera:forward' and status.level==DiagnosticStatus.ERROR
    assert status.values[0].key=='status'
    record=json.loads(status.values[0].value)
    assert record['actual_mode']==value['actual_mode'] and record['setter_results']==value['setter_results']
    assert record['requested_mode']==value['requested_mode'] and record['restart_count']==2
    assert 'path' not in record and not record['ready']


def test_old_native_snapshot_cannot_keep_camera_ready():
    instance=node({'id':'forward','state':'READY','ready':True,'error':'','age_ms':10},age=2.)
    instance.publish_status();status=instance.records[-1].status[0];record=json.loads(status.values[0].value)
    assert not record['ready'] and record['state']=='RUNTIME_UNAVAILABLE' and record['age_ms']>=2010
    assert record['runtime_snapshot_age_ms']>=2000 and status.level==DiagnosticStatus.ERROR


def test_fresh_capture_is_distinct_from_dashboard_delivery_status():
    instance=node({'id':'forward','state':'READY','ready':True,'error':'','age_ms':5,'sequence':17})
    instance.publish_status();status=instance.records[-1].status[0];record=json.loads(status.values[0].value)
    assert status.level==DiagnosticStatus.OK and record['ready'] and record['sequence']==17
    assert 'delivery_ready' not in record # The dashboard measures that independently.


def test_not_ready_capture_cannot_publish_a_recent_previous_jpeg():
    instance=node({'id':'forward','ready':False,'path':'must-not-read.jpg','sequence':2,'age_ms':0})
    instance.camera_seq={};instance.camera_pubs={}
    instance.publish_cameras()
    assert instance.camera_seq=={}
