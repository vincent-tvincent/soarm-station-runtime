"""Per-arm state owner. Network/ROS never writes a motor directly."""
import copy
import hashlib
import json
import math
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from .hardware import DeviceLock, make_arm

def now(): return time.clock_gettime(time.CLOCK_BOOTTIME)
def uid(): return uuid.uuid4().hex

class ArmController:
    def __init__(self,config,system,boot,cameras):
        self.config=config; self.system=system; self.boot=boot; self.cameras=cameras
        self.id=config['id']; self.session=uid(); self.epoch=0
        self.lock=threading.RLock(); self.stop=threading.Event()
        self.backend=make_arm(config); self.permission=False; self.principal=''
        self.phase='IDLE'; self.error=''; self.q={}; self.gripper=0.; self.sample_time=0.; self.sample_ns=0
        self.sample_sequence=0; self.health_data={'torque':'UNKNOWN'}; self.health_time_ns=0; self.op=None
        self.windows={}; self.last_sequence=-1; self.last_input=''; self.last_applied=''; self.generation=0
        self.target=None; self.path=None; self.last_write=0.; self.hold_pending=False; self.hold_event=threading.Event()
        self.commanded_q=None;self.commanded_gripper=None
        self.hold_retarget=True
        self.pending_result_id=None;self.result_ready=set()
        self.semantic_revision=0;self.semantic_record=None;self.last_input_error=''
        self.last_frame_sequence=None;self.last_frame_window_remaining_ms=None;self.rejected_frames=0;self.hold_context={}
        self.limits=config.get('operation_limits',{})
        self.policy_preparation=None;self.model=None;self.model_state='IDLE';self.model_reason=''
        self.tracking_wait={};self.tracking_anchor={};self.tracking_outside_since=None
        self.stats={'lead_limited_ticks':0,'tracking_wait_ticks':0}
        self.completed={}; self.accepted={}; self.pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='policy-'+self.id)
        self.job=None; self.plan_job=None; self.policy=None; self.policy_name=''; self.tick_times=deque(maxlen=10000)
        self.thread=threading.Thread(target=self.run,daemon=True,name='arm-'+self.id)
    def start(self): self.thread.start()
    def run(self):
        lock=None
        try:
            lock=DeviceLock(self.system['lock_dir'],self.config.get('port','mock-'+self.id))
            self.backend.connect()
            with self.lock:
                if self.error:self.phase='HOLD';self.error=''
            next_health=0.; deadline=now(); last_tick=deadline
            while not self.stop.is_set():
                tick=now(); q,g=self.backend.read(); stamp=time.time_ns()
                with self.lock:
                    self.q=q; self.gripper=g; self.sample_time=now(); self.sample_ns=stamp; self.sample_sequence+=1
                    if tick-last_tick>.3 and self.op: self._hold('CONTROL_GAP')
                    if tick>=next_health:
                        self.health_data=self.backend.health();self.health_time_ns=time.time_ns();next_health=tick+1
                    if self.hold_pending:
                        self._finish_hold()
                    self._tick(now(),min(.03,max(.001,tick-last_tick)))
                    self.tick_times.append(tick); last_tick=tick
                deadline+=1/self.system['control_hz']
                if deadline<now(): deadline=now()
                self.stop.wait(max(0,deadline-now()))
        except Exception as exc:
            with self.lock:
                self._fault(str(exc))
        finally:
            self.backend.close()
            if lock: lock.close()
            self.hold_event.set()
    def _hold(self,reason,completed=False):
        if self.model is not None:self.model.invalidate()
        if self.op:
            self.hold_context={'reason':reason,'last_good_sequence':self.last_sequence,'last_frame_sequence':self.last_frame_sequence,
                'last_input_error':self.last_input_error,'rejected_frames':self.rejected_frames,
                'last_frame_window_remaining_ms':self.last_frame_window_remaining_ms,
                'source_gap_ms':(now()-self.op['last_live'])*1000,'direct_gap_ms':(now()-self.op['last_direct'])*1000 if self.op['last_direct'] else None}
            self.pending_result_id=self.op['operation_id']
            self.completed[self.pending_result_id]={'result_code':'COMPLETED' if completed else 'HELD',
                'reason':reason,'execution_completed':completed,'task_outcome':'UNKNOWN',
                'operation_id':self.op['operation_id']}
            if len(self.completed)>128:
                old=next(iter(self.completed));self.completed.pop(old);self.result_ready.discard(old)
        self.op=None; self.target=None;self.path=None;self.windows.clear();self.epoch+=1;self.generation+=1
        self.commanded_q=None;self.commanded_gripper=None
        self.permission=False;self.principal='';self.phase='HOLD';self.error=reason
        self.hold_retarget=not (completed and reason=='PATH_COMPLETE')
        self.hold_pending=True;self.hold_event.clear()
        # Running native work may finish; its generation is no longer admissible.
    def _finish_hold(self):
        if self.last_write:
            if self.hold_retarget:
                self.health_data.update(self.backend.hold())
            else:
                # A completed timed path already ends at its approved final goal.
                # Preserve its support torque instead of replacing it with a lagged measurement.
                self.health_data.update(self.backend.health());self.health_time_ns=time.time_ns()
                if self.health_data.get('torque')!='ON':raise RuntimeError('Torque lost before completion confirmation')
        if self.pending_result_id is not None:
            self.result_ready.add(self.pending_result_id);self.pending_result_id=None
        self.hold_pending=False;self.hold_event.set()
    def _fault(self,detail):
        self._hold('HARDWARE_ERROR: '+detail);self.phase='FAULT';self.error=detail
        if self.pending_result_id is not None:
            self.completed[self.pending_result_id].update(result_code='FAULT',execution_completed=False,reason=detail)
            self.result_ready.add(self.pending_result_id);self.pending_result_id=None
    def hold(self,reason='OPERATOR_HOLD'):
        with self.lock: self._hold(reason)
        confirmed=self.hold_event.wait(1.5)
        return {'success':confirmed and self.phase!='FAULT','message':self.error or reason}
    def set_permission(self,enable,principal,session):
        if not enable: return self.hold('PERMISSION_REVOKED')
        with self.lock:
            if session != self.session: raise ValueError('Controller session mismatch')
            if not principal: raise ValueError('Principal required')
            if self.hold_pending:raise ValueError('Hold confirmation is pending')
            if self.config.get('read_only',False):raise ValueError('This deployment profile is read-only')
            if self.phase=='FAULT' or not self.q or now()-self.sample_time>.15:
                raise ValueError('Fresh measured feedback unavailable')
            if self.op:
                if principal!=self.principal: raise ValueError('BUSY')
                return {'accepted':True,'controller_session':self.session,'permission':True}
            self.permission=True;self.principal=principal;self.phase='IDLE';self.error=''
            return {'accepted':True,'controller_session':self.session,'permission':True}
    def hold_owned(self,session,principal,operation_id=None,reason='SOURCE_STOP'):
        """Late cleanup cannot stop a different owner or a replacement operation."""
        with self.lock:
            if session!=self.session or not principal or principal!=self.principal:
                return {'success':False,'message':'Owner changed; no hold issued'}
            current=self.op['operation_id'] if self.op else None
            if current!=operation_id:
                return {'success':False,'message':'Operation changed; no hold issued'}
            if not self.permission and not self.op and not self.hold_pending:
                return {'success':True,'message':'Already stopped'}
            self._hold(reason)
        confirmed=self.hold_event.wait(1.5)
        return {'success':confirmed and self.phase!='FAULT','message':self.error or reason}
    def _target(self,body):
        q=body.get('positions',{})
        names={j['name'] for j in self.config['joints']}
        if set(q)!=names: raise ValueError('Full named joint set required')
        for j in self.config['joints']:
            v=q[j['name']]
            if not isinstance(v,(int,float)) or not math.isfinite(v) or not j['min_rad']<=v<=j['max_rad']:
                raise ValueError('Joint outside limits: '+j['name'])
        g=body.get('gripper',self.commanded_gripper if self.commanded_gripper is not None else self.gripper)
        if not math.isfinite(g) or not 0<=g<=100: raise ValueError('Gripper requires percent in [0,100]')
        if body.get('gripper_unit','percent')!='percent': raise ValueError('Unknown gripper unit')
        return dict(q),float(g)
    def _reference(self):
        if not self.q or now()-self.sample_time>.15:raise ValueError('Fresh measured feedback unavailable')
        q=dict(self.commanded_q) if self.commanded_q is not None else dict(self.q)
        g=self.commanded_gripper if self.commanded_gripper is not None else self.gripper
        self._target({'positions':q,'gripper':g})
        lead=self.config.get('max_target_lead_rad',.12)
        if any(abs(q[n]-self.q[n])>lead+.002 for n in q):
            raise ValueError('REFERENCE_OUTSIDE_TRACKING_ENVELOPE')
        return q,float(g)
    def _trajectory(self,points,reference=None):
        if not points or len(points)>256:raise ValueError('Trajectory requires 1..256 points')
        seed_q,seed_g=reference if reference is not None else self._reference()
        result=[(0.,dict(seed_q),seed_g)];previous=0.
        maximum=self.limits.get('max_path_duration_s',3600.)
        for point in points:
            t=float(point['time_s']);q,g=self._target(point)
            if not math.isfinite(t) or t<=previous or t>maximum:raise ValueError('Positive increasing trajectory times within profile horizon required')
            for joint in self.config['joints']:
                if abs(q[joint['name']]-result[-1][1][joint['name']])/(t-previous)>joint['velocity_rad_s']+1e-8:
                    raise ValueError('Trajectory exceeds joint velocity')
            if abs(g-result[-1][2])/(t-previous)>self.config.get('gripper',{}).get('velocity_percent_s',100):
                raise ValueError('Trajectory exceeds gripper velocity')
            result.append((t,q,g));previous=t
        return result
    def _complete(self,reason):
        # Normal finite completion releases the operation, not the healthy owner's grant.
        if self.op is None:return
        identifier=self.op['operation_id'];self.pending_result_id=identifier
        self.completed[identifier]={'result_code':'COMPLETED','reason':reason,'execution_completed':True,
                                   'task_outcome':'UNKNOWN','operation_id':identifier}
        if len(self.completed)>128:
            old=next(iter(self.completed));self.completed.pop(old);self.result_ready.discard(old)
        self.op=None;self.path=None;self.target=None;self.windows.clear();self.epoch+=1;self.generation+=1
        self.phase='IDLE';self.error='';self.hold_retarget=False;self.hold_pending=True;self.hold_event.clear()
        if self.model is not None:self.model.invalidate()
    def _instruction(self,profile,text,allow_wait=False):
        if not isinstance(text,str) or len(text.encode())>512:raise ValueError('Invalid model instruction')
        if profile.get('language_conditioned',False):
            if not text and not allow_wait:raise ValueError('Language-conditioned profile requires an instruction')
        elif text:raise ValueError('Policy does not accept language')
        return text

    def begin(self,request):
        with self.lock:
            if self.op or self.hold_pending:raise ValueError('BUSY')
            if not self.permission or request.get('principal')!=self.principal:raise ValueError('Permission required')
            if request.get('runtime_boot_id')!=self.boot or request.get('controller_session')!=self.session:
                raise ValueError('Runtime/session mismatch')
            kind=request['operation_type'];mode=request['mode']
            persistent=request.get('persistent_target',False)
            if type(persistent)!=bool:raise ValueError('persistent_target must be boolean')
            if persistent and (kind!='DIRECT' or mode!='STREAM'):
                raise ValueError('Persistent targets require DIRECT STREAM')
            if kind=='MODEL' and self.policy_preparation:raise ValueError('Native policy preparation is in progress')
            if kind not in ('DIRECT','TRAJECTORY','MODEL') or mode not in ('INLINE','STREAM'):raise ValueError('Unsupported operation')
            constraints=request.get('constraints',{})
            if constraints.get('required_clearance_m',0)>0 or constraints.get('cartesian_path',False):
                raise ValueError('No configured collision/path validator')
            reference=self._reference()
            requested=request.get('duration_s')
            if requested==0:requested=None
            if requested is not None:
                requested=float(requested)
                if not math.isfinite(requested) or requested<=0:raise ValueError('Explicit duration must be finite and positive')
            path=None;cartesian=None;steps=None;instruction=request.get('instruction','')
            if kind=='MODEL':
                name=request.get('profile_id','')
                if name not in self.system.get('profiles',{}):raise ValueError('Unknown policy profile')
                profile=self.system['profiles'][name]
                if profile.get('arm_id')!=self.id:raise ValueError('Policy resource mismatch')
                if profile.get('backend')!='fixture' and profile.get('calibration_revision')!=self.config.get('calibration_revision'):
                    raise ValueError('Policy calibration revision mismatch')
                if mode=='INLINE':
                    steps=copy.deepcopy(request.get('model_steps') or [{'instruction':instruction,'duration_s':requested}])
                    if not 1<=len(steps)<=16:raise ValueError('Expected 1..16 model steps')
                    for step in steps:
                        step['instruction']=self._instruction(profile,step.get('instruction',''))
                        duration=step.get('duration_s') or self.limits.get('default_model_step_duration_s',30.)
                        if not math.isfinite(duration) or not 0<duration<=self.limits.get('max_model_step_duration_s',3600.):raise ValueError('Model step exceeds profile duration')
                        step['duration_s']=float(duration)
                    instruction=steps[0]['instruction']
                else:self._instruction(profile,instruction,allow_wait=True)
            if kind=='TRAJECTORY' and request.get('payload_kind')=='CARTESIAN_TARGET':
                if mode!='INLINE':raise ValueError('Cartesian streaming is not enabled')
                cartesian=request.get('cartesian',[])
                if not 1<=len(cartesian)<=16 or not self.config.get('urdf'):raise ValueError('Registered model and 1..16 Cartesian targets required')
                if any(not math.isfinite(p['duration_s']) or p['duration_s']<=0 for p in cartesian):raise ValueError('Positive Cartesian durations required')
            if mode=='INLINE' and kind in ('DIRECT','TRAJECTORY') and not cartesian:
                points=request.get('points',[])
                if kind=='DIRECT' and len(points)>16:raise ValueError('Too many inline direct steps')
                path=self._trajectory(points,reference)
            elif mode=='STREAM' and request.get('points'):raise ValueError('STREAM cannot contain inline points')
            if mode=='STREAM':
                ceiling=self.limits.get('max_stream_duration_s')
                if ceiling is not None and requested is not None and requested>ceiling:raise ValueError('Explicit stream duration exceeds profile limit')
                duration=requested if requested is not None else ceiling # None is continuous only without a profile ceiling.
            else:
                natural=sum(p['duration_s'] for p in steps) if steps else sum(p['duration_s'] for p in cartesian) if cartesian else path[-1][0]
                margin=1.+(self.config.get('ik_timeout_s',2.) if cartesian else 0.)+(0. if steps else self.limits.get('settle_timeout_s',2.))
                duration=requested if requested is not None else natural+margin
                if duration<natural or duration>self.limits.get('max_inline_duration_s',3600.):raise ValueError('INLINE duration outside profile or shorter than input sequence')
            self.epoch+=1;self.generation+=1;self.last_sequence=-1;self.last_input='';self.last_applied='';self.last_input_error=''
            self.semantic_revision=0;self.semantic_record=None;self.accepted={}
            self.target=None;self.path=path;self.plan_job=None;self.error='';self.phase='READY'
            self.commanded_q,self.commanded_gripper=reference
            self.tracking_wait={};self.tracking_anchor={};self.tracking_outside_since=None
            t=now()
            self.op={**copy.deepcopy(request),'operation_id':uid(),'operation_epoch':self.epoch,'instruction':instruction,
                     'start':t,'deadline':None if duration is None else t+duration,'last_live':t,'last_direct':0.,
                     'persistent_target':persistent,
                     'requires_liveness':(mode=='STREAM' and not persistent) or request.get('require_liveness',False),
                     'requires_gripper_settle':any('gripper' in point for point in request.get('points',[]))}
            if steps:self.op.update(model_steps=steps,model_step=0,step_started=t)
            if cartesian:
                from .kinematics import plan_cartesian
                self.plan_generation=self.generation;self.plan_start=dict(self.q);self.phase='PREPARING'
                self.plan_job=self.pool.submit(plan_cartesian,self.config,cartesian,dict(reference[0]),reference[1])
            self.model_state='WAIT' if kind=='MODEL' else 'IDLE';self.model_reason=''
            return self.feedback()

    def feedback(self):
        with self.lock:
            if not self.op: return {'phase':self.phase,'stream_ready':False}
            t=now(); self.windows={k:v for k,v in self.windows.items() if v>t}
            if not self.windows or max(self.windows.values())-t<.15:
                self.windows[uid()]=t+.25
            window=max(self.windows,key=self.windows.get)
            return {'phase':self.phase,'stream_ready':True,'arm_id':self.id,'runtime_boot_id':self.boot,
                    'controller_session':self.session,'operation_id':self.op['operation_id'],
                    'operation_epoch':self.epoch,'window_id':window,'window_valid_for_ms':int((self.windows[window]-t)*1000),
                    'last_accepted_input_id':self.last_input,'last_applied_input_id':self.last_applied,
                    'intent_generation':self.generation,'last_sequence':self.last_sequence,
                    'last_accepted_semantic_revision':self.semantic_revision,'reason':self.last_input_error}
    def input(self,frame):
        with self.lock:
            self.last_frame_sequence=frame.get('sequence')
            expiry=self.windows.get(frame.get('window_id'))
            self.last_frame_window_remaining_ms=None if expiry is None else (expiry-now())*1000
            try:return self._accept_input(frame)
            except Exception as exc:self.rejected_frames+=1;self.last_input_error=str(exc);raise
    def _accept_input(self,frame):
        if not self.op:raise ValueError('No active operation')
        for key,value in [('runtime_boot_id',self.boot),('controller_session',self.session),
                          ('operation_id',self.op['operation_id']),('operation_epoch',self.epoch),('principal',self.principal)]:
            if frame.get(key)!=value:raise ValueError('Command identity mismatch: '+key)
        t=now()
        if self.windows.get(frame.get('window_id'),0)<=t:raise ValueError('Expired or unknown input window')
        seq=frame['sequence']
        if type(seq)!=int or seq<=self.last_sequence:raise ValueError('Non-increasing sequence')
        frame_kind=frame['frame_kind']
        if frame_kind not in ('COMMAND','HEARTBEAT','END'):raise ValueError('Unknown frame kind')
        if self.op['mode']=='INLINE' and frame_kind!='HEARTBEAT':raise ValueError('INLINE accepts heartbeat only')
        if frame_kind!='COMMAND' and frame.get('body'):raise ValueError('Non-command frame contains body')
        if frame_kind=='COMMAND':
            body=frame['body'];identifier=frame.get('input_id','');kind=self.op['operation_type']
            if not identifier or len(identifier)>64:raise ValueError('Input ID required')
            fingerprint=hashlib.sha256(json.dumps(body,sort_keys=True,allow_nan=False).encode()).hexdigest()
            duplicate=False;revision=0;path=None;instruction=None
            if kind!='DIRECT':
                revision=frame.get('semantic_revision',0)
                if type(revision)!=int or not 0<revision<2**64:raise ValueError('SEMANTIC_REVISION_REQUIRED: upgrade sparse producer')
                if revision<self.semantic_revision:raise ValueError('STALE_SEMANTIC_REVISION')
                if revision==self.semantic_revision:
                    if self.semantic_record!=(identifier,fingerprint):raise ValueError('SEMANTIC_REVISION_CONFLICT')
                    duplicate=True
            if not duplicate:
                if kind=='DIRECT':
                    q,g=self._target(body)
                    # begin() already seeds the admitted reference from the
                    # current measured/held pose. _limit() handles convergence;
                    # the first target need not match a physical input device.
                elif kind=='TRAJECTORY':path=self._trajectory(body['points'],self._reference())
                else:instruction=self._instruction(self.system['profiles'][self.op['profile_id']],body.get('instruction',''))
                # Commit only after the entire new semantic intent validates.
                if kind=='DIRECT':self.target=(q,g);self.op['last_direct']=t
                elif kind=='TRAJECTORY':
                    self.path=path;self.op['start']=t;self.op['endpoint_reached']=False
                    self.op['requires_gripper_settle']=any('gripper' in point for point in body['points'])
                else:self.op['instruction']=instruction;self.target=None
                if kind!='DIRECT':self.semantic_revision=revision;self.semantic_record=(identifier,fingerprint)
                self.generation+=1;self.last_input=identifier
        self.last_sequence=seq;self.op['last_live']=t;self.last_input_error=''
        if frame_kind=='END':self._hold('STREAM_END',completed=True)
        return {'accepted':True,'input_id':self.last_input,'intent_generation':self.generation,'semantic_revision':self.semantic_revision}

    def _tick(self,t,dt):
        if not self.op: return
        op=self.op
        if not self.permission or (op['deadline'] is not None and t>op['deadline']) or (op['requires_liveness'] and t-op['last_live']>.6):
            self._hold('OPERATION_OR_LIVENESS_EXPIRED');return
        if t-self.sample_time>.15: self._hold('FEEDBACK_EXPIRED');return
        if op['mode']=='STREAM' and op['operation_type']=='DIRECT' and not op.get('persistent_target',False) and op['last_direct'] and t-op['last_direct']>op.get('input_timeout_s',.25):
            self._hold('DIRECT_INPUT_EXPIRED');return
        if self.plan_job is not None:
            if not self.plan_job.done():return
            future=self.plan_job;self.plan_job=None
            if self.plan_generation!=self.generation:return
            try:
                if max(abs(self.plan_start[n]-self.q[n]) for n in self.q)>.03:raise ValueError('Planning start changed')
                self.path=self._trajectory(future.result());op['start']=t;self.phase='READY'
            except Exception as exc:self._hold('INVALID_PLAN: '+str(exc));return
        target=self.target
        if self.path:
            elapsed=t-op['start']; previous=self.path[0]
            if elapsed>=self.path[-1][0]: target=self.path[-1][1:]
            else:
                for point in self.path[1:]:
                    if elapsed<=point[0]:
                        f=(elapsed-previous[0])/(point[0]-previous[0])
                        target=({n:previous[1][n]+f*(point[1][n]-previous[1][n]) for n in self.q},previous[2]+f*(point[2]-previous[2]));break
                    previous=point
        if op['operation_type']=='MODEL':
            if op.get('model_steps') and t-op['step_started']>=op['model_steps'][op['model_step']]['duration_s']:
                op['model_step']+=1
                if op['model_step']>=len(op['model_steps']):self._complete('MODEL_STEPS_COMPLETE');return
                op['step_started']=t;op['instruction']=op['model_steps'][op['model_step']]['instruction'];self.generation+=1;self.target=None
            target=self._model_tick(t)
        if target:
            q,g=target
            limited=self._limit(q,g,t,dt)
            if limited is None:return
            applied,actual_g=limited
            if op['operation_type']=='MODEL':
                if not self.model.commit(lambda:self.backend.send(applied,actual_g)):return
            else:self.backend.send(applied,actual_g)
            self.commanded_q=dict(applied);self.commanded_gripper=actual_g
            self.last_write=t;self.last_applied=self.last_input;self.phase='ACTIVE'
            if self.path and t-op['start']>=self.path[-1][0]:
                goal_dispatched=all(abs(q[n]-applied[n])<.002 for n in q) and abs(g-actual_g)<.2
                if goal_dispatched and all(abs(q[j['name']]-self.q[j['name']])<j.get('goal_tolerance_rad',.03) for j in self.config['joints']) and (not op.get('requires_gripper_settle',True) or abs(g-self.gripper)<self.config.get('gripper',{}).get('goal_tolerance_percent',3.)):
                    if op['mode']=='INLINE':self._complete('PATH_COMPLETE')
                    else:op['endpoint_reached']=True
                elif not op.get('endpoint_reached',False) and t-op['start']>self.path[-1][0]+self.limits.get('settle_timeout_s',2.): self._hold('TRACKING_TIMEOUT')
    def _limit(self,target,gripper,t,dt):
        previous=self.commanded_q if self.commanded_q is not None else self.q
        previous_g=self.commanded_gripper if self.commanded_gripper is not None else self.gripper
        lead=self.config.get('max_target_lead_rad',.12)
        outside=any(abs(previous[n]-self.q[n])>lead+1e-9 for n in previous)
        if outside:
            self.stats['tracking_wait_ticks']+=1
            if self.tracking_outside_since is None:self.tracking_outside_since=t
            outside_names=[n for n in previous if abs(previous[n]-self.q[n])>lead+1e-9]
            self.stats['tracking_state']='REFERENCE_OUTSIDE_ENVELOPE';self.stats['limited_actuators']=outside_names
            timeouts=[self._tracking_timeout(n) for n in outside_names]
            if any(value is not None and t-self.tracking_outside_since>value for value in timeouts):self._hold('TRACKING_ENVELOPE_LOST')
            return None # Never jump an admitted reference backwards to project it into a new band.
        self.tracking_outside_since=None;applied={};blocked={}
        for joint in self.config['joints']:
            name=joint['name'];step=joint['velocity_rad_s']*dt
            candidate=previous[name]+max(-step,min(step,target[name]-previous[name]))
            applied[name]=max(self.q[name]-lead,min(self.q[name]+lead,candidate))
            if abs(applied[name]-candidate)>1e-8:blocked[name]=(self.q[name],.005)
        dg=self.config.get('gripper',{}).get('velocity_percent_s',100)*dt
        candidate=previous_g+max(-dg,min(dg,gripper-previous_g))
        if previous_g<self.gripper-15.:
            actual_g=max(previous_g,min(candidate,self.gripper+15.))
        elif previous_g>self.gripper+15.:
            actual_g=min(previous_g,max(candidate,self.gripper-15.))
        else:actual_g=max(self.gripper-15.,min(self.gripper+15.,candidate))
        if abs(actual_g-candidate)>1e-8 or abs(previous_g-self.gripper)>15.:blocked['_gripper']=(self.gripper,.5)
        self.stats['tracking_state']='LEAD_LIMITED' if blocked else 'FOLLOWING';self.stats['limited_actuators']=list(blocked)
        if blocked:self.stats['lead_limited_ticks']+=1
        for name in list(self.tracking_wait):
            if name not in blocked:self.tracking_wait.pop(name);self.tracking_anchor.pop(name,None)
        for name,(measurement,progress) in blocked.items():
            if name not in self.tracking_wait or abs(measurement-self.tracking_anchor[name])>=progress:
                self.tracking_wait[name]=t;self.tracking_anchor[name]=measurement
            elif self._tracking_timeout(name) is not None and t-self.tracking_wait[name]>self._tracking_timeout(name):
                self._hold('TRACKING_STALLED: '+name);return None
        return applied,actual_g
    def _tracking_timeout(self,name):
        settings=self.config.get('gripper',{}) if name=='_gripper' else next(j for j in self.config['joints'] if j['name']==name)
        value=settings.get('tracking_timeout_s')
        if isinstance(value,dict):value=value.get(self.op['operation_type'] if self.op else '')
        return value
    def _model_tick(self,t):
        if self.model is None:
            from .model_runtime import ModelSupervisor
            self.model=ModelSupervisor(self)
        return self.model.tick(t)

    def snapshot(self):
        with self.lock:
            times=list(self.tick_times)
            return {'arm_id':self.id,'runtime_boot_id':self.boot,'controller_session':self.session,'phase':self.phase,
                'permission':self.permission,'operation_id':self.op['operation_id'] if self.op else '',
                'operation_epoch':self.epoch,'operation_type':self.op['operation_type'] if self.op else 'NONE',
                'positions':dict(self.q),'gripper':self.gripper,'gripper_unit':'percent','time_ns':self.sample_ns,
                'sequence':self.sample_sequence,'age_ms':(now()-self.sample_time)*1000,'error':self.error,
                'hold_context':dict(self.hold_context),'last_input_error':self.last_input_error,'rejected_frames':self.rejected_frames,
                'operation_deadline_remaining_s':None if self.op is None or self.op['deadline'] is None else self.op['deadline']-now(),
                'source_liveness_age_ms':None if self.op is None else (now()-self.op['last_live'])*1000,
                'health':dict(self.health_data),'health_time_ns':self.health_time_ns,
                'reference_positions':dict(self.commanded_q) if self.commanded_q is not None else None,'reference_gripper':self.commanded_gripper,'model_state':self.model_state,'model_reason':self.model_reason,'stats':{**self.stats,'no_progress_s':{name:max(0.,now()-started) for name,started in self.tracking_wait.items()}},'measured_hz':(len(times)-1)/(times[-1]-times[0]) if len(times)>1 else 0}
    def close(self):
        self.hold('SHUTDOWN');self.stop.set();self.thread.join(timeout=3);self.pool.shutdown(wait=False,cancel_futures=True)
