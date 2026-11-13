import json
from types import SimpleNamespace
import pytest
from soarm_station.graph_observer import GraphObserver,TrafficSampler


class Clock:
    value=10.
    def __call__(self):return self.value
    def wall(self):return int(self.value*10**9)


class Node:
    def __init__(self):self.subscriptions=[];self.destroyed=[];self.callbacks={}
    def get_node_names_and_namespaces(self):return [('producer','/elsewhere'),('consumer','/elsewhere'),('producer','/elsewhere')]
    def get_topic_names_and_types(self):return [('/arbitrary',['std_msgs/msg/String']),('/_hidden',['std_msgs/msg/String'])]
    def get_publishers_info_by_topic(self,topic):
        qos=SimpleNamespace(reliability=SimpleNamespace(name='RELIABLE'),durability=SimpleNamespace(name='VOLATILE'),history=SimpleNamespace(name='KEEP_LAST'),depth=7)
        return [SimpleNamespace(node_name='producer',node_namespace='/elsewhere',endpoint_gid=[1,2],topic_type='std_msgs/msg/String',qos_profile=qos)]
    def get_subscriptions_info_by_topic(self,topic):return [SimpleNamespace(node_name='consumer',node_namespace='/elsewhere',endpoint_gid=[3,4],topic_type='std_msgs/msg/String',qos_profile=None)]
    def get_service_names_and_types(self):return [('/query',['std_srvs/srv/Trigger'])]
    def get_service_names_and_types_by_node(self,name,namespace):return self.get_service_names_and_types() if name=='producer' else []
    def get_client_names_and_types_by_node(self,name,namespace):return self.get_service_names_and_types() if name=='consumer' else []
    def create_subscription(self,kind,topic,callback,qos,raw=False):
        assert raw
        subscription=object();self.subscriptions.append(subscription);self.callbacks[topic]=callback;return subscription
    def destroy_subscription(self,subscription):self.destroyed.append(subscription)


class Actions:
    def get_action_names_and_types(self,node):return [('/task',['example_interfaces/action/Fibonacci'])]
    def get_action_server_names_and_types_by_node(self,node,name,namespace):return self.get_action_names_and_types(node) if name=='producer' else []
    def get_action_client_names_and_types_by_node(self,node,name,namespace):return self.get_action_names_and_types(node) if name=='consumer' else []


def setup():
    node=Node();clock=Clock();sampler=TrafficSampler(node,resolver=lambda _:object,qos_factory=lambda:None,clock=clock,wall=clock.wall)
    return node,clock,sampler,GraphObserver(node,action_api=Actions(),sampler=sampler,clock=clock,wall=clock.wall)


def test_generic_discovery_retains_every_relation_hidden_topic_and_qos_without_subscribing():
    node,clock,sampler,graph=setup();graph.refresh();data=graph.snapshot()
    assert node.subscriptions==[]
    assert data['coverage']==dict.fromkeys(('nodes','topics','services','actions'),'complete')
    assert {t['name'] for t in data['topics']}=={'/arbitrary','/_hidden'}
    assert next(n for n in data['nodes'] if n['name']=='producer')['instance_count']==2
    assert {e['kind'] for e in data['edges']}=={'publish','subscribe','service_client','service_server','action_client','action_server'}
    assert data['services'][0]['clients']==['node:/elsewhere/consumer']
    assert data['actions'][0]['servers']==['node:/elsewhere/producer']
    assert data['topics'][0]['publishers'][0]['qos']['depth']==7
    assert data['topics'][0]['subscriptions'][0]['qos'] is None
    assert all(t['traffic']['rate_hz'] is None and t['traffic']['state']=='not_sampled' for t in data['topics'])
    assert 'soarm' not in json.dumps(data)
    clock.value+=10;assert graph.snapshot()['state']=='stale'


def test_failed_discovery_is_partial_and_does_not_invent_missing_relationships():
    node,_,_,graph=setup()
    def fail(*args):raise RuntimeError('Discovery changed')
    node.get_client_names_and_types_by_node=fail
    graph.refresh();data=graph.snapshot()
    assert data['coverage']['services']=='partial' and data['state']=='partial'
    assert data['services'][0]['clients']==[]
    assert data['errors'][0]['method']=='get_client_names_and_types_by_node'


def test_explicit_sampling_counts_bytes_only_expires_and_keeps_bounded_history():
    node,clock,sampler,graph=setup();graph.refresh()
    reply=graph.sample({'topic':'/arbitrary','duration_s':5})
    assert reply['accepted'] and len(node.subscriptions)==1
    for index in range(40):
        node.callbacks['/arbitrary'](b'private-token-command-payload')
        clock.value+=.1
    traffic=sampler.snapshot('/arbitrary')
    assert traffic['messages']==40 and traffic['bytes']==40*len(b'private-token-command-payload')
    assert traffic['rate_hz']==pytest.approx(10) and len(traffic['history'])<=20
    assert 'private-token' not in json.dumps(graph.snapshot())
    clock.value+=2;sampler.expire()
    assert len(node.destroyed)==1 and sampler.snapshot('/arbitrary')['state']=='complete'
    assert sampler.snapshot('/never')['messages'] is None


