import copy
from soarm_station.policy_bindings import compile_profile


def contract():
    features=[{'name':'arm1/joint.pos','joint':'joint','unit':'degree'},{'name':'arm1/gripper.pos','joint':'gripper','unit':'percent'}]
    return {'state_features':features,'action_features':copy.deepcopy(features),'action_fps':10,
        'calibration_revision':'verified-cal','language_conditioned':True}


def resources():
    return {'arms':[{'id':'arm1','joints':[{'name':'joint'}],'gripper':{'name':'gripper'},'calibration_revision':'verified-cal'}],
        'cameras':[{'id':'front'},{'id':'wrist'}]}


def test_remote_text_generation_flag_is_not_action_language_conditioning():
    document={'id':'vla','name':'VLA','execution':{'kind':'remote','endpoint':'tcp/host:7447','deployment':'vla','expected_artifact':'digest'},
        'bindings':{'arm':'arm1','cameras':{'observation.images.front':'front'}},'contract':contract()}
    descriptor={'ready':True,'artifact_identity':'digest','instance_id':'server1','semantics':'declared-contract',
        'capabilities':{'language':False,'action_interval':.1,'modes':['chunk'],'action_representation':'canonical',
            'features':[{'name':'observation.images.front','kind':'rgb','shape':[10,10,3]},
                {'name':'observation.state','names':['arm1/joint.pos','arm1/gripper.pos']}],
            'action_feature':{'names':['arm1/joint.pos','arm1/gripper.pos']}}}
    validation,profile=compile_profile(document,None,resources(),descriptor)
    assert validation['ready'],validation
    assert profile['language_conditioned'] is True
    assert profile['remote']['instance']=='server1'
    document['contract'].pop('language_conditioned')
    validation,_=compile_profile(document,None,resources(),descriptor)
    assert not validation['ready'] and any(e['code']=='LANGUAGE_CONTRACT_REQUIRED' for e in validation['errors'])


def test_two_real_views_and_pinned_mask_do_not_require_a_camera_binding_for_mask():
    trained=contract();inputs=[{'key':'observation.images.front','masked':False},{'key':'observation.images.wrist','masked':False},
        {'key':'observation.images.empty_camera_0','masked':True}]
    model={**trained,'category':'smolvla','complete':True,'compatible':True,'camera_inputs':inputs,'root':'/models/demo',
        'checkpoint_path':'/models/demo','artifact_revision':'sha256:pinned','inventory':[],
        'config':{'type':'smolvla','empty_cameras':1,'input_features':{'observation.state':{'shape':[2]},
            'observation.images.front':{'shape':[3,10,10]},'observation.images.wrist':{'shape':[3,10,10]}},'output_features':{'action':{'shape':[2]}}}}
    class Catalog:
        def resolve(self,*args,**kwargs):return copy.deepcopy(model)
    document={'id':'vla','name':'VLA','execution':{'kind':'local','device':'cpu'},
        'model':{'source_id':'project','relative_path':'demo','artifact_revision':'sha256:pinned'},
        'bindings':{'arm':'arm1','cameras':{'observation.images.front':'front','observation.images.wrist':'wrist'}}}
    validation,profile=compile_profile(document,Catalog(),resources())
    assert validation['ready'],validation
    assert profile['_masked_camera_inputs']==['observation.images.empty_camera_0']
    document['contract']={'camera_inputs':[{'key':'observation.images.front','masked':True}]}
    validation,_=compile_profile(document,Catalog(),resources())
    assert not validation['ready'] and any(e['code']=='TRAINED_CAMERA_MASK_CONFLICT' for e in validation['errors'])
