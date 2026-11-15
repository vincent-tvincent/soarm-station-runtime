import copy
import json
from types import SimpleNamespace

import pytest

from soarm_station.graph_observer import GraphObserver, TrafficSampler
from soarm_station.graph_transport import project_transport_activity
from test_graph_observer import Node, Clock, Actions


BASE = '/camera/image_raw/compressed'
TARGET = BASE + '/_buf_cpu'
TYPE = 'sensor_msgs/msg/CompressedImage'


def topic(name, gids, kind=TYPE):
    return {'name': name, 'types': [kind], 'publishers': [
        {'id': 'publisher:' + gid, 'gid': gid, 'type': kind, 'node_id': 'node:unresolved:' + gid}
        for gid in gids]}


def fixture():
    topics = [topic(BASE, ['aa01']), topic(TARGET, ['bb01'])]
    source = {'state': 'watching', 'source': 'dashboard_subscription', 'sample_id': 'base-epoch-1',
              'messages': 12, 'bytes': 1200, 'rate_hz': 6., 'bytes_per_second': 600.,
              'last_message_age_ms': 10., 'unknown_publisher_messages': 0,
              'publisher_counts': [{'publisher_gid': 'bb01', 'messages': 12, 'last_message_age_ms': 10.}],
              'history': [{'at_ns': 1000, 'messages': 6, 'bytes': 600}, {'at_ns': 2000, 'messages': 6, 'bytes': 600}]}
    direct = {'state': 'blocked', 'sample_id': None, 'messages': None,
              'reason': 'High-bandwidth type requires passive source'}
    return topics, {BASE: source, TARGET: direct}


def test_exact_transport_gid_projects_metadata_without_mutating_inputs_or_guessing_owner():
    topics, activity = fixture()
    before = copy.deepcopy((topics, activity))
    result = project_transport_activity(topics, activity)
    projected = result[TARGET]
    assert projected['state'] == 'watching' and projected['messages'] == 12
    assert projected['source'] == 'transport_gid_projection'
    assert projected['measurement_scope'] == 'logical_delivery_attributed_to_transport_gid'
    assert projected['bytes'] is None and projected['bytes_per_second'] is None
    assert projected['publisher_counts'] == activity[BASE]['publisher_counts']
    assert projected['last_message_age_ms'] == 10.
    assert projected['rate_hz'] == 6.
    assert all(bucket['bytes'] is None for bucket in projected['history'])
    assert projected['projection'] == {
        'kind': 'exact_publisher_gid', 'source_topic': BASE, 'source_sample_id': 'base-epoch-1',
        'publisher_gids': ['bb01'], 'complete_source_coverage': True, 'original_state': 'blocked',
        'delivery_scope': 'local_logical_subscription', 'subscriber_delivery_confirmed': False, 'packet_count': False}
    assert (topics, activity) == before
    result[BASE]['publisher_counts'][0]['messages'] = 999
    projected['publisher_counts'][0]['messages'] = 888
    assert (topics, activity) == before


@pytest.mark.parametrize('variant', ['missing_base', 'wrong_base', 'different_type', 'different_type_set',
                                    'missing_type', 'missing_gid', 'different_gid', 'same_participant_only',
                                    'duplicate_global_gid', 'duplicate_target_gid', 'duplicate_base_topic',
                                    'wrong_endpoint_type', 'duplicate_counter_gid', 'unrecognized_suffix'])
