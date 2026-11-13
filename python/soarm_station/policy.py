"""LeRobot checkpoint/processors and native remote engine; no motor access."""
import hashlib
import json
import math
import threading
from dataclasses import dataclass
import time
from pathlib import Path

def verify_bundle(directory):
    root=Path(directory).resolve();m=json.loads((root/'manifest.json').read_text())
    if m['schema_version']!=1 or m['lerobot_version']!='0.7.0':raise ValueError('Incompatible policy manifest')
    for name,digest in m['files'].items():
        p=(root/name).resolve()
        if not p.is_relative_to(root) or hashlib.sha256(p.read_bytes()).hexdigest()!=digest:raise ValueError('Policy artifact digest mismatch')
    return m

def native_state(profile,q,g):
    result=[]
    for f in profile['state_features']:
        value=g if f['joint']=='gripper' else q[f['joint']]
        if f['unit']=='degree':value=math.degrees(value)
        elif f['unit'] not in ('radian','percent'):raise ValueError('Unknown policy state unit')
        result.append(value)
    return result
def target_from_action(profile,action):
    values=action.detach().cpu().reshape(-1).tolist() if hasattr(action,'detach') else list(action)
    if len(values)!=len(profile['action_features']):raise ValueError('Policy action dimension mismatch')
    result={'positions':{},'gripper':None,'gripper_unit':'percent'}
    for f,v in zip(profile['action_features'],values):
        if f['unit']=='degree':v=math.radians(v)
        elif f['unit'] not in ('radian','percent'):raise ValueError('Unknown policy action unit')
        if f['joint']=='gripper':
            if f['unit']!='percent':raise ValueError('Gripper mapping must be explicit percent')
            result['gripper']=v
        else:result['positions'][f['joint']]=v
    if result['gripper'] is None:raise ValueError('Policy must declare gripper output')
    return result
def observations(profile,q,g,cameras):
    import cv2,numpy as np
    obs={'observation.state':np.array(native_state(profile,q,g),dtype=np.float32)}
    for feature,name in profile.get('cameras',{}).items():
        c=cameras[name];data=Path(c['path']).read_bytes();image=cv2.imdecode(np.frombuffer(data,dtype=np.uint8),cv2.IMREAD_COLOR)
        obs[feature]=cv2.cvtColor(image,cv2.COLOR_BGR2RGB)
    return obs

@dataclass(frozen=True)
class ModelOutcome:
    state: str
    target: dict | None = None
    interval_s: float = 0.
    source_time: float = 0.
    max_age_s: float = 5.
    execution_generation: int = 0
    session: object = None
    reason: str = ''
    hold_token: object = None

class NativeHoldRequested(Exception):pass

