"""A delayed source cleanup must not stop a replacement operation."""
from pathlib import Path
import time
from soarm_station.config import load
from soarm_station.control import ArmController


def arm():
    config=load(Path(__file__).resolve().parents[1]/'config/mock.yaml')
    a=ArmController(config['arms'][0],config,'boot',{})
    a.q={j['name']:0. for j in a.config['joints']};a.gripper=50.;a.sample_time=time.monotonic()
    a.backend.q=dict(a.q);a.backend.gripper=50.
    a.set_permission(True,'operator',a.session)
    a.begin({'principal':'operator','runtime_boot_id':'boot','controller_session':a.session,'operation_type':'DIRECT','mode':'STREAM'})
    return a


def test_old_source_never_holds_a_replacement_operation(monkeypatch):
    monkeypatch.setattr('soarm_station.control.now',time.monotonic)
    a=arm();operation=a.op['operation_id']
    for session,principal,identifier in [(a.session,'other',operation),('old','operator',operation),(a.session,'operator','old-operation'),(a.session,'operator',None)]:
        assert not a.hold_owned(session,principal,identifier)['success']
        assert a.op['operation_id']==operation and a.permission and not a.hold_pending
    monkeypatch.setattr(a.hold_event,'wait',lambda timeout:True)
    assert a.hold_owned(a.session,'operator',operation)['success']
    assert a.op is None and not a.permission and a.hold_pending
    a.pool.shutdown(wait=False)


def test_unadmitted_source_cleanup_only_revokes_its_own_idle_grant(monkeypatch):
    monkeypatch.setattr('soarm_station.control.now',time.monotonic)
    a=arm();a._hold('TEST');a.hold_pending=False;a.hold_event.set()
    a.set_permission(True,'replacement',a.session)
    assert not a.hold_owned(a.session,'operator',None)['success']
    assert a.permission and a.principal=='replacement'
    monkeypatch.setattr(a.hold_event,'wait',lambda timeout:True)
    assert a.hold_owned(a.session,'replacement',None)['success']
    assert not a.permission
    a.pool.shutdown(wait=False)
