import pytest
try:
    import rclpy
    from soarm_station_system.conversion import goal_from_dict,request_from_goal,stream_from_dict,frame_to_dict
    ROS_AVAILABLE=True
except ImportError:
    ROS_AVAILABLE=False
pytestmark=pytest.mark.skipif(not ROS_AVAILABLE,reason='Run conversion tests with sourced ROS and rebuilt interfaces')

def envelope(kind='DIRECT',mode='STREAM'):
    return {'arm_id':'arm1','runtime_boot_id':'boot','controller_session':'controller','operation_type':kind,'mode':mode}

def test_omitted_stream_duration_is_zero_on_wire_and_unlimited_in_request():
    goal=goal_from_dict(envelope(),'owner')
    assert goal.operation.requested_constraints.max_operation_duration_ms==0
    assert request_from_goal(goal)['duration_s'] is None
    assert request_from_goal(goal)['persistent_target'] is False

def test_gateway_browser_target_semantics_survive_ros_roundtrip():
    goal=goal_from_dict(envelope(),'owner',persistent_target=True)
    assert goal.input.persistent_target is True
    request=request_from_goal(goal)
    assert request['persistent_target'] is True and request['operation_type']=='DIRECT' and request['mode']=='STREAM'

def test_http_json_cannot_select_gateway_managed_target_semantics():
    with pytest.raises(ValueError,match='managed by the control WebSocket'):
        goal_from_dict(envelope()|{'persistent_target':True},'owner')

@pytest.mark.parametrize('kind,mode',[('MODEL','STREAM'),('TRAJECTORY','STREAM'),('DIRECT','INLINE')])
def test_persistent_target_rejects_other_operation_contracts(kind,mode):
    with pytest.raises(ValueError,match='Persistent targets require DIRECT STREAM'):
        goal_from_dict(envelope(kind,mode),'owner',persistent_target=True)
    goal=goal_from_dict(envelope(),'owner');goal.operation.operation_type=kind;goal.input.mode=mode;goal.input.persistent_target=True
    with pytest.raises(ValueError,match='Persistent targets require DIRECT STREAM'):request_from_goal(goal)

def test_explicit_long_duration_survives_conversion():
    goal=goal_from_dict(envelope()|{'duration_s':180.},'owner')
    assert request_from_goal(goal)['duration_s']==180.

def test_sparse_revision_roundtrip_and_legacy_upgrade_error():
    data={'arm_id':'arm1','frame_kind':'COMMAND','payload_kind':'MODEL_TASK','semantic_revision':512,
          'body':{'instruction':''},'input_id':'current','sequence':600}
    message=stream_from_dict(data,'owner');decoded=frame_to_dict(message)
    assert decoded['semantic_revision']==512 and decoded['body']['instruction']==''
    data.pop('semantic_revision')
    with pytest.raises(ValueError,match='SEMANTIC_REVISION_REQUIRED'):stream_from_dict(data,'owner')

def test_non_language_inline_model_can_carry_empty_bounded_step():
    goal=goal_from_dict(envelope('MODEL','INLINE')|{'profile_id':'act','model_steps':[{'instruction':'','duration_s':.5}]},'owner')
    request=request_from_goal(goal)
    assert request['model_steps']==[{'instruction':'','duration_s':.5}]


