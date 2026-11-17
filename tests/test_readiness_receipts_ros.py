"""Source-aged readiness receipts: no ROS node, sockets or devices are started."""
from types import SimpleNamespace
import pytest
try:
    from builtin_interfaces.msg import Time
    from diagnostic_msgs.msg import DiagnosticArray,DiagnosticStatus,KeyValue
    from rclpy.serialization import serialize_message
    from soarm_station_system.dashboard_node import DashboardNode
    import soarm_station_system.dashboard_node as dashboard
    AVAILABLE=True
except ImportError:AVAILABLE=False
pytestmark=pytest.mark.skipif(not AVAILABLE,reason='Run with sourced ROS and Station interfaces')


@pytest.mark.parametrize('key',['controller:arm1','image:forward','diagnostic:camera:forward'])
def test_delayed_or_repeated_message_never_renews_source_freshness(monkeypatch,key):
    clock={'wall':1000_000_000_000,'mono':10.}
    monkeypatch.setattr(dashboard.time,'time_ns',lambda:clock['wall'])
    monkeypatch.setattr(dashboard.time,'monotonic',lambda:clock['mono'])
    owner=SimpleNamespace(readiness_receipts={})
    stamp=Time(sec=999,nanosec=650_000_000)
    DashboardNode.source_receipt(owner,key,stamp)
    assert owner.readiness_receipts[key]==pytest.approx(9.65)
    clock['wall']+=1_500_000_000;clock['mono']+=1.5
    DashboardNode.source_receipt(owner,key,stamp)
    assert owner.readiness_receipts[key]==pytest.approx(9.65)
    assert clock['mono']-owner.readiness_receipts[key]==pytest.approx(1.85)


def test_future_source_timestamp_removes_prior_ready_evidence(monkeypatch):
    monkeypatch.setattr(dashboard.time,'time_ns',lambda:1000_000_000_000)
    monkeypatch.setattr(dashboard.time,'monotonic',lambda:10.)
    owner=SimpleNamespace(readiness_receipts={'controller:arm1':9.99})
    DashboardNode.source_receipt(owner,'controller:arm1',Time(sec=1001,nanosec=0))
    assert 'controller:arm1' not in owner.readiness_receipts


def test_old_retained_status_is_stale_immediately_on_receipt(monkeypatch):
    monkeypatch.setattr(dashboard.time,'time_ns',lambda:1000_000_000_000)
    monkeypatch.setattr(dashboard.time,'monotonic',lambda:10.)
    owner=SimpleNamespace(readiness_receipts={})
    DashboardNode.source_receipt(owner,'controller:arm1',Time(sec=995,nanosec=0))
    assert owner.readiness_receipts['controller:arm1']==pytest.approx(5.)
    from soarm_station.readiness import project_readiness
    result=project_readiness({'arms':[{'id':'arm1'}]},
        {'arm1':{'feedback_valid':True,'feedback_age_ms':0,'phase':'IDLE'}},owner.readiness_receipts,{},[],10.)
    assert result['components'][0]['state']=='waiting'


def test_real_diagnostic_array_callback_filters_ids_and_keeps_source_age(monkeypatch):
    monkeypatch.setattr(dashboard.time,'time_ns',lambda:1000_000_000_000)
    monkeypatch.setattr(dashboard.time,'monotonic',lambda:10.)
    owner=DashboardNode.__new__(DashboardNode)
    owner.config={'arms':[],'cameras':[{'id':'forward'}]}
    owner.readiness_receipts={};owner.component_diagnostics={}
    owner.motor_records={};owner.motor_record_errors={}
    message=DiagnosticArray();message.header.stamp=Time(sec=999,nanosec=750_000_000)
    message.status=[
        DiagnosticStatus(name='camera:forward',hardware_id='camera:forward',level=DiagnosticStatus.OK,
            message='READY',values=[KeyValue(key='status',value='{"ready":true,"age_ms":5}')]),
        DiagnosticStatus(name='/soarm_station/telemetry_node/storage',level=DiagnosticStatus.WARN,
            message='Waiting for mount',values=[KeyValue(key='storage_state',value='waiting')]),
        DiagnosticStatus(name='camera:unconfigured',level=DiagnosticStatus.ERROR,message='Ignore this resource'),
        DiagnosticStatus(name='unrelated',message='Ignore this component'),
    ]
    serialize_message(message)
    # This is the actual /health route, including the component-health callback.
    # Lyrical DiagnosticStatus.level is a byte value, not int-convertible text.
    DashboardNode.motor_health(owner,message)
    expected={'camera:forward','/soarm_station/telemetry_node/storage'}
    assert set(owner.component_diagnostics)==expected
    assert set(owner.readiness_receipts)=={'diagnostic:'+name for name in expected}
    assert all(value==pytest.approx(9.75) for value in owner.readiness_receipts.values())
    assert owner.component_diagnostics['camera:forward']['values']['status']=='{"ready":true,"age_ms":5}'
    assert owner.component_diagnostics['/soarm_station/telemetry_node/storage']['message']=='Waiting for mount'
    assert owner.motor_records=={}
