"""Bounded model supervisor. Explicit native outcomes, cadence and source age."""
import hashlib
import json
import time
import copy
from .policy import ModelOutcome,load_session,poll_session,prepare_remote_observation,current_remote_observation

class ModelSupervisor:
    def __init__(self,owner):
        self.owner=owner;self.session=None;self.profile_key=None;self.token=None
        self.job=None;self.job_kind=None;self.job_token=None;self.started=0.;self.needs_reset=True
        self.job_profile={};self.job_key=None;self.job_deadline=0.;self.current_profile={};self.current_key=None
        self.failed_operation_id=None;self.session_operation_id=None;self.retired_errors=0;self.retired_cleanups=0
        self.pending=None;self.active=None;self.due=0.;self.moving=False;self.held_token=None
        self.prepared=None;self.last_prepare=-1e30;self.pending_ready=0.
        self.loads=0;self.resets=0;self.actions=0;self.wait_ticks=0;self.holds=0
    def install_prepared(self,profile,session):
        """Install an idle paused session; caller owns the arm lock and drained old work."""
        if self.owner.op is not None and self.owner.op.get('operation_type')=='MODEL':
            raise ValueError('Cannot replace an active MODEL session')
        self.session=session;self.profile_key=hashlib.sha256(json.dumps(profile,sort_keys=True).encode()).hexdigest()
        self.token=None;self.job=None;self.pending=None;self.active=None;self.prepared=None
        self.needs_reset=True;self.failed_operation_id=None;self.session_operation_id=None
        self.current_profile=profile;self.current_key=self.profile_key;self.moving=False;self.held_token=None
        self.loads+=1

    def invalidate(self):
        self.token=None;self.pending=None;self.active=None;self.prepared=None;self.moving=False;self.held_token=None
        if self.session is not None:self.session.pause()
    def status(self,state,reason=''):
        self.owner.model_state=state;self.owner.model_reason=reason
    def _failure(self,reason):
        if self.owner.op is not None:self.failed_operation_id=self.owner.op['operation_id']
        self.status('FAULT',reason);self.owner._hold('MODEL_FAULT: '+reason)
    def _native_hold(self,outcome,acknowledge=True):
        token=(id(outcome.session),outcome.hold_token)
        if self.held_token!=token:
            self.owner.health_data.update(self.owner.backend.hold())
            self.owner.commanded_q=dict(self.owner.q);self.owner.commanded_gripper=self.owner.gripper
            if acknowledge:outcome.session.acknowledge_hold(outcome.hold_token)
            self.held_token=token;self.holds+=1
        self.pending=None;self.active=None;self.moving=False
        self.status('HOLD',outcome.reason)
    def _outcome(self,outcome,t):
        if outcome.state=='FAULT':self._failure(outcome.reason);return
        if outcome.state=='HOLD':self._native_hold(outcome);return
        if outcome.state=='WAIT':
            if self.active is None:self.status('WAIT',outcome.reason)
            return
        if outcome.state!='ACTION':self._failure('Unknown native outcome');return
        self.owner._target(outcome.target)
        self.pending=outcome;self.pending_ready=t
    def _submit(self,kind,token,fn,*args):
        self.job_kind=kind;self.job_token=token;self.started=time.monotonic()
        self.job_profile=copy.deepcopy(self.current_profile);self.job_key=self.current_key
        timeout=self.job_profile.get('startup_timeout_s',120.) if kind=='load' else self.job_profile.get('timeout_s',2.)
        self.job_deadline=self.started+timeout;self.job=self.owner.pool.submit(fn,*args)
    def tick(self,t):
        from .control import now
        op=self.owner.op;profile=self.owner.system['profiles'][op['profile_id']]
        key=hashlib.sha256(json.dumps(profile,sort_keys=True).encode()).hexdigest()
        self.current_profile=profile;self.current_key=key
        token=(op['operation_id'],self.owner.generation)
        if token!=self.token:
            self.token=token;self.pending=None;self.active=None;self.prepared=None;self.last_prepare=-1e30;self.needs_reset=True;self.due=t;self.moving=False;self.held_token=None
            if self.session is not None:self.session.pause()
            self.status('WAIT','Preparing current model intent')
        if self.job is not None and self.job.done():
            future,kind,issued,issued_key=self.job,self.job_kind,self.job_token,self.job_key;self.job=None
            current=issued is not None and issued==self.token and issued_key==key
            try:result=future.result()
            except Exception as exc:
                if current and kind!='cleanup':self._failure(str(exc));return None
                self.retired_errors+=1;result=None
            if result is not None and kind=='load':
                if issued_key==key:
                    self.session=result;self.profile_key=issued_key;self.loads+=1;self.needs_reset=True;self.failed_operation_id=None;self.session_operation_id=op['operation_id']
                else:
                    self.retired_cleanups+=1;self._submit('cleanup',None,result.close)
            elif current:
                if kind=='reset':self.needs_reset=False;self.resets+=1;self.session_operation_id=op['operation_id']
                elif kind=='prepare':self.prepared=result
                elif kind=='predict' and result is not None:self._outcome(result,t)
            if self.owner.op is None:return None
        if self.job is not None:
            if self.job_token==self.token and self.job_key==key and time.monotonic()>self.job_deadline:
                self._failure('Native '+self.job_kind+' deadline exceeded');return None
            if self.job_token!=self.token:self.status('WAIT','Retired native work is draining')
        if profile.get('language_conditioned',False) and not op.get('instruction'):
            self.status('WAIT','Awaiting language task intent');return None
        unusable=self.session is not None and getattr(self.session,'terminally_failed',False)
        origin=self.failed_operation_id or self.session_operation_id
        if unusable and origin in (None,op['operation_id']):
            self._failure('Native session terminally failed; fresh authorized operation required');return None
        if self.session is None or self.profile_key!=key or unusable:
            if self.job is None:
                old=self.session;self.session=None;self.profile_key=None;self.session_operation_id=None
                def replace():
                    if old is not None:old.close()
                    return load_session(profile)
                self._submit('load',token,replace)
            self.status('WAIT','Loading registered policy');return None
        if self.needs_reset:
            if self.job is None:self._submit('reset',token,self.session.reset_task,op.get('instruction',''),token)
            self.status('WAIT','Resetting native task state');return None
        if getattr(self.session,'control_polls',False):
            if t+1e-9>=self.due and self.prepared is not None:
                images,acquired=self.prepared
                if time.monotonic()-acquired>profile.get('max_image_age_ms',250)/1000:
                    self._failure('Prepared camera observation expired');return None
                obs=current_remote_observation(profile,images,self.owner.q,self.owner.gripper)
                self._outcome(self.session.poll(obs,acquired),t)
                if self.owner.op is None:return None
        if self.pending is not None and t+1e-9>=self.due:
            candidate=self.pending;self.pending=None;gate=self.session.gate(candidate)
            if gate.state=='ACTION':
                in_time=self.pending_ready<=self.due+1e-9 or (getattr(self.session,'control_polls',False) and t-self.due<=1/self.owner.system['control_hz']+1e-9)
                anchor=self.due if self.active is not None and in_time else t
                self.active=candidate;self.due=anchor+candidate.interval_s
                self.held_token=None;self.actions+=1;self.status('ACTION')
            else:self._outcome(gate,t)
            if self.owner.op is None:return None
        # Native remote engine prefetches chunks itself. Local select_action can
        # prepare one next endpoint while the current native interval executes.
        remote=getattr(self.session,'control_polls',False)
        request_due=(t-self.last_prepare>=1/profile.get('action_fps',30)) if remote else self.pending is None
        if self.job is None and request_due:
            snapshots={name:camera.snapshot() for name,camera in self.owner.cameras.items()}
            acquired=[self.owner.sample_time]
            for name in profile.get('cameras',{}).values():
                frame=snapshots.get(name,{})
                if frame.get('age_ms',1e9)>profile.get('max_image_age_ms',250):self._failure('Required camera observation expired');return None
                acquired.append(frame['boottime'])
            source=time.monotonic()-max(0.,now()-min(acquired))
            if remote:
                self.last_prepare=t;self._submit('prepare',token,prepare_remote_observation,profile,dict(self.owner.q),self.owner.gripper,snapshots,source)
            else:self._submit('predict',token,poll_session,self.session,profile,dict(self.owner.q),self.owner.gripper,snapshots,source)
        if self.active is not None and t<=self.due+1e-9:
            gate=self.session.gate(self.active)
            if gate.state=='ACTION':self.status('ACTION');return self.owner._target(self.active.target)
            self._outcome(gate,t);return None
        self.active=None;self.wait_ticks+=1
        if self.owner.model_state not in ('HOLD','FAULT'):self.status('WAIT','Awaiting next eligible native action')
        return None
    def commit(self,send):
        if self.active is None:return False
        def hold():self._native_hold(ModelOutcome('HOLD',session=self.session,hold_token=self.session._hold_token(),reason='Native final dispatch hold'),acknowledge=False)
        sent,state=self.session.commit(self.active,send,hold)
        if sent:self.moving=True;return True
        self._outcome(state,0.)
        return False
