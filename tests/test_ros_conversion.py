import pytest
try:
    import rclpy
    from soarm_system.conversion import goal_from_dict,request_from_goal,stream_from_dict,frame_to_dict
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
    from soarm_system.runtime_node import RuntimeNode
    from soarm_interfaces.msg import OperationStatus
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
    node=SimpleNamespace(snapshot={'arms':{'arm1':state}},received_snapshot=time.monotonic(),
        get_clock=lambda:SimpleNamespace(now=lambda:SimpleNamespace(to_msg=lambda:Time())),
        status={'arm1':publisher},inference={'arm1':publisher},health=publisher,rejected={})
    RuntimeNode.publish_status(node)
    status=next(message for message in messages if isinstance(message,OperationStatus))
    assert status.measured_hz==0. and status.gripper_percent==0. and status.feedback_valid
