"""No ROS installation or hardware: exercise leases and real receipt counters."""
import json
import inspect
import sys
import uuid
from types import SimpleNamespace, ModuleType

import pytest

from soarm_station.graph_observer import GraphObserver, TrafficSampler
from soarm_station.graph_watch import GraphWatch, STATUS_TYPE, action_activity
from test_graph_observer import Node, Clock, Actions


def setup(config=None, provider=None, status_decoder=None):
    node, clock = Node(), Clock()
    watcher = GraphWatch(node, config, activity_provider=provider, resolver=lambda _: object,
                         qos_factory=lambda: None, status_decoder=status_decoder, clock=clock, wall=clock.wall)
    return node, clock, watcher


def discover(watcher, *topics):
    watcher.discover([{'name': name, 'types': [kind]} for name, kind in topics])


def acquire(watcher, client_id=None):
    result = watcher.request({'operation': 'acquire', 'client_id': client_id or str(uuid.uuid4())})
    assert result['accepted']
    return result


def data(watcher, name):
    return watcher.snapshot()[1][name]


def test_discovery_and_lease_request_do_not_create_ros_subscriptions_in_http_thread():
    node, clock, watcher = setup()
    discover(watcher, ('/value', 'std_msgs/msg/String'))
    watcher.reconcile()
    assert node.subscriptions == []
    assert data(watcher, '/value')['state'] == 'inactive'
    lease = acquire(watcher)
    assert lease['expires_in_ms'] == 12000
    assert node.subscriptions == []
    watcher.reconcile()
    assert len(node.subscriptions) == 1
    initial = data(watcher, '/value')
    assert initial['state'] == 'watching' and initial['messages'] == 0
    assert initial['last_message_age_ms'] is None


def test_shared_clients_idempotent_acquire_renew_and_scoped_release_keep_subscription_and_counters():
    node, clock, watcher = setup()
    discover(watcher, ('/value', 'std_msgs/msg/String'))
    client = str(uuid.uuid4())
    first = acquire(watcher, client)
    assert acquire(watcher, client)['lease_id'] == first['lease_id']
    second = acquire(watcher)
    watcher.reconcile()
    node.callbacks['/value'](b'hello', SimpleNamespace(publisher_gid=b'\x01\x02'))
    sample_id = data(watcher, '/value')['sample_id']
    clock.value += 11
    assert watcher.request({'operation': 'renew', 'lease_id': first['lease_id']})['expires_in_ms'] == 12000
    watcher.reconcile()
    assert len(node.subscriptions) == 1 and not node.destroyed
    assert data(watcher, '/value')['sample_id'] == sample_id
    assert data(watcher, '/value')['messages'] == 1
    watcher.request({'operation': 'release', 'lease_id': second['lease_id']})
    watcher.reconcile()
    assert len(node.subscriptions) == 1 and not node.destroyed
    assert not watcher.request({'operation': 'release', 'lease_id': 'someone-else'})['accepted']
    node.callbacks['/value'](b'new', None)
    assert data(watcher, '/value')['messages'] == 2


def test_expiry_without_browser_release_stops_callbacks_and_background_cleans_up():
    node, clock, watcher = setup()
    discover(watcher, ('/value', 'std_msgs/msg/String'))
    lease = acquire(watcher)
    watcher.reconcile()
    callback = node.callbacks['/value']
    callback(b'first', None)
    clock.value += 12
    callback(b'must-not-count-after-expiry', None)
    assert watcher.records['/value']['messages'] == 1
    assert data(watcher, '/value')['state'] == 'inactive'
    assert watcher.request({'operation': 'renew', 'lease_id': lease['lease_id']})['code'] == 'not_found'
    watcher.reconcile()
    assert len(node.destroyed) == 1 and watcher.snapshot()[0]['subscriptions'] == 0


