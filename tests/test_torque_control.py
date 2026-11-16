"""Explicit torque changes use simulated devices and the real owner thread."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time

import pytest

from soarm_station.config import load
from soarm_station.control import ArmController
from soarm_station.hardware import LeRobotArm,MockArm,ReadOnlySO101,TorqueConfirmationError
from soarm_station.worker import Runtime


class Bus:
    def __init__(self,torque):
        self.torque=dict(torque);self.events=[]
        self.present={n:2000+i*31 for i,n in enumerate(torque)}
        self.goals={n:400 for n in torque};self.enable_effect=True
    def sync_read(self,register,normalize=True):
        self.events.append(('read',register,normalize))
        return dict(self.torque if register=='Torque_Enable' else self.present)
    def sync_write(self,register,values,normalize=True):
        self.events.append(('write',register,dict(values),normalize));self.goals=dict(values)
    def enable_torque(self):
        self.events.append(('enable',))
        if self.enable_effect:self.torque=dict.fromkeys(self.torque,1)
    def disable_torque(self):
        self.events.append(('disable',));self.torque=dict.fromkeys(self.torque,0)


def native(values):
    arm=LeRobotArm({'joints':[{'name':'joint'}]});arm.gripper_name='gripper';arm.bus=Bus(values)
    return arm


@pytest.mark.parametrize('initial',[{'joint':0,'gripper':0},{'joint':1,'gripper':0}])
def test_enable_stages_current_raw_measurement_before_any_enable(initial):
    arm=native(initial);old_goals=dict(arm.bus.goals)
    result=arm.set_torque(True)
    assert result=={'torque':'ON','writes':1} and arm.dispatch_ready
    writes=[entry for entry in arm.bus.events if entry[0]=='write']
    assert writes==[('write','Goal_Position',arm.bus.present,False)]
    assert arm.bus.goals==arm.bus.present and arm.bus.goals!=old_goals
    assert arm.bus.events.index(writes[0])<arm.bus.events.index(('enable',))
    assert arm.bus.events[-1]==('read','Torque_Enable',False)


def test_disable_does_not_write_an_unnecessary_hold_goal():
    arm=native({'joint':1,'gripper':0});arm.dispatch_ready=True
    assert arm.set_torque(False)=={'torque':'OFF','writes':0}
    assert not arm.dispatch_ready and ('disable',) in arm.bus.events
    assert not any(entry[0] in ('write','enable') for entry in arm.bus.events)


@pytest.mark.parametrize('enabled',[False,True])
def test_native_same_state_requests_are_read_only(enabled):
    arm=native(dict.fromkeys(('joint','gripper'),int(enabled)))
    assert arm.set_torque(enabled)['torque']==('ON' if enabled else 'OFF')
    assert arm.bus.events==[('read','Torque_Enable',False)] and arm.writes==0


def test_failed_enable_confirmation_is_an_actual_error():
    arm=native({'joint':0,'gripper':0});arm.bus.enable_effect=False
    with pytest.raises(TorqueConfirmationError,match='not confirmed') as error:
        arm.set_torque(True)
    assert error.value.measured_torque=='OFF'
    assert not arm.dispatch_ready and arm.bus.goals==arm.bus.present


def test_missing_motor_readback_cannot_be_treated_as_all_enabled():
    arm=native({'joint':1})
    with pytest.raises(RuntimeError,match='Incomplete'):
        arm.set_torque(True)
    assert not any(entry[0]!='read' for entry in arm.bus.events)


def test_final_health_readback_cannot_claim_on_when_a_motor_disappears():
    arm=native({'joint':1,'gripper':1})
    assert arm.torque_state()=='ON'
    arm.bus.torque.pop('gripper')
    with pytest.raises(RuntimeError,match='Incomplete'):
        arm.health()


def test_read_only_adapters_cannot_change_torque():
    arm=native({'joint':0,'gripper':0});arm.config['read_only']=True
    for backend in (arm,ReadOnlySO101({}),MockArm({'read_only':True,'joints':[]})):
        with pytest.raises(PermissionError,match='read-only'):
            backend.set_torque(True)
    assert arm.bus.events==[]


class OwnerMock(MockArm):
    def __init__(self,config):
        super().__init__(config);self.controller=None;self.events=[]
        self.read_block=threading.Event();self.read_release=threading.Event();self.read_release.set()
        self.read_entered=threading.Event();self.fail_torque=False
    def read(self):
        if self.read_block.is_set():
            self.read_entered.set();assert self.read_release.wait(4.)
        return super().read()
    def torque_state(self):
        self.events.append(('read_torque',threading.get_ident()))
        return super().torque_state()
    def set_torque(self,enabled):
        a=self.controller
        self.events.append(('set_torque',threading.get_ident(),enabled,a.permission,a.op))
        assert not a.permission and a.op is None
        if self.fail_torque:raise OSError('SIMULATED_TORQUE_WRITE_FAILURE')
        return super().set_torque(enabled)
    def block_read(self):
        self.read_release.clear();self.read_block.set()
        assert self.read_entered.wait(1.)
    def unblock_read(self):self.read_block.clear();self.read_release.set()


def wait_for(predicate,seconds=1.):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        if predicate():return
        time.sleep(.002)
    raise AssertionError('Mock condition did not become true')


@pytest.fixture
def running(tmp_path,monkeypatch):
    monkeypatch.setattr('soarm_station.control.now',time.monotonic)
    config=load(Path(__file__).resolve().parents[1]/'config/mock.yaml')
    config['lock_dir']=str(tmp_path/'locks')
    arm=ArmController(config['arms'][0],config,'mock-boot',{})
    arm.backend=OwnerMock(arm.config);arm.backend.controller=arm;arm.backend.torque=True
    arm.start();wait_for(lambda:arm.sample_sequence>0)
    try:yield arm
    finally:
        arm.backend.unblock_read();arm.stop.set();arm.thread.join(2.);arm.pool.shutdown(wait=False,cancel_futures=True)
        assert not arm.thread.is_alive()


def begin(arm):
    arm.set_permission(True,'operator',arm.session)
    return arm.begin({'principal':'operator','runtime_boot_id':arm.boot,'controller_session':arm.session,
        'operation_type':'DIRECT','mode':'STREAM','persistent_target':True})


def test_off_revokes_before_owner_io_then_on_seeds_pose_without_permission_or_resume(running):
    arm=running;grant=begin(arm);old_epoch=arm.epoch
    result=arm.set_torque(False,arm.session)
    assert result['success'] and result['measured_torque']=='OFF'
    assert arm.op is None and not arm.permission and not arm.hold_pending and arm.epoch>old_epoch
    assert not arm.windows and arm.target is None and arm.commanded_q is None
    assert arm.completed[grant['operation_id']]['result_code']=='HELD'
    assert arm.backend.writes==0
    arm.backend.q={name:.04 for name in arm.backend.q};moved=dict(arm.backend.q)
    result=arm.set_torque(True,arm.session)
    assert result['success'] and result['measured_torque']=='ON'
    assert arm.backend.q==moved and arm.backend.writes==1
    assert not arm.permission and arm.op is None and arm.phase=='HOLD'
    assert all(row['torque_enabled'] is True for row in arm.health_data['motors'])
    assert all(event[1]==arm.thread.ident for event in arm.backend.events)


def test_already_on_is_idempotent_and_does_not_disrupt_a_healthy_operation(running):
    arm=running;grant=begin(arm);identity=(arm.epoch,arm.principal,arm.op['operation_id'])
    result=arm.set_torque(True,arm.session)
    assert result['success'] and arm.permission
    assert identity==(arm.epoch,arm.principal,arm.op['operation_id'])
    assert arm.op['operation_id']==grant['operation_id'] and arm.backend.writes==0
    assert not any(event[0]=='set_torque' for event in arm.backend.events)


def test_already_off_still_revokes_permission_without_writes(running):
    arm=running;arm.backend.torque=False;begin(arm)
    assert arm.set_torque(False,arm.session)['success']
    assert not arm.permission and arm.op is None and arm.backend.writes==0
    assert not any(event[0]=='set_torque' for event in arm.backend.events)


def test_exact_session_bool_read_only_and_resource_validation_do_no_io(running):
    arm=running;runtime=Runtime.__new__(Runtime);runtime.arms={arm.id:arm}
    for body,reason in [
        ({'arm_id':'missing','enabled':False,'expected_controller_session':arm.session},'Unknown arm'),
        ({'arm_id':arm.id,'enabled':False,'expected_controller_session':'stale'},'session mismatch'),
        ({'arm_id':arm.id,'enabled':1,'expected_controller_session':arm.session},'boolean'),
    ]:
        with pytest.raises(ValueError,match=reason):runtime.call({'op':'set_torque',**body})
    arm.config['read_only']=True
    with pytest.raises(ValueError,match='read-only'):arm.set_torque(False,arm.session)
    assert arm.backend.events==[] and arm.backend.writes==0


def test_pending_request_blocks_admission_and_newer_hold_cancels_enable(running):
    arm=running;arm.backend.torque=False;arm.backend.block_read()
    with ThreadPoolExecutor(max_workers=1) as callers:
        future=callers.submit(arm.set_torque,True,arm.session)
        wait_for(lambda:arm.torque_request is not None)
        with pytest.raises(ValueError,match='pending'):arm.set_permission(True,'operator',arm.session)
        with pytest.raises(ValueError,match='pending'):arm.set_torque(False,arm.session)
        with arm.lock:arm._hold('NEWER_OPERATOR_STOP')
        arm.backend.unblock_read();result=future.result(1.)
    assert not result['success'] and 'superseded' in result['message']
    assert not arm.backend.torque and arm.backend.writes==0 and arm.backend.events==[]


def test_unstarted_request_after_ack_timeout_never_executes_later(running):
    arm=running;arm.backend.torque=False;arm.backend.block_read()
    result=arm.set_torque(True,arm.session)
    assert not result['success'] and result['measured_torque']=='UNKNOWN'
    assert arm.torque_request['canceled'].is_set()
    arm.backend.unblock_read();wait_for(lambda:arm.torque_request is None)
    assert not arm.backend.torque and arm.backend.events==[] and arm.backend.writes==0


def test_unconfirmed_off_still_revokes_immediately_and_cannot_resume_prior_operation(running):
    arm=running;begin(arm);arm.backend.block_read()
    with ThreadPoolExecutor(max_workers=1) as callers:
        future=callers.submit(arm.set_torque,False,arm.session)
        wait_for(lambda:arm.torque_request is not None)
        assert not arm.permission and arm.op is None
        result=future.result(2.)
    assert not result['success'] and result['measured_torque']=='UNKNOWN'
    arm.backend.unblock_read();wait_for(lambda:arm.torque_request is None and not arm.hold_pending)
    assert not arm.permission and arm.op is None and arm.phase=='HOLD'
    assert arm.backend.torque and arm.backend.writes==0


def test_actual_torque_io_failure_faults_and_revokes_without_claiming_success(running):
    arm=running;begin(arm);arm.backend.fail_torque=True
    result=arm.set_torque(False,arm.session)
    assert not result['success'] and result['measured_torque']=='UNKNOWN'
    assert 'SIMULATED_TORQUE_WRITE_FAILURE' in result['message']
    assert arm.phase=='FAULT' and arm.op is None and not arm.permission
    assert arm.health_data['torque']=='UNKNOWN'
    wait_for(lambda:not arm.thread.is_alive())


def test_partial_confirmation_reports_real_mixed_state_and_fault(running):
    arm=running;begin(arm)
    def partial(_):raise TorqueConfirmationError('OFF','MIXED')
    arm.backend.set_torque=partial
    result=arm.set_torque(False,arm.session)
    assert not result['success'] and result['measured_torque']=='MIXED'
    assert arm.phase=='FAULT' and not arm.permission and arm.op is None
    assert all(row['torque_enabled'] is None for row in arm.health_data['motors'])


def test_owner_shutdown_releases_pending_request_without_serial_dispatch(running):
    arm=running;arm.backend.torque=False;arm.backend.block_read()
    with ThreadPoolExecutor(max_workers=1) as callers:
        future=callers.submit(arm.set_torque,True,arm.session)
        wait_for(lambda:arm.torque_request is not None)
        arm.stop.set();arm.backend.unblock_read();result=future.result(1.)
    assert not result['success'] and not arm.backend.torque and arm.backend.writes==0
