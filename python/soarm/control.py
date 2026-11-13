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
        self.sample_sequence=0; self.health_data={'torque':'UNKNOWN'}; self.op=None
        self.windows={}; self.last_sequence=-1; self.last_input=''; self.last_applied=''; self.generation=0
        self.target=None; self.path=None; self.last_write=0.; self.hold_pending=False; self.hold_event=threading.Event()
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
                        self.health_data=self.backend.health();next_health=tick+1
                    if self.hold_pending:
                        # No goal write into a disarmed motor. Never torque off a raised arm.
                        if self.last_write and self.health_data.get('torque')=='ON':
                            self.backend.send(self.q,self.gripper)
                        self.hold_pending=False;self.hold_event.set()
                    self._tick(now(),min(.03,max(.001,tick-last_tick)))
                    self.tick_times.append(tick); last_tick=tick
                deadline+=1/self.system['control_hz']
                if deadline<now(): deadline=now()
                self.stop.wait(max(0,deadline-now()))
        except Exception as exc:
            with self.lock:
                self._hold('HARDWARE_ERROR: '+str(exc));self.phase='FAULT';self.error=str(exc)
        finally:
            self.backend.close()
            if lock: lock.close()
            self.hold_event.set()
    def _hold(self,reason,completed=False):
        if self.op:
            self.completed[self.op['operation_id']]={'result_code':'COMPLETED' if completed else 'HELD',
                'reason':reason,'execution_completed':completed,'task_outcome':'UNKNOWN',
                'operation_id':self.op['operation_id']}
            if len(self.completed)>128: self.completed.pop(next(iter(self.completed)))
        self.op=None; self.target=None;self.path=None;self.windows.clear();self.epoch+=1;self.generation+=1
        self.permission=False;self.principal='';self.phase='HOLD';self.error=reason
        self.hold_pending=True;self.hold_event.clear()
        # Running native work may finish; its generation is no longer admissible.
    def hold(self,reason='OPERATOR_HOLD'):
        with self.lock: self._hold(reason)
        confirmed=self.hold_event.wait(1.5)
        return {'success':confirmed and self.phase!='FAULT','message':self.error or reason}
    def set_permission(self,enable,principal,session):
        if not enable: return self.hold('PERMISSION_REVOKED')
        with self.lock:
            if session != self.session: raise ValueError('Controller session mismatch')
            if not principal: raise ValueError('Principal required')
            if self.config.get('read_only',False):raise ValueError('This deployment profile is read-only')
            if self.phase=='FAULT' or not self.q or now()-self.sample_time>.15:
                raise ValueError('Fresh measured feedback unavailable')
            if self.op:
                if principal!=self.principal: raise ValueError('BUSY')
                return {'accepted':True,'controller_session':self.session,'permission':True}
            self.permission=True;self.principal=principal;self.phase='IDLE';self.error=''
            return {'accepted':True,'controller_session':self.session,'permission':True}
    def _target(self,body):
        q=body.get('positions',{})
        names={j['name'] for j in self.config['joints']}
        if set(q)!=names: raise ValueError('Full named joint set required')
        for j in self.config['joints']:
            v=q[j['name']]
            if not isinstance(v,(int,float)) or not math.isfinite(v) or not j['min_rad']<=v<=j['max_rad']:
                raise ValueError('Joint outside limits: '+j['name'])
        g=body.get('gripper',self.gripper)
        if not math.isfinite(g) or not 0<=g<=100: raise ValueError('Gripper requires percent in [0,100]')
        if body.get('gripper_unit','percent')!='percent': raise ValueError('Unknown gripper unit')
        return dict(q),float(g)
    def _trajectory(self,points):
        if not points or len(points)>256: raise ValueError('Trajectory requires 1..256 points')
        result=[(0.,dict(self.q),self.gripper)]; prev=0.
        for p in points:
            t=float(p['time_s']);q,g=self._target(p)
            if not math.isfinite(t) or t<=prev or t>120: raise ValueError('Increasing positive trajectory times required')
            for j in self.config['joints']:
                if abs(q[j['name']]-result[-1][1][j['name']])/(t-prev)>j['velocity_rad_s']+1e-8:
                    raise ValueError('Trajectory exceeds joint velocity')
            if abs(g-result[-1][2])/(t-prev)>self.config.get('gripper',{}).get('velocity_percent_s',100):
                raise ValueError('Trajectory exceeds gripper velocity')
            result.append((t,q,g));prev=t
        return result
    def begin(self,request):
        with self.lock:
            if self.op: raise ValueError('BUSY')
            if not self.permission or request.get('principal')!=self.principal: raise ValueError('Permission required')
            if request.get('runtime_boot_id')!=self.boot or request.get('controller_session')!=self.session:
                raise ValueError('Runtime/session mismatch')
            kind=request['operation_type'];mode=request['mode']
            if kind not in ('DIRECT','TRAJECTORY','MODEL') or mode not in ('INLINE','STREAM'): raise ValueError('Unsupported operation')
            constraints=request.get('constraints',{})
            if constraints.get('required_clearance_m',0)>0 or constraints.get('cartesian_path',False):
                raise ValueError('No configured collision/path validator')
            duration=float(request.get('duration_s',30))
            if not math.isfinite(duration) or not 0<duration<=120: raise ValueError('duration_s must be in (0,120]')
            path=None; target=None;cartesian=None
            if kind=='TRAJECTORY' and request.get('payload_kind')=='CARTESIAN_TARGET':
                if mode!='INLINE':raise ValueError('Cartesian streaming is not enabled')
                cartesian=request.get('cartesian',[])
                if not 1<=len(cartesian)<=16:raise ValueError('Cartesian input requires 1..16 targets')
                if not self.config.get('urdf'):raise ValueError('No registered kinematics model')
            if mode=='INLINE':
                if kind in ('DIRECT','TRAJECTORY'):
                    points=request.get('points',[])
                    if kind=='DIRECT' and len(points)>16: raise ValueError('Too many inline direct steps')
                    path=self._trajectory(points) if not cartesian else None
                    if path and path[-1][0]>duration: raise ValueError('Path exceeds operation duration')
                elif not request.get('instruction') and not request.get('model_steps'):raise ValueError('MODEL instruction required')
            elif request.get('points'): raise ValueError('STREAM cannot contain inline points')
            if kind=='MODEL':
                profile=request.get('profile_id','')
                if profile not in self.system.get('profiles',{}): raise ValueError('Unknown policy profile')
                p=self.system['profiles'][profile]
                if p.get('arm_id')!=self.id: raise ValueError('Policy resource mismatch')
                if p.get('backend')!='fixture' and p.get('calibration_revision')!=self.config.get('calibration_revision'):
                    raise ValueError('Policy calibration revision mismatch')
                if len(request.get('instruction','').encode())>512: raise ValueError('Instruction too long')
                if request.get('instruction') and not p.get('language_conditioned',False): raise ValueError('Policy does not accept language')
            self.epoch+=1;self.generation+=1;self.last_sequence=-1;self.last_input='';self.last_applied=''
            self.accepted={};self.target=target;self.path=path;self.plan_job=None;self.error='';self.phase='READY'
            self.op={**copy.deepcopy(request),'operation_id':uid(),'operation_epoch':self.epoch,
                     'start':now(),'deadline':now()+duration,'last_live':now(),'last_direct':0.,'picked_up':False,
                     'requires_liveness':mode=='STREAM' or request.get('require_liveness',False)}
            if kind=='MODEL' and mode=='INLINE':
                steps=request.get('model_steps') or [{'instruction':request['instruction'],'duration_s':duration}]
                if not 1<=len(steps)<=16 or any(not 0<float(step['duration_s'])<=duration or not step['instruction'] or len(step['instruction'].encode())>512 for step in steps):
                    self._hold('INVALID_MODEL_STEPS');raise ValueError('Invalid model steps')
                if sum(float(step['duration_s']) for step in steps)>duration:
                    self._hold('INVALID_MODEL_STEPS');raise ValueError('Model steps exceed duration cap')
                self.op.update(model_steps=steps,model_step=0,step_started=now(),instruction=steps[0]['instruction'])
            if cartesian:
                from .kinematics import plan_cartesian
                self.plan_generation=self.generation;self.plan_start=dict(self.q);self.phase='PREPARING'
                self.plan_job=self.pool.submit(plan_cartesian,self.config,cartesian,dict(self.q),self.gripper)
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
                    'intent_generation':self.generation,'last_sequence':self.last_sequence}
    def input(self,frame):
        with self.lock:
            if not self.op: raise ValueError('No active operation')
            for k,v in [('runtime_boot_id',self.boot),('controller_session',self.session),
                        ('operation_id',self.op['operation_id']),('operation_epoch',self.epoch),('principal',self.principal)]:
                if frame.get(k)!=v: raise ValueError('Command identity mismatch: '+k)
            t=now()
            if self.windows.get(frame.get('window_id'),0)<=t: raise ValueError('Expired or unknown input window')
            seq=frame['sequence']
            if not isinstance(seq,int) or seq<=self.last_sequence: raise ValueError('Non-increasing sequence')
            frame_kind=frame['frame_kind']
            if frame_kind not in ('COMMAND','HEARTBEAT','END'): raise ValueError('Unknown frame kind')
            if self.op['mode']=='INLINE' and frame_kind!='HEARTBEAT': raise ValueError('INLINE accepts heartbeat only')
            if frame_kind!='COMMAND' and frame.get('body'): raise ValueError('Non-command frame contains body')
            if frame_kind=='COMMAND':
                body=frame['body']; input_id=frame.get('input_id','')
                if not input_id or len(input_id)>64: raise ValueError('Input ID required')
                kind=self.op['operation_type']
                fingerprint=hashlib.sha256(json.dumps(body,sort_keys=True,allow_nan=False).encode()).hexdigest()
                if kind!='DIRECT' and input_id in self.accepted:
                    if self.accepted[input_id]!=fingerprint: raise ValueError('Input ID conflicts')
                    # A duplicate never reactivates a superseded intent.
                elif kind=='DIRECT':
                    q,g=self._target(body)
                    if not self.op['picked_up'] and max(abs(q[n]-self.q[n]) for n in q)>.2:
                        raise ValueError('Leader pickup: align to measured pose')
                    if not self.op['picked_up'] and abs(g-self.gripper)>15:
                        raise ValueError('Gripper pickup: align to measured opening')
                    self.target=(q,g);self.op['picked_up']=True;self.op['last_direct']=t
                    self.generation+=1;self.last_input=input_id
                else:
                    if kind=='TRAJECTORY': self.path=self._trajectory(body['points']);self.op['start']=t
                    else:
                        instruction=body.get('instruction','')
                        if not instruction or len(instruction.encode())>512: raise ValueError('Invalid model instruction')
                        self.op['instruction']=instruction;self.target=None
                    self.generation+=1;self.last_input=input_id;self.accepted[input_id]=fingerprint
                    if len(self.accepted)>256: raise ValueError('Intent history full')
            self.last_sequence=seq;self.op['last_live']=t
            if frame_kind=='END': self._hold('STREAM_END',completed=True)
            return {'accepted':True,'input_id':self.last_input,'intent_generation':self.generation}
    def _tick(self,t,dt):
        if not self.op: return
        op=self.op
        if not self.permission or t>op['deadline'] or (op['requires_liveness'] and t-op['last_live']>.6):
            self._hold('OPERATION_OR_LIVENESS_EXPIRED');return
        if t-self.sample_time>.15: self._hold('FEEDBACK_EXPIRED');return
        if op['mode']=='STREAM' and op['operation_type']=='DIRECT' and op['last_direct'] and t-op['last_direct']>op.get('input_timeout_s',.25):
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
                if op['model_step']>=len(op['model_steps']):self._hold('MODEL_STEPS_COMPLETE',completed=True);return
                op['step_started']=t;op['instruction']=op['model_steps'][op['model_step']]['instruction'];self.generation+=1;self.target=None
            target=self._model_tick(t)
        if target:
            q,g=target
            applied={}
            for j in self.config['joints']:
                n=j['name'];step=min(j['velocity_rad_s']*dt,.12)
                applied[n]=self.q[n]+max(-step,min(step,q[n]-self.q[n]))
            dg=self.config.get('gripper',{}).get('velocity_percent_s',100)*dt
            actual_g=self.gripper+max(-dg,min(dg,g-self.gripper))
            self.backend.send(applied,actual_g);self.last_write=t;self.last_applied=self.last_input;self.phase='ACTIVE'
            if self.path and t-op['start']>=self.path[-1][0]:
                if max(abs(q[n]-self.q[n]) for n in q)<.03 and abs(g-self.gripper)<3:
                    self._hold('PATH_COMPLETE',completed=True)
                elif t-op['start']>self.path[-1][0]+2: self._hold('TRACKING_TIMEOUT')
    def _model_tick(self,t):
        op=self.op;p=self.system['profiles'][op['profile_id']]
        if not op.get('instruction'):return None
        if self.job and self.job.done():
            generation,future=self.job_generation,self.job;self.job=None
            try: result=future.result()
            except Exception as exc: self._hold('INFERENCE_ERROR: '+str(exc));return
            if generation==self.generation and result is not None:
                self.target=self._target(result);self.model_result_time=t
        if self.job is None and t-getattr(self,'model_requested',0)>=1/p.get('action_fps',30):
            from .policy import predict
            snapshots={k:v.snapshot() for k,v in self.cameras.items()}
            for name in p.get('cameras',{}).values():
                if name not in snapshots or snapshots[name].get('age_ms',1e9)>p.get('max_image_age_ms',250):
                    self._hold('POLICY_CAMERA_EXPIRED');return
            self.job_generation=self.generation;self.model_requested=t
            self.model_job_timeout=p.get('startup_timeout_s',120) if self.policy is None else p.get('timeout_s',2)
            self.job=self.pool.submit(predict,self,p,dict(self.q),self.gripper,snapshots,op.get('instruction',''))
        if self.job and t-self.model_requested>getattr(self,'model_job_timeout',2): self._hold('INFERENCE_TIMEOUT');return
        if self.target and t-getattr(self,'model_result_time',0)<=p.get('max_action_age_s',.2): return self.target
        return None
    def snapshot(self):
        with self.lock:
            times=list(self.tick_times)
            return {'arm_id':self.id,'runtime_boot_id':self.boot,'controller_session':self.session,'phase':self.phase,
                'permission':self.permission,'operation_id':self.op['operation_id'] if self.op else '',
                'operation_epoch':self.epoch,'operation_type':self.op['operation_type'] if self.op else 'NONE',
                'positions':dict(self.q),'gripper':self.gripper,'gripper_unit':'percent','time_ns':self.sample_ns,
                'sequence':self.sample_sequence,'age_ms':(now()-self.sample_time)*1000,'error':self.error,
                'health':dict(self.health_data),'measured_hz':(len(times)-1)/(times[-1]-times[0]) if len(times)>1 else 0}
    def close(self):
        self.hold('SHUTDOWN');self.stop.set();self.thread.join(timeout=3);self.pool.shutdown(wait=False,cancel_futures=True)