def test_last_release_stops_only_watch_subscriptions_not_finite_sample():
    node, clock, watcher = setup()
    sampler = TrafficSampler(node, resolver=lambda _: object, qos_factory=lambda: None, clock=clock, wall=clock.wall)
    sampler.request('/finite', ['std_msgs/msg/String'])
    finite = node.subscriptions[0]
    discover(watcher, ('/value', 'std_msgs/msg/String'))
    lease = acquire(watcher)
    watcher.reconcile()
    watcher.request({'operation': 'release', 'lease_id': lease['lease_id']})
    watcher.reconcile()
    assert len(node.destroyed) == 1 and finite not in node.destroyed
    assert sampler.snapshot('/finite')['state'] == 'sampling'


def test_lease_validation_cap_and_disabled_state():
    _, _, watcher = setup()
    for body in ({}, [], {'operation': 'acquire'}, {'operation': 'acquire', 'client_id': 'not-a-uuid'},
                 {'operation': 'renew'}, {'operation': 'release', 'lease_id': 1}):
        with pytest.raises(ValueError):
            watcher.request(body)
    for _ in range(8):
        acquire(watcher)
    result = watcher.request({'operation': 'acquire', 'client_id': str(uuid.uuid4())})
    assert result['code'] == 'client_limit' and not result['accepted']
    _, _, disabled = setup({'enabled': False})
    assert not disabled.request({'operation': 'acquire', 'client_id': str(uuid.uuid4())})['accepted']


def test_counters_publisher_gid_and_history_stay_bounded_with_unknown_attribution():
    node, clock, watcher = setup()
    discover(watcher, ('/value', 'std_msgs/msg/String'))
    lease = acquire(watcher)
    watcher.reconcile()
    callback = node.callbacks['/value']
    for index in range(80):
        if index % 10 == 0:
            watcher.request({'operation': 'renew', 'lease_id': lease['lease_id']})
        callback(b'private-command-body', SimpleNamespace(publisher_gid=index.to_bytes(2, 'big')))
        clock.value += .5
    callback(b'unknown', None)
    result = data(watcher, '/value')
    assert result['messages'] == 81
    assert len(result['history']) == 20 and len(result['publisher_counts']) == 64
    assert result['unknown_publisher_messages'] == 17
    assert result['publisher_counts'][0]['publisher_gid'] == '0001'
    assert 'private-command' not in json.dumps(watcher.snapshot())
    assert result['rate_hz'] < 3


def test_rolling_budgets_allow_unlimited_duration_below_rate_limit_and_report_real_idle():
    node, clock, watcher = setup({'max_messages_per_window': 20, 'max_bytes_per_window': 100})
    discover(watcher, ('/value', 'std_msgs/msg/String'))
    lease = acquire(watcher)
    watcher.reconcile()
    sample = data(watcher, '/value')['sample_id']
    for _ in range(100):
        watcher.request({'operation': 'renew', 'lease_id': lease['lease_id']})
        node.callbacks['/value'](b'abc', None)
        clock.value += 1
        watcher.reconcile()
    result = data(watcher, '/value')
    assert result['messages'] == 100 and result['state'] == 'watching'
    assert result['sample_id'] == sample and len(node.subscriptions) == 1
    clock.value += 6
    result = data(watcher, '/value')
    assert result['rate_hz'] == 0 and result['last_message_age_ms'] == 7000


def test_burst_budget_throttles_then_resumes_same_counter_identity_without_hot_retry_loop():
    node, clock, watcher = setup({'max_messages_per_window': 2})
    discover(watcher, ('/value', 'std_msgs/msg/String'))
    acquire(watcher)
    watcher.reconcile()
    sample = data(watcher, '/value')['sample_id']
    for _ in range(3):
        node.callbacks['/value'](b'x', None)
    assert data(watcher, '/value')['state'] == 'throttled'
    watcher.reconcile()
    assert len(node.destroyed) == 1
    for _ in range(10):
        watcher.reconcile()
    assert len(node.subscriptions) == 1
    clock.value += 6
    watcher.reconcile()
    assert len(node.subscriptions) == 2
    node.callbacks['/value'](b'x', None)
    result = data(watcher, '/value')
    assert result['messages'] == 4 and result['sample_id'] == sample and result['state'] == 'watching'


