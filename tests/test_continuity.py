import copy
import time
from pathlib import Path
import pytest
from soarm_station.config import load
from soarm_station.control import ArmController

ROOT=Path(__file__).resolve().parents[1]
class Clock:
    value=1000.
    def __call__(self):return self.value
    def advance(self,seconds):self.value+=seconds

@pytest.fixture
def setup(monkeypatch):
    clock=Clock();monkeypatch.setattr('soarm_station.control.now',clock)
    config=load(ROOT/'config/mock.yaml');arm=ArmController(config['arms'][0],config,'boot',{})
    arm.q={j['name']:0. for j in arm.config['joints']};arm.backend.q=dict(arm.q);arm.gripper=50.;arm.sample_time=clock()
    return arm,clock

def begin(arm,kind='DIRECT',mode='STREAM',enable=True,**kwargs):
    if enable:arm.set_permission(True,'owner',arm.session)
    return arm.begin({'principal':'owner','runtime_boot_id':'boot','controller_session':arm.session,
                      'operation_type':kind,'mode':mode,**kwargs})

def frame(arm,body=None,kind='COMMAND',revision=0,identifier=None):
    grant=arm.feedback()
    return {k:grant[k] for k in ('runtime_boot_id','controller_session','operation_id','operation_epoch','window_id')}|{
        'principal':'owner','sequence':arm.last_sequence+1,'frame_kind':kind,'input_id':identifier or str(revision),
        'semantic_revision':revision,'body':body or {}}

def point(arm,value,seconds=1.):return {'positions':{n:value for n in arm.q},'gripper':50.,'time_s':seconds}
def tick(arm,clock,seconds=.01,follow=True):
    clock.advance(seconds)
    if follow:arm.q=dict(arm.backend.q);arm.gripper=arm.backend.gripper
    arm.sample_time=clock()
    if arm.hold_pending:arm._finish_hold()
    arm._tick(clock(),min(.03,seconds))

def test_healthy_stream_runs_beyond_120_seconds(setup):
    arm,clock=setup;begin(arm)
    for i in range(1400):
        arm.input(frame(arm,{'positions':dict(arm.q),'gripper':50.},identifier=str(i)))
        tick(arm,clock,.1)
    assert arm.op is not None and arm.permission and arm.op['deadline'] is None

def test_browser_persistent_target_survives_idle_then_accepts_fresh_intent(setup):
    arm,clock=setup;begin(arm,persistent_target=True)
    assert arm.op['persistent_target'] and not arm.op['requires_liveness']
    desired={'positions':{n:.02 for n in arm.q},'gripper':50.}
    old=frame(arm,desired,identifier='initial-target');arm.input(old)
    for _ in range(50):tick(arm,clock,.1)
    assert arm.op and arm.permission and arm.op['deadline'] is None
    assert arm.target[0]==desired['positions']
    assert arm.commanded_q==pytest.approx(desired['positions'])
    with pytest.raises(ValueError,match='Expired or unknown input window'):
        arm.input({**old,'sequence':arm.last_sequence+1,'input_id':'queued-old-window'})
    replacement={'positions':{n:.03 for n in arm.q},'gripper':50.}
    arm.input(frame(arm,replacement,identifier='fresh-target'))
    tick(arm,clock)
    assert arm.op and arm.last_input=='fresh-target' and arm.permission

def test_persistent_target_still_honors_explicit_stop_and_never_rearms(setup):
    arm,clock=setup;begin(arm,persistent_target=True)
    arm.input(frame(arm,{'positions':dict(arm.q),'gripper':50.},identifier='pickup'))
    retired=frame(arm,{'positions':dict(arm.q),'gripper':50.},identifier='late')
    arm._hold('OPERATOR_HOLD');arm._finish_hold()
    with pytest.raises(ValueError,match='No active operation'):arm.input(retired)
    with pytest.raises(ValueError,match='Permission required'):begin(arm,enable=False,persistent_target=True)
    assert not arm.permission and arm.op is None

@pytest.mark.parametrize('reason',['feedback','duration'])
def test_persistent_target_preserves_feedback_faults_and_explicit_duration(setup,reason):
    arm,clock=setup;begin(arm,persistent_target=True,**({'duration_s':.1} if reason=='duration' else {}))
    arm.input(frame(arm,{'positions':dict(arm.q),'gripper':50.},identifier='pickup'))
    clock.advance(.2)
    if reason=='duration':arm.sample_time=clock()
    arm._tick(clock(),.01)
    assert arm.op is None and not arm.permission
    assert arm.error==('FEEDBACK_EXPIRED' if reason=='feedback' else 'OPERATION_OR_LIVENESS_EXPIRED')

