import inspect
import types
from concurrent.futures import ThreadPoolExecutor

import pytest
import yaml

from soarm_station.config import load
from soarm_station.subscription_activity import SubscriptionActivity,observe_subscription,publisher_gid


class Clock:
    value=0.
    def __call__(self):return self.value


def info(value=1):return types.SimpleNamespace(publisher_gid=bytes([value])*24)


def test_passive_snapshot_counts_receipts_not_payloads_and_expires_rate():
    clock=Clock();activity=SubscriptionActivity(clock=clock)
    assert activity.register('/camera/image/compressed')
    first=activity.snapshot()['/camera/image/compressed']
    assert first['messages']==0 and first['bytes'] is None
    assert first['rate_hz'] is None and first['last_message_age_ms'] is None
    clock.value=1.;activity.receive('/camera/image/compressed',info())
    clock.value=1.5;activity.receive('/camera/image/compressed',info(2))
    clock.value=2.;observed=activity.snapshot()['/camera/image/compressed']
    assert observed['source']=='dashboard_subscription' and observed['sample_id']==first['sample_id']
    assert observed['messages']==2 and observed['bytes'] is None and observed['rate_hz']==1.
    assert observed['last_message_age_ms']==500.
    assert observed['publisher_counts']==[
        {'publisher_gid':bytes([1]*24).hex(),'messages':1,'last_message_age_ms':1000.},
        {'publisher_gid':bytes([2]*24).hex(),'messages':1,'last_message_age_ms':500.}]
    observed['publisher_counts'][0]['messages']=999
    assert activity.snapshot()['/camera/image/compressed']['publisher_counts'][0]['messages']==1
    clock.value=4.;idle=activity.snapshot()['/camera/image/compressed']
    assert idle['rate_hz']==0. and idle['messages']==2 and idle['last_message_age_ms']==2500.


def test_publisher_and_rate_history_limits_remain_explicit():
    clock=Clock();activity=SubscriptionActivity(clock=clock,max_publishers=2,max_receipts=2)
    activity.register('/feedback')
    for stamp,gid in ((.1,1),(.2,2),(.3,3),(.4,0)):
        clock.value=stamp;activity.receive('/feedback',info(gid))
    clock.value=1.;snapshot=activity.snapshot()['/feedback']
    assert snapshot['messages']==4 and snapshot['unattributed_messages']==2
    assert len(snapshot['publisher_counts'])==2
    assert snapshot['rate_hz'] is None  # Overflow must not turn a truncated history into a false rate.
    assert len(activity._topics['/feedback']['recent'])==2
    clock.value=3.;assert activity.snapshot()['/feedback']['rate_hz']==0.


def test_passive_history_uses_half_second_counts_with_stable_wall_anchor():
    clock=Clock();clock.value=10.;wall=Clock();wall.value=1_700_000_000_000_000_000
    activity=SubscriptionActivity(clock=clock,wall=wall)
    activity.register('/camera/image/compressed')
    initial=activity.snapshot()['/camera/image/compressed']['history']
    assert initial==[{'at_ns':wall.value,'messages':0,'bytes':None}]
    anchor=wall.value
    for stamp in (10.1,10.2,10.6):
        clock.value=stamp;activity.receive('/camera/image/compressed',info())
    clock.value=11.1;wall.value+=99_000_000_000
    history=activity.snapshot()['/camera/image/compressed']['history']
    assert history==[
        {'at_ns':anchor,'messages':2,'bytes':None},
        {'at_ns':anchor+500_000_000,'messages':1,'bytes':None},
        {'at_ns':anchor+1_000_000_000,'messages':0,'bytes':None}]
    history[0]['messages']=999
    assert activity.snapshot()['/camera/image/compressed']['history'][0]['messages']==2


def test_passive_history_is_bounded_and_old_activity_ages_to_observed_zero():
    clock=Clock();anchor=1_700_000_000_000_000_000
    activity=SubscriptionActivity(clock=clock,wall=lambda:anchor)
    activity.register('/state')
    for index in range(40):
        clock.value=index*.5+.1;activity.receive('/state',info())
    result=activity.snapshot()['/state'];history=result['history']
    assert result['messages']==40 and len(history)==20
    assert history[0]['at_ns']==anchor+10_000_000_000
    assert [bucket['messages'] for bucket in history]==[1]*20
    assert all(bucket['bytes'] is None for bucket in history)
    assert len(activity._topics['/state']['buckets'])<=20
    clock.value=40.;idle=activity.snapshot()['/state']
    assert idle['messages']==40 and len(idle['history'])==20
    assert all(bucket['messages']==0 and bucket['bytes'] is None for bucket in idle['history'])
    assert activity._topics['/state']['buckets']=={}


@pytest.mark.parametrize('value',[None,[],[256],[-1],bytes(24),'not-a-gid',list(range(65)),
    {},{'implementation_identifier':'rmw_cyclonedds_cpp'},{'data':None},{'data':{}},
    {'data':{1:2}},{'data':bytes(24)},{'data':1000000000},{'data':list(range(65))}])
def test_invalid_or_absent_gid_is_unattributed(value):
    assert publisher_gid(types.SimpleNamespace(publisher_gid=value)) is None