def test_no_projection_when_identity_type_or_global_publisher_uniqueness_is_not_proven(variant):
    topics, activity = fixture()
    if variant == 'missing_base':
        topics = topics[1:]
    elif variant == 'wrong_base':
        topics[0]['name'] = '/other/camera'
    elif variant == 'different_type':
        topics[1]['types'] = ['sensor_msgs/msg/Image']
    elif variant == 'different_type_set':
        topics[1]['types'].append('sensor_msgs/msg/Image')
    elif variant == 'missing_type':
        topics[0]['types'] = []
    elif variant == 'missing_gid':
        topics[1]['publishers'][0]['gid'] = None
    elif variant == 'different_gid':
        activity[BASE]['publisher_counts'][0]['publisher_gid'] = 'cc01'
    elif variant == 'same_participant_only':
        topics[1]['publishers'][0]['gid'] = '11' * 12 + '00000103'
        activity[BASE]['publisher_counts'][0]['publisher_gid'] = '11' * 12 + '00000203'
    elif variant == 'duplicate_global_gid':
        topics.append(topic('/unrelated', ['bb01'], 'std_msgs/msg/String'))
    elif variant == 'duplicate_target_gid':
        topics[1]['publishers'].append(copy.deepcopy(topics[1]['publishers'][0]))
    elif variant == 'duplicate_base_topic':
        topics.append(copy.deepcopy(topics[0]))
    elif variant == 'wrong_endpoint_type':
        topics[1]['publishers'][0]['type'] = 'sensor_msgs/msg/Image'
    elif variant == 'duplicate_counter_gid':
        activity[BASE]['publisher_counts'].append(copy.deepcopy(activity[BASE]['publisher_counts'][0]))
    elif variant == 'unrecognized_suffix':
        topics[1]['name'] = BASE + '/_buf_cpu/not-a-buffer-topic'
    result = project_transport_activity(topics, activity)
    assert result == activity, variant


@pytest.mark.parametrize('field,value', [('state', 'inactive'), ('state', 'unavailable'),
                                       ('sample_id', None), ('sample_id', ''), ('messages', None),
                                       ('messages', -1), ('messages', True), ('messages', 11),
                                       ('source', 'transport_gid_projection')])
def test_source_must_be_current_with_valid_counter_epoch_and_consistent_counts(field, value):
    topics, activity = fixture()
    activity[BASE][field] = value
    assert project_transport_activity(topics, activity) == activity


def test_partial_gid_coverage_projects_only_matched_counts_with_no_aggregate_rate_or_history():
    topics, activity = fixture()
    topics.append(topic(BASE + '/_buf_gpu', ['cc01']))
    activity[BASE]['messages'] = 20
    activity[BASE]['publisher_counts'].append({'publisher_gid': 'cc01', 'messages': 8, 'last_message_age_ms': 5.})
    result = project_transport_activity(topics, activity)
    assert result[TARGET]['messages'] == 12 and result[TARGET]['last_message_age_ms'] == 10.
    assert result[BASE + '/_buf_gpu']['messages'] == 8
    for name in (TARGET, BASE + '/_buf_gpu'):
        assert result[name]['rate_hz'] is None and result[name]['history'] == []
        assert not result[name]['projection']['complete_source_coverage']
        assert result[name]['unknown_publisher_messages'] == 0


@pytest.mark.parametrize('unknown', [None, 1, True])
def test_unknown_source_attribution_never_borrows_aggregate_rate_or_history(unknown):
    topics, activity = fixture()
    activity[BASE]['unknown_publisher_messages'] = unknown
    result = project_transport_activity(topics, activity)[TARGET]
    assert result['messages'] == 12
    assert result['rate_hz'] is None and result['history'] == []
    assert not result['projection']['complete_source_coverage']


def test_multiple_exact_publisher_counters_sum_and_keep_individual_ages():
    topics, activity = fixture()
    topics[1]['publishers'].append(topic(TARGET, ['cc01'])['publishers'][0])
    activity[BASE]['messages'] = 20
    activity[BASE]['publisher_counts'].append({'publisher_gid': 'cc01', 'messages': 8, 'last_message_age_ms': 5.})
    result = project_transport_activity(topics, activity)[TARGET]
    assert result['messages'] == 20 and result['last_message_age_ms'] == 5.
    assert result['publisher_counts'] == activity[BASE]['publisher_counts']
    assert result['projection']['complete_source_coverage']
    activity[BASE]['publisher_counts'][0]['last_message_age_ms'] = None
    activity[BASE]['publisher_counts'][1]['last_message_age_ms'] = float('nan')
    assert project_transport_activity(topics, activity)[TARGET]['last_message_age_ms'] is None