def test_sampler_limits_unknown_types_high_bandwidth_concurrency_and_byte_budget():
    node,clock,sampler,_=setup()
    assert not sampler.request('/camera',['sensor_msgs/msg/Image'])['accepted']
    assert node.subscriptions==[]
    assert sampler.request('/a',['std_msgs/msg/String'])['accepted']
    assert sampler.request('/b',['std_msgs/msg/String'])['accepted']
    assert not sampler.request('/c',['std_msgs/msg/String'])['accepted']
    node.callbacks['/a'](b'x'*65537);sampler.expire()
    assert sampler.snapshot('/a')['state']=='complete'
    assert sampler.snapshot('/a')['messages']==1 and sampler.snapshot('/a')['bytes']==65537
    assert '64 KiB' in sampler.snapshot('/a')['reason']
    sampler.close();assert len(node.destroyed)==2
    assert not sampler.request('/a',['std_msgs/msg/String'])['accepted']


def test_sampler_has_finite_duration_and_never_exports_even_known_command_payload():
    node,clock,sampler,graph=setup();graph.refresh()
    with pytest.raises(ValueError):graph.sample({'topic':'/arbitrary','duration_s':float('nan')})
    with pytest.raises(ValueError):graph.sample({'topic':'/missing'})
    with pytest.raises(ValueError):graph.sample({'topic':'/arbitrary','duration_s':11})
    sampler.request('/arbitrary',['std_msgs/msg/String'],duration=1)
    node.callbacks['/arbitrary'](b'secret_command')
    clock.value+=1;sampler.expire();assert 'secret_command' not in json.dumps(sampler.snapshot('/arbitrary'))
    assert sampler.snapshot('/arbitrary')['rate_hz']==1


def test_failed_subscription_cleanup_retains_handle_continues_others_and_bounds_replacements():
    node,clock,sampler,_=setup()
    sampler.request('/a',['std_msgs/msg/String'],duration=1)
    sampler.request('/b',['std_msgs/msg/String'],duration=1)
    first=node.subscriptions[0];original=node.destroy_subscription
    def destroy(handle):
        if handle is first:raise RuntimeError('Try again later')
        original(handle)
    node.destroy_subscription=destroy;clock.value+=2;sampler.expire()
    assert len(node.destroyed)==1 and sampler.subscriptions=={'/a':first}
    assert sampler.status()['cleanup_pending']==1
    assert not sampler.request('/a',['std_msgs/msg/String'])['accepted']
    assert sampler.request('/c',['std_msgs/msg/String'])['accepted']
    assert not sampler.request('/d',['std_msgs/msg/String'])['accepted']
    node.destroy_subscription=original;sampler.close()
    assert not sampler.subscriptions and sampler.status()['cleanup_pending']==0


def test_observer_loop_survives_refresh_and_cleanup_failures_then_recovers():
    node,clock,sampler,graph=setup();refresh=graph.refresh;expire=sampler.expire;calls={'refresh':0,'cleanup':0}
    class Stop:
        count=0
        def is_set(self):return self.count>=4
        def wait(self,_):self.count+=1;clock.value+=2
    graph.stop=Stop()
    def flaky_refresh():
        calls['refresh']+=1
        if calls['refresh']==1:raise RuntimeError('Temporary graph error')
        refresh()
    def flaky_cleanup():
        calls['cleanup']+=1
        if calls['cleanup']==1:raise RuntimeError('Temporary cleanup error')
        expire()
    graph.refresh=flaky_refresh;sampler.expire=flaky_cleanup;graph._run()
    assert calls['refresh']==4 and calls['cleanup']==4
    assert graph.snapshot()['sequence']==3 and graph.snapshot()['state']=='ready'
    assert graph.snapshot()['errors']==[]


def test_unknown_rmw_endpoint_owners_remain_explicit_without_invalid_node_queries():
    node,_,_,graph=setup();original=node.get_publishers_info_by_topic
    def publishers(topic):
        entries=original(topic);entries[0].node_name='_NODE_NAME_UNKNOWN_';entries[0].node_namespace='_NODE_NAMESPACE_UNKNOWN_';return entries
    node.get_publishers_info_by_topic=publishers
    service=node.get_service_names_and_types_by_node
    def known_only(name,namespace):
        assert namespace.startswith('/')
        return service(name,namespace)
    node.get_service_names_and_types_by_node=known_only
    graph.refresh();data=graph.snapshot()
    assert data['coverage']['topics']=='partial' and data['coverage']['services']=='complete'
    unresolved=next(n for n in data['nodes'] if n.get('discovery_state')=='unresolved_endpoint_owner')
    assert unresolved['fully_qualified_name'] is None and unresolved['namespace'] is None
    assert all(t['publishers'][0]['node_id']==unresolved['id'] for t in data['topics'])
    assert any(error['error']=='UnknownEndpointOwner' for error in data['errors'])
