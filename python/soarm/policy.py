"""LeRobot checkpoint/processors and native remote engine; no motor access."""
import hashlib
import json
import math
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
class LocalPolicy:
    def __init__(self,profile):
        import torch
        from lerobot.configs.policies import PreTrainedConfig
        from lerobot.policies.factory import get_policy_class,make_pre_post_processors
        manifest=verify_bundle(profile['bundle']);checkpoint=Path(profile['bundle'])/'pretrained_model'
        for field in ('state_features','action_features','cameras','action_fps','calibration_revision','arm_id','language_conditioned'):
            if manifest[field]!=profile[field]:raise ValueError('Policy manifest/profile mismatch: '+field)
        cfg=PreTrainedConfig.from_pretrained(checkpoint);cfg.device=profile.get('device','cpu')
        self.policy=get_policy_class(cfg.type).from_pretrained(checkpoint,config=cfg).to(cfg.device).eval()
        self.pre,self.post=make_pre_post_processors(cfg,pretrained_path=checkpoint,
            preprocessor_overrides={'device_processor':{'device':cfg.device}})
        self.policy.reset();self.profile=profile
    def predict(self,obs,instruction):
        import torch
        batch={}
        for name,value in obs.items():
            v=torch.from_numpy(value)
            if name.startswith('observation.images.'):v=v.permute(2,0,1).float()/255
            batch[name]=v
        batch['task']=instruction
        with torch.inference_mode():return self.post(self.policy.select_action(self.pre(batch)))
    def close(self):pass
class RemotePolicy:
    def __init__(self,profile,instruction):
        from lerobot.inference import RemoteInferenceConfig
        from lerobot.remote_inference import RemoteClient,RemoteInferenceEngine
        from lerobot.utils.action_interpolator import ActionInterpolator
        self.config=RemoteInferenceConfig(**profile['remote']);self.client=RemoteClient.connect(self.config)
        cap=self.client.capabilities
        expected_names=tuple(f['name'] for f in profile['state_features'])
        state=next(f for f in cap.features if f.name=='observation.state')
        if tuple(state.names)!=expected_names:raise ValueError('Remote policy state mapping mismatch')
        if tuple(cap.action_feature.names)!=tuple(f['name'] for f in profile['action_features']):raise ValueError('Remote action mapping mismatch')
        if abs(cap.action_interval-1/profile['action_fps'])>1e-6:raise ValueError('Remote policy cadence mismatch')
        self.client.admit(features=cap.features,action_feature=cap.action_feature,semantics=self.config.semantics,
            action_interval=cap.action_interval,mode=self.config.mode)
        self.engine=RemoteInferenceEngine(self.client,self.config,task=instruction);self.interpolator=ActionInterpolator(multiplier=1)
        self.engine.start();self.engine.resume()
    def predict(self,obs,instruction):
        from lerobot.inference import dispatch_tick
        self.engine.submit_observation(obs,capture_time=time.monotonic())
        if self.engine.failed:raise RuntimeError('Native remote inference failed')
        result=[]
        dispatch_tick(self.engine,self.interpolator,send=lambda action:result.append(action),hold=lambda:None)
        return result[0] if result else None
    def close(self):self.engine.stop()
def predict(owner,profile,q,g,cameras,instruction):
    token=(owner.epoch,owner.generation)
    if profile.get('backend')=='fixture':return {'positions':q,'gripper':g,'gripper_unit':'percent'}
    if getattr(owner,'policy_token',None)!=token:
        if owner.policy:owner.policy.close()
        owner.policy=RemotePolicy(profile,instruction) if profile.get('backend')=='remote' else LocalPolicy(profile)
        owner.policy_token=token
    action=owner.policy.predict(observations(profile,q,g,cameras),instruction)
    return target_from_action(profile,action) if action is not None else None