@pytest.mark.parametrize('kwargs',[
    {'persistent_target':'true'}, {'persistent_target':True,'operation_type':'MODEL'},
    {'persistent_target':True,'operation_type':'TRAJECTORY'}, {'persistent_target':True,'mode':'INLINE'},
])
def test_persistent_target_is_only_a_typed_direct_stream_contract(setup,kwargs):
    arm,_=setup;arm.set_permission(True,'owner',arm.session)
    request={'principal':'owner','runtime_boot_id':'boot','controller_session':arm.session,
        'operation_type':'DIRECT','mode':'STREAM',**kwargs}
    with pytest.raises(ValueError,match='persistent_target|Persistent targets'):arm.begin(request)
    assert arm.op is None

def test_explicit_duration_and_optional_profile_ceiling_are_honored(setup):
    arm,clock=setup;begin(arm,duration_s=2.)
    for _ in range(22):
        if arm.op is not None:arm.input(frame(arm,kind='HEARTBEAT'))
        tick(arm,clock,.1)
    assert arm.op is None and not arm.permission

def test_profile_ceiling_without_request_is_honored(setup):
    arm,clock=setup;arm.limits['max_stream_duration_s']=2.
    begin(arm);assert arm.op['deadline']==pytest.approx(clock()+2.)

def test_inline_path_longer_than_120_seconds_is_valid_within_profile(setup):
    arm,clock=setup;begin(arm,'TRAJECTORY','INLINE',points=[point(arm,.1,180.)])
    assert arm.op['deadline']>clock()+180.

def test_one_grant_two_completed_inline_actions_preserve_reference(setup):
    arm,clock=setup
    begin(arm,'TRAJECTORY','INLINE',points=[point(arm,.1,1.)])
    arm.q={n:.08 for n in arm.q};arm.commanded_q={n:.1 for n in arm.q};arm.backend.q=dict(arm.q);arm.backend.torque=True
    arm.op['start']=clock()-1.1;arm._tick(clock(),.01)
    assert arm.op is None and arm.permission
    arm._finish_hold();reference=dict(arm.commanded_q)
    begin(arm,'TRAJECTORY','INLINE',enable=False,points=[point(arm,.2,1.)])
    assert arm.path[0][1]==reference
    tick(arm,clock,.01,follow=False)
    assert all(arm.commanded_q[n]>=reference[n] for n in reference)
    assert arm.principal=='owner'

def test_stream_endpoint_does_not_finish_or_use_ancient_settle_timer(setup):
    arm,clock=setup;begin(arm,'TRAJECTORY')
    arm.input(frame(arm,{'points':[point(arm,.02,.2)]},revision=1))
    for _ in range(40):
        arm.input(frame(arm,kind='HEARTBEAT'));tick(arm,clock,.1)
    assert arm.op and arm.op['endpoint_reached'] and arm.permission
    arm.q={n:-.011 for n in arm.q};arm.sample_time=clock()
    arm.input(frame(arm,kind='HEARTBEAT'));tick(arm,clock,.01,follow=False)
    assert arm.op and arm.permission
    arm.input(frame(arm,{'points':[point(arm,.03,.2)]},revision=2))
    assert not arm.op['endpoint_reached']

def test_stream_replacement_under_lag_advances_from_commanded_reference(setup):
    arm,clock=setup;begin(arm,'TRAJECTORY')
    arm.commanded_q={n:.1 for n in arm.q}
    arm.input(frame(arm,{'points':[point(arm,.4,1.)]},revision=1))
    tick(arm,clock,.01,follow=False)
    assert arm.commanded_q['elbow_flex']==pytest.approx(.103)

def test_more_than_256_sparse_intents_and_retired_retries_are_bounded(setup):
    arm,clock=setup;begin(arm,'TRAJECTORY');body={'points':[point(arm,.02,1.)]}
    for revision in range(1,601):
        clock.advance(.001);arm.sample_time=clock()
        arm.input(frame(arm,body,revision=revision,identifier='intent-'+str(revision)))
    assert arm.semantic_revision==600 and len(arm.accepted)==0
    before=(arm.generation,arm.op['start'],copy.deepcopy(arm.path))
    with pytest.raises(ValueError,match='STALE_SEMANTIC_REVISION'):arm.input(frame(arm,body,revision=1,identifier='intent-1'))
    arm.input(frame(arm,body,revision=600,identifier='intent-600'))
    assert before==(arm.generation,arm.op['start'],arm.path)
    with pytest.raises(ValueError):arm.input(frame(arm,{'points':[]},revision=601))
    assert arm.semantic_revision==600 and before==(arm.generation,arm.op['start'],arm.path)