def test_gid_normalization_matches_endpoint_hex():
    assert publisher_gid(types.SimpleNamespace(publisher_gid=[1,2,255]))=='0102ff'
    assert publisher_gid(types.SimpleNamespace(publisher_gid='0102FF'))=='0102ff'
    assert publisher_gid({'publisher_gid':bytes([1,2,255])})=='0102ff'
    assert publisher_gid({'publisher_gid':{'implementation_identifier':'rmw_cyclonedds_cpp','data':bytes([1,2,255])}})=='0102ff'
    assert publisher_gid({'publisher_gid':{'implementation_identifier':'rmw_fastrtps_cpp','data':[1,2,255]}})=='0102ff'


def test_real_typed_dict_message_info_reaches_passive_publisher_counters():
    clock=Clock();activity=SubscriptionActivity(clock=clock);callbacks=[];handled=[]
    def create(message_type,topic,callback,qos):
        callbacks.append(callback);return types.SimpleNamespace(topic_name=topic)
    observe_subscription(activity,create,object,'/camera/image/compressed',handled.append,object())
    gid=bytes(range(1,25))
    metadata={'source_timestamp':1_700_000_000_000_000_000,
        'received_timestamp':1_700_000_000_050_000_000,
        'publication_sequence_number':8,'reception_sequence_number':7,
        'publisher_gid':{'implementation_identifier':'rmw_cyclonedds_cpp','data':gid}}
    message=object();clock.value=1.;callbacks[0](message,metadata)
    clock.value=1.5;observed=activity.snapshot()['/camera/image/compressed']
    assert handled==[message] and handled[0] is message
    assert observed['messages']==1 and observed['unattributed_messages']==0
    assert observed['publisher_counts']==[
        {'publisher_gid':gid.hex(),'messages':1,'last_message_age_ms':500.}]
    assert observed['last_message_age_ms']==500.  # DDS wall timestamps are not used for local age.


def test_callback_wrapper_uses_resolved_topic_preserves_message_and_handler_errors():
    clock=Clock();activity=SubscriptionActivity(clock=clock);created=[];received=[]
    def create(message_type,topic,callback,qos):
        created.append((message_type,topic,callback,qos))
        return types.SimpleNamespace(topic_name='/remapped/camera')
    message=object();message_type=object();qos=object()
    observe_subscription(activity,create,message_type,'/camera',lambda m:received.append(m),qos)
    assert len(created)==1 and created[0][0] is message_type and created[0][3] is qos
    callback=created[0][2]
    assert len(inspect.signature(callback).parameters)==2
    assert all(p.default is inspect.Parameter.empty for p in inspect.signature(callback).parameters.values())
    callback(message,info())
    assert received==[message] and received[0] is message
    assert set(activity.snapshot())=={'/remapped/camera'}
    assert activity.snapshot()['/remapped/camera']['messages']==1
    error=RuntimeError('original handler failure')
    def fail(message):raise error
    observe_subscription(activity,create,message_type,'/camera',fail,qos)
    with pytest.raises(RuntimeError) as raised:created[1][2](message,info())
    assert raised.value is error
    # Two existing subscriptions to one remapped topic must not double-count graph rates.
    assert activity.snapshot()['/remapped/camera']['messages']==1


def test_first_handler_failure_still_counts_a_real_receipt():
    activity=SubscriptionActivity();callbacks=[]
    def create(message_type,topic,callback,qos):
        callbacks.append(callback);return types.SimpleNamespace(topic_name=topic)
    def fail(message):raise LookupError('handler')
    observe_subscription(activity,create,object,'/event',fail,object())
    with pytest.raises(LookupError):callbacks[0](object(),info())
    assert activity.snapshot()['/event']['messages']==1


def test_concurrent_receipts_have_exact_bounded_counters():
    activity=SubscriptionActivity(max_receipts=16);activity.register('/state')
    def receive_many(_):
        for _ in range(1000):activity.receive('/state',info())
    with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(receive_many,range(4)))
    snapshot=activity.snapshot()['/state']
    assert snapshot['messages']==4000 and snapshot['publisher_counts'][0]['messages']==4000
    assert len(activity._topics['/state']['recent'])<=16


def config_file(tmp_path,watch):
    path=tmp_path/'config.yaml'
    path.write_text(yaml.safe_dump({'schema_version':1,'arms':[],'cameras':[],
        'graph_observer':{'watch':watch}}))
    return path


@pytest.mark.parametrize('watch',[
    [],{'enabled':'true'},{'max_topics':0},{'max_topics':65},{'max_topics':True},
    {'max_clients':9},{'lease_s':2.},{'lease_s':31.},{'lease_s':float('nan')},{'lease_s':True},
    {'max_messages_per_window':50001},{'max_messages_per_window':1.5},
    {'max_bytes_per_window':33554433},
])
def test_graph_watch_config_rejects_invalid_types_and_limits(tmp_path,watch):
    with pytest.raises(ValueError,match='graph watch|Graph watch'):load(config_file(tmp_path,watch))


def test_graph_watch_config_accepts_defaults_and_explicit_boundaries(tmp_path):
    assert load(config_file(tmp_path,{}))['graph_observer']['watch']=={}
    watch={'enabled':False,'max_topics':64,'max_clients':8,'lease_s':30.,
        'max_messages_per_window':50000,'max_bytes_per_window':33554432}
    assert load(config_file(tmp_path,watch))['graph_observer']['watch']==watch