def test_known_high_bandwidth_types_never_auto_subscribe_and_unknown_oversized_payload_stops():
    node, clock, watcher = setup()
    topics = [('/raw', 'sensor_msgs/msg/Image'), ('/jpeg', 'sensor_msgs/msg/CompressedImage'),
              ('/cloud', 'sensor_msgs/msg/PointCloud2'), ('/audio', 'audio_msgs/msg/Audio'),
              ('/bytes', 'std_msgs/msg/UInt8MultiArray'), ('/custom', 'custom_msgs/msg/Data')]
    discover(watcher, *topics)
    acquire(watcher)
    watcher.reconcile()
    assert set(node.callbacks) == {'/custom'}
    node.callbacks['/custom'](b'x' * 65537, None)
    watcher.reconcile()
    assert data(watcher, '/custom')['state'] == 'blocked'
    assert data(watcher, '/custom')['messages'] == 1
    clock.value += 6
    watcher.reconcile()
    assert len(node.subscriptions) == 1
    assert all(data(watcher, name)['state'] == 'blocked' for name, _ in topics)


def test_passive_camera_and_ordinary_topic_receipts_need_no_extra_subscription_or_serialization():
    values = {
        '/camera': {'sample_id': 'camera-process-1', 'source': 'dashboard_subscription', 'messages': 50,
                    'bytes': None, 'last_message_age_ms': 10, 'rate_hz': 30,
                    'unattributed_messages': 0,
                    'publisher_counts': [{'publisher_gid': '0102', 'messages': 50, 'last_message_age_ms': 10}],
                    'payload': 'never-copy-this'},
        '/joint_states': {'sample_id': 'state-process-1', 'messages': 100, 'last_message_age_ms': None},
    }
    node, clock, watcher = setup(provider=lambda: values)
    discover(watcher, ('/camera', 'sensor_msgs/msg/CompressedImage'), ('/joint_states', 'sensor_msgs/msg/JointState'))
    acquire(watcher)
    watcher.reconcile()
    assert node.subscriptions == []
    status, result = watcher.snapshot()
    assert status['passive_topics'] == 2 and status['subscriptions'] == 0
    assert result['/camera']['source'] == 'dashboard_subscription'
    assert result['/camera']['bytes'] is None and result['/camera']['messages'] == 50
    assert result['/camera']['unknown_publisher_messages'] == 0
    assert result['/joint_states']['last_message_age_ms'] is None
    assert 'never-copy-this' not in json.dumps(result)
    values['/camera']['messages'] = 51
    assert data(watcher, '/camera')['messages'] == 51


def test_passive_failure_preserves_subscription_ownership_but_marks_activity_unavailable():
    values = {'/value': {'sample_id': 'passive-1', 'messages': 1, 'last_message_age_ms': 0}}
    node, clock, watcher = setup(provider=lambda: values)
    discover(watcher, ('/value', 'std_msgs/msg/String'))
    acquire(watcher)
    watcher.reconcile()
    def fail():
        raise RuntimeError('temporary provider failure')
    watcher.provider = fail
    clock.value += 2
    watcher.reconcile()
    assert node.subscriptions == []
    assert data(watcher, '/value')['state'] == 'unavailable'
    assert data(watcher, '/value')['last_message_age_ms'] == 2000
    assert watcher.snapshot()[0]['errors'][0]['operation'] == 'passive_provider'


def test_topic_ceiling_cleanup_failure_and_topology_changes_keep_handles_owned():
    node, clock, watcher = setup({'max_topics': 2})
    discover(watcher, ('/a', 'std_msgs/msg/String'), ('/b', 'std_msgs/msg/String'), ('/c', 'std_msgs/msg/String'))
    acquire(watcher)
    watcher.reconcile()
    assert len(node.subscriptions) == 2 and data(watcher, '/c')['state'] == 'limited'
    first, original = node.subscriptions[0], node.destroy_subscription
    def destroy(handle):
        if handle is first:
            raise RuntimeError('cleanup failed')
        original(handle)
    node.destroy_subscription = destroy
    discover(watcher, ('/c', 'std_msgs/msg/String'))
    watcher.reconcile()
    assert watcher.snapshot()[0]['cleanup_pending'] == 1
    assert watcher.snapshot()[0]['subscriptions'] == 2  # failed /a plus new /c
    assert data(watcher, '/c')['state'] == 'watching'
    node.destroy_subscription = original
    watcher.close()
    assert not watcher.subscriptions