class NativePolicy:
    control_polls=False
    """The native engine owns queue eligibility; the arm owner acknowledges holds."""
    def _initialize(self,profile):
        from lerobot.utils.action_interpolator import ActionInterpolator
        self.profile=profile;self.interval=1./profile['action_fps']
        self.max_age=profile.get('action_source_max_age_s',5.)
        self.interpolator=ActionInterpolator(multiplier=1)
        self.commit_interpolator=ActionInterpolator(multiplier=1)
        self.intent_token=None;self.reset_count=0;self.hold_ack=None;self.chunk_capture=None
    @property
    def terminally_failed(self):
        stopped=getattr(self.engine,'_stop_event',None)
        return self.engine.failed or (stopped is not None and stopped.is_set())
    @property
    def execution_generation(self):
        runtime=getattr(self.engine,'runtime',None)
        return runtime.generation if runtime is not None else self.reset_count
    def _hold_token(self):
        hold=getattr(self.engine,'_hold',None)
        return (self.reset_count,self.execution_generation,getattr(hold,'started_at',0.),getattr(hold,'reason',None))
    def gate(self,outcome=None):
        if self.engine.failed:return ModelOutcome('FAULT',session=self,reason='Native inference session faulted')
        if not self.engine.dispatch_allowed():
            if self.engine.failed:return ModelOutcome('FAULT',session=self,reason='Native action/source deadline expired')
            token=self._hold_token()
            return ModelOutcome('WAIT' if token==self.hold_ack else 'HOLD',session=self,
                reason=getattr(getattr(self.engine,'_hold',None),'reason',None) or 'Native startup/refill wait',hold_token=token)
        if outcome is not None:
            if outcome.execution_generation!=self.execution_generation:return ModelOutcome('WAIT',session=self,reason='Native execution generation replaced')
            if time.monotonic()-outcome.source_time>outcome.max_age_s:
                return ModelOutcome('FAULT',session=self,reason='Policy action source observation expired')
        self.hold_ack=None
        return ModelOutcome('ACTION',session=self)
    def _request_hold(self):raise NativeHoldRequested()
    def acknowledge_hold(self,token):
        self.engine.acknowledge_hold();self.hold_ack=token
    def reset_task(self,instruction,token):
        # Called on the bounded native worker, never on the motor thread.
        if token!=self.intent_token:
            if self.intent_token is not None:
                self.engine.pause();self.engine.reset();self.interpolator.reset()
            if self.engine.task!=instruction:self.engine.set_task(instruction)
            self.engine.resume()
            self.intent_token=token;self.reset_count+=1;self.hold_ack=None;self.chunk_capture=None
    def pause(self):self.engine.pause()
    def commit(self,outcome,send,hold):
        from lerobot.inference.dispatch import commit_action
        state=self.gate(outcome)
        if state.state!='ACTION':return False,state
        held=[]
        def native_hold():
            token=self._hold_token();hold();self.hold_ack=token;held.append(token)
        sent=commit_action(self.engine,self.commit_interpolator,send,hold=native_hold)
        if sent:return True,ModelOutcome('ACTION',session=self)
        return False,ModelOutcome('FAULT' if self.engine.failed else 'HOLD',session=self,
            reason='Native dispatch denied',hold_token=held[-1] if held else self._hold_token())
    def poll(self,obs,capture_time):
        from lerobot.inference.dispatch import next_interpolated_action
        self._observe(obs,capture_time)
        try:action=next_interpolated_action(self.engine,self.interpolator,self._frame(obs),hold=self._request_hold)
        except NativeHoldRequested:
            state=self.gate()
            return ModelOutcome('WAIT',session=self,reason='Native eligibility changed; retry') if state.state=='ACTION' else state
        if self.engine.failed:return ModelOutcome('FAULT',session=self,reason='Native inference failed')
        if action is None:return ModelOutcome('WAIT',session=self,reason='Native action buffer has no eligible endpoint')
        source=self._action_source(capture_time)
        result=ModelOutcome('ACTION',target_from_action(self.profile,action),self.interval,source,self.max_age,
                            self.execution_generation,self)
        state=self.gate(result)
        return result if state.state=='ACTION' else state
    def _observe(self,obs,capture_time):pass
    def _frame(self,obs):return obs
    def _action_source(self,capture_time):return capture_time
    def close(self):self.engine.stop()

class LocalPolicy(NativePolicy):
    def __init__(self,profile):
        from lerobot.configs.policies import PreTrainedConfig
        from lerobot.policies.factory import get_policy_class,make_pre_post_processors
        from lerobot.inference import SyncInferenceEngine
        self._initialize(profile)
        manifest=verify_bundle(profile['bundle']);checkpoint=Path(profile['bundle'])/'pretrained_model'
        for field in ('state_features','action_features','cameras','action_fps','calibration_revision','arm_id','language_conditioned'):
            if manifest[field]!=profile[field]:raise ValueError('Policy manifest/profile mismatch: '+field)
        cfg=PreTrainedConfig.from_pretrained(checkpoint);cfg.device=profile.get('device','cpu')
        self.policy=get_policy_class(cfg.type).from_pretrained(checkpoint,config=cfg).to(cfg.device).eval()
        pre,post=make_pre_post_processors(cfg,pretrained_path=checkpoint,
            preprocessor_overrides={'device_processor':{'device':cfg.device}})
        self.engine=SyncInferenceEngine(self.policy,pre,post,{},[f['name'] for f in profile['action_features']],
            task='',device=cfg.device,robot_type=profile.get('robot_type','soarm_station_multi'))
        self.engine.reset();self.engine.start()
    def _observe(self,obs,capture_time):
        # Cached native actions retain their original chunk's acquisition time.
        if self.chunk_capture is None or self.policy.count_queued_actions()==0:self.chunk_capture=capture_time
    def _action_source(self,capture_time):return self.chunk_capture