def test_legacy_sparse_frame_rejects_before_mutation_but_direct_is_unchanged(setup):
    arm,clock=setup;begin(arm,'TRAJECTORY');generation=arm.generation
    with pytest.raises(ValueError,match='SEMANTIC_REVISION_REQUIRED'):arm.input(frame(arm,{'points':[point(arm,.02)]}))
    assert arm.generation==generation and arm.path is None and arm.semantic_revision==0
    arm._hold('cancel');arm._finish_hold();begin(arm)
    arm.input(frame(arm,{'positions':dict(arm.q),'gripper':50.},identifier='legacy-direct'))
    assert arm.last_input=='legacy-direct'

def test_rate_bound_is_not_overridden_by_measured_lead_projection(setup):
    arm,clock=setup;begin(arm)
    arm.commanded_q={n:.1219 for n in arm.q}
    for j in arm.config['joints']:j['velocity_rad_s']=.1;j['tracking_timeout_s']=.5
    previous=dict(arm.commanded_q)
    assert arm._limit({n:.4 for n in arm.q},50.,clock(),.01) is None
    assert arm.commanded_q==previous
    clock.advance(.6)
    assert arm._limit({n:.4 for n in arm.q},50.,clock(),.01) is None
    assert not arm.permission and arm.error=='TRACKING_ENVELOPE_LOST'

def test_non_language_model_allows_empty_task_and_reuses_resident_session(setup):
    arm,clock=setup
    arm.system['profiles']['fixture']={'backend':'fixture','arm_id':arm.id,'language_conditioned':False,'action_fps':20}
    begin(arm,'MODEL',profile_id='fixture',instruction='')
    def pump_until(predicate):
        for _ in range(200):
            arm.input(frame(arm,kind='HEARTBEAT'));tick(arm,clock,.01);time.sleep(.001)
            if predicate():return
        raise AssertionError('Native fixture did not progress')
    pump_until(lambda:arm.model and arm.model.actions>0)
    session=arm.model.session;loads=arm.model.loads;resets=arm.model.resets
    arm.input(frame(arm,{'instruction':''},revision=1,identifier='new-task'))
    pump_until(lambda:arm.model.resets>resets)
    assert arm.model.session is session and arm.model.loads==loads==1
    with pytest.raises(ValueError,match='does not accept language'):arm.input(frame(arm,{'instruction':'ignored text'},revision=2))
    assert arm.semantic_revision==1

def test_actual_cancel_revokes_retained_permission(setup):
    arm,clock=setup;begin(arm,'TRAJECTORY','INLINE',points=[point(arm,0.,.1)])
    tick(arm,clock,.2);arm._finish_hold();assert arm.permission
    arm._hold('OPERATOR_HOLD');arm._finish_hold()
    assert not arm.permission


def test_continuous_gripper_contact_keeps_grasp_and_bounded_goal(setup):
    arm,clock=setup;arm.gripper=0.;arm.backend.gripper=0.;begin(arm)
    arm.input(frame(arm,{'positions':dict(arm.q),'gripper':0.},identifier='pickup'))
    for i in range(300):
        arm.input(frame(arm,{'positions':dict(arm.q),'gripper':100.},identifier='grasp-'+str(i)))
        tick(arm,clock,.01,follow=False)
        assert arm.commanded_gripper<=15.00001
    assert arm.op and arm.permission and arm.target[1]==100.
    assert arm.commanded_gripper==pytest.approx(15.)
    assert arm.stats['tracking_state']=='LEAD_LIMITED'

def test_midrange_contact_compliance_does_not_freeze_body_and_release_is_rate_bounded(setup):
    arm,clock=setup;begin(arm)
    arm.input(frame(arm,{'positions':dict(arm.q),'gripper':50.},identifier='pickup'))
    arm.commanded_gripper=35.;arm.gripper=50.1
    target=dict(arm.q);target['shoulder_pan']=.05
    arm.input(frame(arm,{'positions':target,'gripper':0.},identifier='grasp'))
    tick(arm,clock,.01,follow=False)
    assert arm.commanded_q['shoulder_pan']>0. and arm.commanded_gripper==35.
    assert arm.permission and arm.op
    previous=arm.commanded_gripper
    arm.input(frame(arm,{'positions':target,'gripper':80.},identifier='release'))
    tick(arm,clock,.01,follow=False)
    assert previous<arm.commanded_gripper<=previous+1.000001

def test_body_trajectory_preserves_grasp_reference_without_requiring_grip_position(setup):
    arm,clock=setup;begin(arm)
    arm._complete('PATH_COMPLETE');arm._finish_hold()
    arm.commanded_gripper=35.;arm.gripper=50.1
    begin(arm,'TRAJECTORY','INLINE',enable=False,points=[{'positions':dict(arm.q),'time_s':.1}])
    assert arm.path[0][2]==35. and arm.path[-1][2]==35. and not arm.op['requires_gripper_settle']
    tick(arm,clock,.2,follow=False)
    assert arm.op is None and arm.permission