def test_new_lease_after_full_expiry_gets_new_counter_identity_and_late_callbacks_cannot_count():
    node, clock, watcher = setup()
    discover(watcher, ('/value', 'std_msgs/msg/String'))
    acquire(watcher)
    watcher.reconcile()
    callback, previous = node.callbacks['/value'], data(watcher, '/value')['sample_id']
    clock.value += 13
    acquire(watcher)
    callback(b'late-before-reconcile', None)
    assert watcher.records['/value']['messages'] == 0
    watcher.reconcile()
    callback(b'late', None)
    assert data(watcher, '/value')['messages'] == 0
    assert data(watcher, '/value')['sample_id'] != previous


def test_missing_type_and_resolver_failure_are_explicit_and_retry_is_bounded():
    node, clock, watcher = setup()
    watcher.discover([{'name': '/ambiguous', 'types': ['a/msg/A', 'b/msg/B']}, {'name': '/missing', 'types': ['missing/msg/A']}])
    attempts = []
    def resolve(kind):
        attempts.append(kind)
        raise ImportError('message package unavailable')
    watcher.resolver = resolve
    acquire(watcher)
    for _ in range(10):
        watcher.reconcile()
    assert attempts == ['missing/msg/A'] and node.subscriptions == []
    assert data(watcher, '/ambiguous')['state'] == 'unavailable'
    assert data(watcher, '/ambiguous')['messages'] is None
    assert data(watcher, '/missing')['state'] == 'unavailable'
    assert data(watcher, '/missing')['messages'] is None


def test_false_cleanup_result_still_consumes_subscription_slot_until_confirmed_destroyed():
    node, clock, watcher = setup({'max_topics': 1})
    discover(watcher, ('/old', 'std_msgs/msg/String'))
    acquire(watcher)
    watcher.reconcile()
    original = node.destroy_subscription
    node.destroy_subscription = lambda _: False
    discover(watcher, ('/new', 'std_msgs/msg/String'))
    watcher.reconcile()
    assert watcher.snapshot()[0]['cleanup_pending'] == 1
    assert data(watcher, '/new')['state'] == 'limited'
    assert len(node.subscriptions) == 1
    node.destroy_subscription = original
    watcher.reconcile()
    assert len(node.subscriptions) == 2 and data(watcher, '/new')['state'] == 'watching'


def test_watch_cap_keeps_action_metadata_first_and_never_creates_more_than_64_subscriptions():
    node, clock, watcher = setup({'max_topics': 999})
    topics = [(f'/ordinary/{index:03}', 'std_msgs/msg/String') for index in range(70)]
    discover(watcher, *topics, ('/task/_action/status', STATUS_TYPE),
             ('/task/_action/feedback', 'example_interfaces/action/Fibonacci_FeedbackMessage'))
    acquire(watcher)
    watcher.reconcile()
    assert len(node.subscriptions) == 64
    assert '/task/_action/status' in node.callbacks and '/task/_action/feedback' in node.callbacks
    assert watcher.snapshot()[0]['coverage']['limited'] == 8


def test_lease_renewal_does_not_reset_rolling_budget_and_bytes_can_throttle_independently():
    for config, payload in [({'max_messages_per_window': 2}, b'x'),
                            ({'max_bytes_per_window': 2}, b'x')]:
        node, clock, watcher = setup(config)
        discover(watcher, ('/value', 'std_msgs/msg/String'))
        lease = acquire(watcher)
        watcher.reconcile()
        node.callbacks['/value'](payload, None)
        node.callbacks['/value'](payload, None)
        watcher.request({'operation': 'renew', 'lease_id': lease['lease_id']})
        node.callbacks['/value'](payload, None)
        assert data(watcher, '/value')['state'] == 'throttled'
        assert data(watcher, '/value')['messages'] == 3