def test_projection_identity_is_stable_across_updates_but_changes_with_epoch_target_or_gid_set():
    topics, activity = fixture()
    first = project_transport_activity(topics, activity)[TARGET]['sample_id']
    activity[BASE]['messages'] = activity[BASE]['publisher_counts'][0]['messages'] = 13
    assert project_transport_activity(topics, activity)[TARGET]['sample_id'] == first
    activity[BASE]['messages'] = activity[BASE]['publisher_counts'][0]['messages'] = 0
    reset = project_transport_activity(topics, activity)[TARGET]
    assert reset['messages'] == 0 and reset['sample_id'] == first  # Consumers detect counter regression.
    activity[BASE]['sample_id'] = 'base-epoch-2'
    assert project_transport_activity(topics, activity)[TARGET]['sample_id'] != first
    activity[BASE]['sample_id'] = 'base-epoch-1'
    topics[1]['publishers'][0]['gid'] = 'cc01'
    activity[BASE]['publisher_counts'][0]['publisher_gid'] = 'cc01'
    assert project_transport_activity(topics, activity)[TARGET]['sample_id'] != first
    topics, activity = fixture()
    new_target = BASE + '/_buf_other'
    topics[1]['name'] = new_target
    assert project_transport_activity(topics, activity)[new_target]['sample_id'] != first


def test_direct_live_activity_wins_even_when_idle():
    topics, activity = fixture()
    activity[TARGET] = {'state': 'watching', 'source': 'ros_subscription', 'sample_id': 'direct',
                        'messages': 0, 'last_message_age_ms': None}
    assert project_transport_activity(topics, activity) == activity


def test_borrowed_history_is_bounded_and_never_exports_logical_byte_counts_or_extra_fields():
    topics, activity = fixture()
    activity[BASE]['history'] = [{'at_ns': n, 'messages': 1, 'bytes': 100, 'private': 'omit'} for n in range(30)]
    result = project_transport_activity(topics, activity)[TARGET]
    assert len(result['history']) == 20 and result['history'][0]['at_ns'] == 10
    assert all(item['bytes'] is None for item in result['history'])
    assert 'omit' not in json.dumps(result)


def test_graph_snapshot_projects_without_new_subscriptions_and_recomputes_display_coverage():
    topics, activity = fixture()
    node, clock = Node(), Clock()
    node.get_topic_names_and_types = lambda: [(entry['name'], entry['types']) for entry in topics]
    node.get_publishers_info_by_topic = lambda name: [SimpleNamespace(
        node_name='producer' if name == BASE else '_NODE_NAME_UNKNOWN_',
        node_namespace='/elsewhere' if name == BASE else '_NODE_NAMESPACE_UNKNOWN_',
        endpoint_gid=bytes.fromhex('aa01' if name == BASE else 'bb01'), topic_type=TYPE, qos_profile=None)]
    class Watcher:
        def discover(self, _): pass
        def snapshot(self):
            return {'active': True, 'subscriptions': 0, 'coverage': {'watching': 1, 'blocked': 1}}, copy.deepcopy(activity)
    sampler = TrafficSampler(node, resolver=lambda _: object, qos_factory=lambda: None, clock=clock, wall=clock.wall)
    graph = GraphObserver(node, action_api=Actions(), sampler=sampler, watcher=Watcher(), clock=clock, wall=clock.wall)
    graph.refresh()
    before = copy.deepcopy(graph.current)
    result = graph.snapshot()
    assert node.subscriptions == []
    assert result['watch']['coverage'] == {'watching': 2}
    assert result['watch']['direct_coverage'] == {'watching': 1, 'blocked': 1}
    assert result['watch']['projected_topics'] == 1 and result['watch']['subscriptions'] == 0
    assert next(entry for entry in result['topics'] if entry['name'] == TARGET)['activity']['messages'] == 12
    assert graph.current == before
    assert any(owner.get('discovery_state') == 'unresolved_endpoint_owner' for owner in result['nodes'])