def configured_model(setup):
    from soarm_station.model_runtime import ModelSupervisor
    from soarm_station.policy import FixturePolicy
    import hashlib,json
    arm,clock=setup
    profile={'backend':'fixture','arm_id':arm.id,'language_conditioned':False,'action_fps':20,'timeout_s':.01}
    arm.system['profiles']['fixture']=profile;begin(arm,'MODEL',profile_id='fixture',instruction='')
    supervisor=ModelSupervisor(arm);arm.model=supervisor;supervisor.session=FixturePolicy(profile)
    supervisor.profile_key=hashlib.sha256(json.dumps(profile,sort_keys=True).encode()).hexdigest()
    supervisor.token=(arm.op['operation_id'],arm.generation);supervisor.needs_reset=False;supervisor.due=clock()
    return arm,clock,supervisor

def test_retired_job_error_and_timeout_do_not_revoke_new_intent(setup):
    from concurrent.futures import Future
    arm,clock,s=configured_model(setup)
    old=(arm.op['operation_id'],arm.generation-1)
    future=Future();s.job=future;s.job_kind='predict';s.job_token=old;s.job_key=s.profile_key;s.job_deadline=time.monotonic()-5
    s.tick(clock());assert arm.op and arm.permission
    future.set_exception(RuntimeError('obsolete failed prediction'))
    s.tick(clock());assert arm.op and arm.permission and s.retired_errors==1

def test_current_native_job_failure_still_revokes(setup):
    from concurrent.futures import Future
    arm,clock,s=configured_model(setup)
    future=Future();future.set_exception(RuntimeError('current failed prediction'))
    s.job=future;s.job_kind='predict';s.job_token=s.token;s.job_key=s.profile_key
    s.tick(clock());assert not arm.permission and arm.op is None and arm.model_state=='FAULT'

def test_failed_remote_session_recreated_only_for_a_fresh_authorized_operation(setup,monkeypatch):
    from soarm_station.policy import FixturePolicy
    import soarm_station.model_runtime as module
    arm,clock,s=configured_model(setup);profile=arm.system['profiles']['fixture'];closed=[];loaded=[]
    class FailedRemote(FixturePolicy):
        terminally_failed=True
        def close(self):closed.append(True)
    s.session=FailedRemote(profile)
    def load(p):loaded.append(True);return FixturePolicy(p)
    monkeypatch.setattr(module,'load_session',load)
    old_operation=arm.op['operation_id'];s.tick(clock())
    assert not arm.permission and arm.op is None and not loaded and s.failed_operation_id==old_operation
    arm._finish_hold();begin(arm,'MODEL',profile_id='fixture',instruction='')
    for _ in range(100):
        arm.sample_time=clock();s.tick(clock());time.sleep(.001)
        if s.session is not None and not getattr(s.session,'terminally_failed',False):break
    assert loaded==[True] and closed==[True] and arm.permission and arm.op
    s.session.terminally_failed=True;s.tick(clock())
    assert not arm.permission and arm.op is None and loaded==[True]

def test_stale_profile_load_is_cleaned_instead_of_adopted(setup):
    from concurrent.futures import Future
    arm,clock,s=configured_model(setup);closed=[]
    stale=type('Resource',(),{'close':lambda self:closed.append(True)})()
    future=Future();future.set_result(stale)
    s.job=future;s.job_kind='load';s.job_token=('old-op',1);s.job_key='different-profile'
    s.tick(clock())
    for _ in range(50):
        if closed:break
        time.sleep(.001)
    assert closed==[True] and s.session is not stale and arm.permission

def test_remote_failure_while_idle_can_rebind_on_first_fresh_operation(setup,monkeypatch):
    from soarm_station.policy import FixturePolicy
    import soarm_station.model_runtime as module
    arm,clock,s=configured_model(setup);profile=arm.system['profiles']['fixture'];closed=[]
    class FailedRemote(FixturePolicy):
        terminally_failed=True
        def close(self):closed.append(True)
    s.session=FailedRemote(profile);s.session_operation_id='previous-completed-operation';s.failed_operation_id=None
    monkeypatch.setattr(module,'load_session',lambda profile:FixturePolicy(profile))
    for _ in range(100):
        s.tick(clock());time.sleep(.001)
        if s.session is not None and not getattr(s.session,'terminally_failed',False):break
    assert closed==[True] and arm.op is not None and arm.permission

def test_language_stream_wait_does_not_start_native_startup_deadline(setup):
    arm,clock=setup
    arm.system['profiles']['language']={'backend':'fixture','arm_id':arm.id,'language_conditioned':True,'action_fps':10}
    begin(arm,'MODEL',profile_id='language',instruction='')
    for _ in range(1400):
        arm.input(frame(arm,kind='HEARTBEAT'));tick(arm,clock,.1)
    assert arm.op and arm.permission and arm.model_state=='WAIT' and arm.model.loads==0