def test_action_feedback_type_uses_generated_action_impl(monkeypatch):
    _, _, watcher = setup()
    watcher.resolver = None
    utilities, package = ModuleType('rosidl_runtime_py.utilities'), ModuleType('rosidl_runtime_py')
    seen = []
    def get_action(kind):
        seen.append(kind)
        return SimpleNamespace(Impl=SimpleNamespace(FeedbackMessage=object))
    utilities.get_action = get_action
    monkeypatch.setitem(sys.modules, 'rosidl_runtime_py', package)
    monkeypatch.setitem(sys.modules, 'rosidl_runtime_py.utilities', utilities)
    assert watcher._resolve('example_interfaces/action/Fibonacci_FeedbackMessage') is object
    assert seen == ['example_interfaces/action/Fibonacci']


def test_real_action_status_and_feedback_are_counted_without_inventing_client_requests_or_payload_export():
    decoder = lambda _: SimpleNamespace(status_list=[SimpleNamespace(status=1), SimpleNamespace(status=2), SimpleNamespace(status=4)])
    node, clock, watcher = setup(status_decoder=decoder)
    discover(watcher, ('/task/_action/status', STATUS_TYPE), ('/task/_action/feedback', 'example_interfaces/action/Fibonacci_FeedbackMessage'))
    acquire(watcher)
    watcher.reconcile()
    node.callbacks['/task/_action/status'](b'status-binary', SimpleNamespace(publisher_gid=b'\x01'))
    node.callbacks['/task/_action/feedback'](b'private-feedback-data', SimpleNamespace(publisher_gid=b'\x02'))
    result = action_activity('/task', watcher.snapshot()[1])
    assert result['state'] == 'watching' and result['messages'] == 2
    assert result['status_messages'] == result['feedback_messages'] == 1
    assert result['status']['status_summary']['counts']['executing'] == 1
    assert result['requests']['state'] == 'unavailable'
    assert result['status_topic'] == '/task/_action/status'
    assert 'private-feedback' not in json.dumps(result)
    assert action_activity('/undiscovered', {})['messages'] is None


def test_graph_snapshot_additive_activity_preserves_finite_sampling_contract():
    node, clock, watcher = setup()
    sampler = TrafficSampler(node, resolver=lambda _: object, qos_factory=lambda: None, clock=clock, wall=clock.wall)
    graph = GraphObserver(node, action_api=Actions(), sampler=sampler, watcher=watcher, clock=clock, wall=clock.wall)
    graph.refresh()
    graph.watch({'operation': 'acquire', 'client_id': str(uuid.uuid4())})
    watcher.reconcile()
    node.callbacks['/arbitrary'](b'data', SimpleNamespace(publisher_gid=b'\x01\x02'))
    result = graph.snapshot()
    topic = next(topic for topic in result['topics'] if topic['name'] == '/arbitrary')
    assert result['watch']['active'] and topic['activity']['messages'] == 1
    assert topic['traffic']['state'] == 'not_sampled'
    assert graph.sample({'topic': '/arbitrary'})['state'] == 'sampling'
    assert result['actions'][0]['activity']['requests']['state'] == 'unavailable'
    graph.close()