def test_initial_native_integer_samples_publish_as_ros_float64():
    import time
    from types import SimpleNamespace
    from builtin_interfaces.msg import Time
    from rclpy.serialization import serialize_message
    from soarm_station_system.runtime_node import RuntimeNode
    from soarm_station_interfaces.msg import OperationStatus
    messages=[]
    def publish(message):
        if isinstance(message,OperationStatus):
            assert isinstance(message.measured_hz,float)
            assert isinstance(message.gripper_percent,float)
        serialize_message(message)
        messages.append(message)
    publisher=SimpleNamespace(publish=publish)
    state=dict(runtime_boot_id='boot',controller_session='session',operation_id='',operation_epoch=0,
        phase='IDLE',operation_type='NONE',permission=False,health={'torque':'ON'},positions={'joint':0.},
        age_ms=0,sequence=1,measured_hz=0,gripper=0,error='')
    node=SimpleNamespace(config={'arms':[{'id':'arm1','joints':[{'name':'joint','goal_tolerance_rad':.03}]}],'cameras':[]},snapshot={'arms':{'arm1':state}},received_snapshot=time.monotonic(),
        get_clock=lambda:SimpleNamespace(now=lambda:SimpleNamespace(to_msg=lambda:Time())),
        status={'arm1':publisher},inference={'arm1':publisher},health=publisher,rejected={})
    RuntimeNode.publish_status(node)
    status=next(message for message in messages if isinstance(message,OperationStatus))
    assert status.measured_hz==0. and status.gripper_percent==0. and status.feedback_valid


@pytest.mark.parametrize('mode',['POSITION_ONLY','STRICT_POSE'])
def test_cartesian_inline_json_roundtrip_uses_existing_ros_payload(mode):
    data=envelope('TRAJECTORY','INLINE')|{'payload_kind':'CARTESIAN_TARGET','cartesian':[
        {'frame_id':'arm1_base','position':[.1,.2,.3],'orientation_mode':mode,'orientation':[0.,0.,0.,1.],'duration_s':1.25}]}
    goal=goal_from_dict(data,'owner');request=request_from_goal(goal)
    assert goal.input.inline_sequence.payload_kind=='CARTESIAN_TARGET'
    assert request['payload_kind']=='CARTESIAN_TARGET'
    assert request['cartesian']==data['cartesian']
    assert not goal.input.inline_sequence.joint_trajectory.points


@pytest.mark.parametrize('patch',[
    {'mode':'STREAM'}, {'points':[{'positions':{'joint':0.},'time_s':1.}]},
    {'operation_type':'DIRECT'},
    {'cartesian':[{'frame_id':'base','position':[float('nan'),0.,0.],'duration_s':1.}]},
    {'cartesian':[{'frame_id':'base','position':[0.,0.],'duration_s':1.}]},
    {'cartesian':[{'frame_id':'base','position':[0.,0.,0.],'orientation_mode':'TOOL_AXIS','duration_s':1.}]},
    {'cartesian':[{'frame_id':'base','position':[0.,0.,0.],'orientation_mode':'STRICT_POSE','duration_s':1.}]},
    {'cartesian':[{'frame_id':'base','position':[0.,0.,0.],'duration_s':0.}]},
])
def test_cartesian_json_rejects_mixed_stream_and_malformed_inputs(patch):
    data=envelope('TRAJECTORY','INLINE')|{'payload_kind':'CARTESIAN_TARGET','cartesian':[
        {'frame_id':'base','position':[.1,.2,.3],'duration_s':1.}]}
    data.update(patch)
    with pytest.raises(ValueError):goal_from_dict(data,'owner')


def test_manual_jog_roundtrip_preserves_distinct_rate_and_hold_semantics():
    intent={'translation':[0.,-.5,.2], 'roll':.25, 'elevation':0., 'position_only':False,
            'level':3,'gripper_rate':0.,'gripper_target':None}
    wire={'arm_id':'arm1','frame_kind':'COMMAND','payload_kind':'MANUAL_JOG',
          'window_id':'window','sequence':3,'input_id':'input','body':intent}
    for target in [None,0.,62.]:
        intent['gripper_target']=target
        decoded=frame_to_dict(stream_from_dict(wire,'operator'))
        assert decoded['body']['jog']==intent and decoded['window_id']=='window'
    for patch in [{'translation':[0.,float('nan'),0.]},{'level':True},{'gripper_rate':1.,'gripper_target':20.}]:
        with pytest.raises(ValueError):stream_from_dict({**wire,'body':{**intent,**patch}},'operator')