class RemotePolicy(NativePolicy):
    control_polls=True
    def __init__(self,profile,instruction=''):
        from lerobot.inference import RemoteInferenceConfig
        from lerobot.remote_inference import RemoteClient,RemoteInferenceEngine
        self._initialize(profile)
        values=dict(profile['remote']);values.setdefault('max_observation_age_s',self.max_age)
        self.config=RemoteInferenceConfig(**values);self.max_age=self.config.max_observation_age_s
        self.client=RemoteClient.connect(self.config);cap=self.client.capabilities
        state=next(f for f in cap.features if f.name=='observation.state')
        if tuple(state.names)!=tuple(f['name'] for f in profile['state_features']):raise ValueError('Remote policy state mapping mismatch')
        if tuple(cap.action_feature.names)!=tuple(f['name'] for f in profile['action_features']):raise ValueError('Remote action mapping mismatch')
        if abs(cap.action_interval-self.interval)>1e-6:raise ValueError('Remote policy cadence mismatch')
        self.client.admit(features=cap.features,action_feature=cap.action_feature,semantics=self.config.semantics,
            action_interval=cap.action_interval,mode=self.config.mode)
        self.engine=RemoteInferenceEngine(self.client,self.config,task=instruction);self.engine.start();self.engine.resume()
    def _observe(self,obs,capture_time):self.engine.submit_observation(obs,capture_time=capture_time)
    def _frame(self,obs):return None
    def _action_source(self,capture_time):
        current=self.engine.runtime.current
        return current.capture_time if current is not None else capture_time

class FixturePolicy:
    """Hardware-free fixture implements the same outcome contract, without torch."""
    def __init__(self,profile):self.profile=profile;self.intent_token=None;self.resets=0;self.calls=0;self.paused=False
    def reset_task(self,instruction,token):
        if token!=self.intent_token:self.resets+=1;self.intent_token=token
        self.paused=False
    def pause(self):self.paused=True
    def poll(self,obs,capture_time):
        self.calls+=1
        if self.profile.get('fixture_delay_s'):time.sleep(self.profile['fixture_delay_s'])
        states=self.profile.get('fixture_states',['ACTION']);state=states[min(self.calls-1,len(states)-1)]
        return ModelOutcome(state,obs if state=='ACTION' else None,1/self.profile.get('action_fps',30),capture_time,
            self.profile.get('action_source_max_age_s',5),0,self,'fixture '+state,('fixture',self.calls) if state=='HOLD' else None)
    def gate(self,outcome=None):
        if self.paused:return ModelOutcome('WAIT',session=self,reason='fixture paused')
        if outcome and time.monotonic()-outcome.source_time>outcome.max_age_s:return ModelOutcome('FAULT',session=self,reason='Policy action source observation expired')
        return ModelOutcome('ACTION',session=self)
    def acknowledge_hold(self,token):pass
    def commit(self,outcome,send,hold):
        state=self.gate(outcome)
        if state.state!='ACTION':return False,state
        send();return True,state
    def close(self):pass

def load_session(profile):
    if profile.get('backend')=='fixture':return FixturePolicy(profile)
    return RemotePolicy(profile) if profile.get('backend')=='remote' else LocalPolicy(profile)

def poll_session(session,profile,q,g,cameras,capture_time):
    if profile.get('backend')=='fixture':obs={'positions':q,'gripper':g,'gripper_unit':'percent'}
    else:obs=observations(profile,q,g,cameras)
    return session.poll(obs,capture_time)


def prepare_remote_observation(profile,q,g,cameras,capture_time):
    return observations(profile,q,g,cameras),capture_time

def current_remote_observation(profile,prepared,q,g):
    import numpy as np
    obs=dict(prepared);obs['observation.state']=np.asarray(native_state(profile,q,g),dtype=np.float32)
    return obs
