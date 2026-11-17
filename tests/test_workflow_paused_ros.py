"""Paused owned streams are not idle drift; no ROS node or devices are started."""
from types import SimpleNamespace
import pytest

try:
    import numpy as np
    from sensor_msgs.msg import JointState
    from soarm_station_interfaces.msg import OperationStatus
    from soarm_station_system.workflow_node import WorkflowNode
    import soarm_station_system.workflow_node as workflow
    AVAILABLE=True
except ImportError:AVAILABLE=False

pytestmark=pytest.mark.skipif(not AVAILABLE,reason='Sourced ROS and Station interfaces required')


def owner(monkeypatch,phase,*,feedback_valid=True):
    clock=[100.];events=[]
    monkeypatch.setattr(workflow.time,'monotonic',lambda:clock[0])
    state=OperationStatus(arm_id='arm1',phase=phase,feedback_valid=feedback_valid)
    node=SimpleNamespace(config={'arms':[{'id':'arm1'}],
        'monitoring':{'stale_ms':500,'drift_rad':.15,'debounce_s':.05}},event_pending={},
        joints={},states={'arm1':state},references={'arm1':np.array([0.])},pending={},alerts={},
        emit=lambda *event:events.append(event))
    def tick(position):
        node.joints['arm1']=(JointState(name=['joint'],position=[position]),clock[0])
        WorkflowNode.rules(node);clock[0]+=.1
    return node,events,tick


def test_paused_stream_does_not_emit_idle_drift_as_last_goal_finishes(monkeypatch):
    node,events,tick=owner(monkeypatch,'PAUSED')
    tick(.3);tick(.6)
    assert events==[] and node.pending=={} and node.alerts=={}
    assert list(node.references['arm1'])==[.6]


def test_actual_idle_drift_is_still_reported(monkeypatch):
    node,events,tick=owner(monkeypatch,'IDLE')
    tick(.3);tick(.6)
    assert len(events)==1 and events[0][1]=='UNEXPECTED_POSE_CHANGE'
    assert list(node.references['arm1'])==[0.]


def test_paused_stream_still_reports_unavailable_feedback(monkeypatch):
    _,events,tick=owner(monkeypatch,'PAUSED',feedback_valid=False)
    tick(.3);tick(.6)
    assert len(events)==1 and events[0][1]=='FEEDBACK_UNAVAILABLE'
