"""Resolve explicit trained contracts to runtime resources without loading models."""
import copy
import math
from .policy_common import PolicyError,revision


def compile_profile(document,catalog,resource_config,remote_descriptor=None,recheck=False):
    errors=[];warnings=[];model=None;category=None
    def error(code,message,path):errors.append({'code':code,'message':message,'path':path})
    arm_id=document['bindings']['arm'];arms={a['id']:a for a in resource_config.get('arms',[])}
    arm=arms.get(arm_id)
    if arm is None:error('ARM_UNRESOLVED','Configured arm is not available','bindings.arm')
    kind=document['execution']['kind'];contract={};inputs=[]
    if kind=='local':
        try:
            reference=document['model']
            if not reference.get('artifact_revision'):error('ARTIFACT_REVISION_REQUIRED','Select and pin the artifact content revision','model.artifact_revision')
            model=catalog.resolve(reference,recheck=recheck);category=model.get('category')
            contract={key:copy.deepcopy(model[key]) for key in ('state_features','action_features','action_fps','language_conditioned','calibration_revision') if model.get(key) not in (None,[])};inputs=copy.deepcopy(model.get('camera_inputs',[]))
            if not model.get('complete') or not model.get('compatible',True):error('MODEL_INCOMPATIBLE','Artifact is incomplete or its integrity/metadata conflicts','model')
        except PolicyError as exc:error(exc.code,exc.message,'model')
    elif remote_descriptor:
        caps=remote_descriptor.get('capabilities',{})
        category=remote_descriptor.get('category') or caps.get('policy_type')
        for feature in caps.get('features',[]):
            if feature.get('kind')=='rgb' or feature.get('name','').startswith('observation.images.'):
                inputs.append({'key':feature['name'],'shape':feature.get('shape'),'role':None,'masked':False})
        interval=caps.get('action_interval')
        if interval:contract['action_fps']=1./interval
        # Native caps.language advertises text generation, not action conditioning.
        # language_conditioned must come from an explicit trained/profile contract.
        if document['execution'].get('expected_artifact')!=remote_descriptor.get('artifact_identity'):
            error('REMOTE_ARTIFACT_CHANGED','Advertised remote artifact does not match the profile','execution.expected_artifact')
        if not remote_descriptor.get('ready'):error('REMOTE_NOT_READY','The remote server does not advertise readiness','execution')
    else:
        warnings.append({'code':'REMOTE_NOT_INSPECTED','message':'Remote metadata must be inspected before preparation'})
    supplied=document.get('contract',{})
    for field,value in supplied.items():
        known=contract.get(field)
        if known not in (None,[],{}) and known!=value:
            error('TRAINED_CONTRACT_CONFLICT','Explicit profile declaration conflicts with artifact metadata','contract.'+field)
        else:contract[field]=copy.deepcopy(value)
    declarations=contract.get('camera_inputs',[])
    if not inputs and not model:inputs=copy.deepcopy(declarations)
    elif declarations:
        declared={i['key']:i for i in declarations if isinstance(i,dict) and 'key' in i}
        existing={i['key']:i for i in inputs}
        for key,extra in declared.items():
            if key not in existing:
                error('UNKNOWN_CAMERA_INPUT','Profile cannot add an image input not declared by the selected artifact','contract.camera_inputs');continue
            item=existing[key]
            if 'masked' in extra and extra['masked']!=item.get('masked',False):
                error('TRAINED_CAMERA_MASK_CONFLICT','Required/masked input status comes from the pinned artifact','contract.camera_inputs')
            if extra.get('shape') is not None and item.get('shape') is not None and extra['shape']!=item['shape']:
                error('CAMERA_SHAPE_CONFLICT','Profile image shape conflicts with trained input','contract.camera_inputs')
            if item.get('role') is None and extra.get('role') is not None:item['role']=extra['role']
    state=copy.deepcopy(contract.get('state_features') or (model or {}).get('state_features') or [])
    actions=copy.deepcopy(contract.get('action_features') or (model or {}).get('action_features') or [])
    for name,features in (('state_features',state),('action_features',actions)):
        if not features:error('TRAINED_MAPPING_REQUIRED','Named ordered features and units must be explicitly declared','contract.'+name)
        override=document['bindings'].get('state' if name=='state_features' else 'actions',{})
        for feature in features:
            if feature.get('name') in override:feature['joint']=override[feature['name']]
            if feature.get('unit') not in ('degree','radian','percent'):error('UNKNOWN_UNIT','Feature units must be degree/radian/percent','contract.'+name)
        if len({f.get('name') for f in features})!=len(features):error('DUPLICATE_FEATURE','Feature names must be unique','contract.'+name)
    camera_ids={c['id'] for c in resource_config.get('cameras',[])};bindings=document['bindings'].get('cameras',{})
    expected={i['key'] for i in inputs if not i.get('masked',False)}
    masked={i['key'] for i in inputs if i.get('masked',False)}
    if set(bindings)!=expected:
        error('CAMERA_BINDINGS_INCOMPLETE','Bindings must match every real image input; masked inputs stay read-only','bindings.cameras')
    if any(name not in camera_ids for name in bindings.values()):error('CAMERA_UNRESOLVED','A bound camera is not configured','bindings.cameras')
    fps=contract.get('action_fps') or (model or {}).get('action_fps')
    if isinstance(fps,bool) or not isinstance(fps,(int,float)) or not math.isfinite(fps) or fps<=0:error('ACTION_FPS_REQUIRED','Trained action cadence must be declared','contract.action_fps')
    language=contract.get('language_conditioned',(model or {}).get('language_conditioned'))
    if type(language)!=bool:error('LANGUAGE_CONTRACT_REQUIRED','Declare whether the trained policy uses language','contract.language_conditioned')
    calibration=contract.get('calibration_revision') or (model or {}).get('calibration_revision')
    if not calibration:error('CALIBRATION_REFERENCE_REQUIRED','The trained calibration revision must be declared','contract.calibration_revision')
    if arm:
        target={j['name'] for j in arm.get('joints',[])};gripper=arm.get('gripper',{}).get('name','gripper')
        for name,features in (('state_features',state),('action_features',actions)):
            mapped=[f.get('joint') for f in features]
            if set(mapped)!=target|{gripper} or len(set(mapped))!=len(mapped):error('JOINT_BINDINGS_MISMATCH','Policy must explicitly map every body joint and the gripper once','contract.'+name)
            for feature in features:
                if (feature.get('joint')==gripper)!=(feature.get('unit')=='percent'):
                    error('GRIPPER_UNIT_MISMATCH','Only the gripper uses percent; body joints use calibrated angular units','contract.'+name)
        actual_calibration=arm.get('calibration_revision')
        if not actual_calibration:error('RESOURCE_CALIBRATION_UNKNOWN','The arm profile does not declare its calibration revision','bindings.arm')
        elif calibration and calibration!=actual_calibration:error('CALIBRATION_MISMATCH','Policy/arm calibration revisions differ','contract.calibration_revision')
    if kind=='remote' and remote_descriptor:
        caps=remote_descriptor.get('capabilities',{})
        advertised=next((f for f in caps.get('features',[]) if f.get('name')=='observation.state'),{})
        if advertised.get('names') and advertised['names']!=[f['name'] for f in state]:
            error('REMOTE_STATE_ORDER_MISMATCH','Ordered state features differ from the remote descriptor','contract.state_features')
        advertised_action=caps.get('action_feature',{})
        if advertised_action.get('names') and advertised_action['names']!=[f['name'] for f in actions]:
            error('REMOTE_ACTION_ORDER_MISMATCH','Ordered action features differ from the remote descriptor','contract.action_features')
        if caps.get('action_representation') not in (None,'canonical'):
            error('REMOTE_ACTION_REPRESENTATION','Remote post-processor actions are not canonical for this adapter','execution')
    if model:
        config=model.get('config') or {}
        for name,features,field in (('state_features',state,'input_features'),('action_features',actions,'output_features')):
            key='observation.state' if name=='state_features' else 'action'
            shape=(config.get(field,{}).get(key) or {}).get('shape')
            if shape and len(features)!=math.prod(shape):error('FEATURE_DIMENSION_MISMATCH','Named feature count differs from checkpoint shape','contract.'+name)
        if masked:
            if not expected:error('REAL_CAMERA_REQUIRED','At least one real image input is required','bindings.cameras')
            if config.get('type') not in ('smolvla','pi05'):
                error('MASKED_INPUT_UNSUPPORTED','This installed adapter does not declare masked-input support','contract.camera_inputs')
            if config.get('type')=='smolvla' and int(config.get('empty_cameras',0) or 0)<len(masked):
                error('MASKED_INPUT_UNSUPPORTED','SmolVLA native empty-camera budget differs from the pinned masks','contract.camera_inputs')
    validation={'valid':True,'ready':not errors and (kind=='local' or remote_descriptor is not None),
        'errors':errors,'warnings':warnings,'category':category,'resolved_model':{k:model.get(k) for k in ('id','name','category','source_id','relative_path','artifact_revision','camera_inputs')} if model else None}
    if not validation['ready']:return validation,None
    runtime={'arm_id':arm_id,'backend':kind,'device':document['execution'].get('device','cpu'),
        'state_features':state,'action_features':actions,'cameras':dict(bindings),'action_fps':float(fps),
        'language_conditioned':language,'calibration_revision':calibration,'robot_type':'soarm_station_multi',
        **document.get('settings',{})}
    # The native adapter uses its explicit gripper argument; normalize custom logical names.
    for feature in runtime['state_features']+runtime['action_features']:
        if feature['joint']==arm.get('gripper',{}).get('name','gripper'):feature['joint']='gripper'
    if kind=='local':
        runtime['_masked_camera_inputs']=sorted(masked)
        runtime['_artifact']={'root':model['root'],'checkpoint_path':model['checkpoint_path'],
            'artifact_revision':model['artifact_revision'],'files':model['inventory'],'category':category,
            'size_bytes':model.get('size_bytes',sum(f.get('size',0) for f in model['inventory']))}
    else:
        execution=document['execution'];runtime['remote']={key:execution[key] for key in ('endpoint','deployment','expected_artifact','instance') if execution.get(key) is not None}
        if remote_descriptor.get('instance_id'):runtime['remote']['instance']=remote_descriptor['instance_id']
        runtime['remote']['semantics']=remote_descriptor.get('semantics') or remote_descriptor.get('capabilities',{}).get('semantics','')
        modes=remote_descriptor.get('capabilities',{}).get('modes')
        if modes and 'chunk' not in modes:
            validation['errors'].append({'code':'REMOTE_MODE_UNSUPPORTED','message':'Remote deployment does not support the native chunk mode','path':'execution'});validation['ready']=False;return validation,None
    return validation,runtime