def test_callback_requires_message_info_and_attributes_real_mapping_metadata_to_each_publisher():
    node, clock, watcher = setup()
    discover(watcher, ('/value', 'std_msgs/msg/String'))
    acquire(watcher)
    watcher.reconcile()
    callback = node.callbacks['/value']
    # rclpy first tries to bind just the message. An optional info argument
    # incorrectly succeeds here and causes all real GIDs to be omitted.
    with pytest.raises(TypeError):
        inspect.signature(callback).bind(object())
    inspect.signature(callback).bind(object(), object())
    callback(b'a', {'publisher_gid': b'\x01\x02', 'source_timestamp': 123})
    callback(b'b', {'publisher_gid': b'\x03\x04'})
    callback(b'c', {'publisher_gid': b'\x01\x02'})
    callback(b'd', {})
    result = data(watcher, '/value')
    assert {item['publisher_gid']: item['messages'] for item in result['publisher_counts']} == {'0102': 2, '0304': 1}
    assert result['unknown_publisher_messages'] == 1


def test_global_byte_budget_limits_aggregate_raw_topics_and_recovers_after_the_rolling_window():
    node, clock, watcher = setup()
    discover(watcher, *[(f'/value/{index}', 'std_msgs/msg/String') for index in range(3)])
    acquire(watcher)
    watcher.reconcile()
    payload = b'x' * 65536
    for index in range(2):
        for _ in range(128):
            node.callbacks[f'/value/{index}'](payload, None)
        assert data(watcher, f'/value/{index}')['state'] == 'watching'
    node.callbacks['/value/2'](payload, None)
    result = data(watcher, '/value/2')
    assert result['state'] == 'throttled' and 'Global' in result['reason']
    status = watcher.snapshot()[0]
    assert status['global_max_bytes_per_window'] == 16 * 1024 * 1024
    assert status['global_window_bytes'] == 257 * 65536
    clock.value += 6
    assert watcher.snapshot()[0]['global_window_bytes'] == 0
    watcher.reconcile()
    node.callbacks['/value/2'](b'resumed', None)
    assert data(watcher, '/value/2')['state'] == 'watching'


def test_global_message_budget_applies_across_topics_while_passive_counts_do_not_consume_it():
    provider = lambda: {'/camera': {'sample_id': 'existing-camera', 'messages': 1000000, 'bytes': None}}
    node, clock, watcher = setup(provider=provider)
    discover(watcher, *[(f'/value/{index}', 'std_msgs/msg/String') for index in range(6)],
             ('/camera', 'sensor_msgs/msg/CompressedImage'))
    acquire(watcher)
    watcher.reconcile()
    assert watcher.snapshot()[0]['global_window_messages'] == 0
    for index in range(5):
        for _ in range(10000):
            node.callbacks[f'/value/{index}'](b'x', None)
    node.callbacks['/value/5'](b'x', None)
    assert 'Global' in data(watcher, '/value/5')['reason']
    status = watcher.snapshot()[0]
    assert status['global_max_messages_per_window'] == 50000
    assert status['global_window_messages'] == 50001
    assert data(watcher, '/camera')['state'] == 'watching'


def test_passive_history_keeps_only_valid_bounded_counter_buckets_and_nullable_bytes():
    history = [{'at_ns': index * 500000000, 'messages': index % 3, 'bytes': None, 'payload': 'not-exported'} for index in range(30)]
    values = {'/camera': {'sample_id': 'camera', 'messages': 30, 'history': history}}
    node, clock, watcher = setup(provider=lambda: values)
    discover(watcher, ('/camera', 'sensor_msgs/msg/CompressedImage'))
    acquire(watcher)
    watcher.reconcile()
    result = data(watcher, '/camera')['history']
    assert len(result) == 20 and result[0]['at_ns'] == 10 * 500000000
    assert all(item['bytes'] is None for item in result)
    assert 'not-exported' not in json.dumps(result)
    values['/camera']['history'] = [
        {'at_ns': 1, 'messages': 0, 'bytes': None},
        {'at_ns': 2, 'messages': True, 'bytes': None},
        {'at_ns': -1, 'messages': 1, 'bytes': None},
        {'at_ns': 3, 'messages': 1, 'bytes': 'unknown'},
        {'at_ns': 4, 'messages': 2, 'bytes': 12},
    ]
    assert data(watcher, '/camera')['history'] == [
        {'at_ns': 1, 'messages': 0, 'bytes': None}, {'at_ns': 4, 'messages': 2, 'bytes': 12}]
