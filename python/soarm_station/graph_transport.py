"""Attribute existing logical receipt counters to exact transport publisher GIDs.

This is response assembly only: no ROS subscriptions, inferred node ownership,
payload handling, packet counting, or claims about other subscribers' delivery.
"""
import copy
import hashlib
import json
import math
import re
from collections import defaultdict


TRANSPORT_TOPIC = re.compile(r'(?P<base>.+)/_buf_[A-Za-z0-9_]+$')


def _count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _age(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def _gid(value):
    if not isinstance(value, str) or not value or len(value) > 128:
        return None
    try:
        data = bytes.fromhex(value)
        return data.hex() if data and any(data) else None
    except ValueError:
        return None


def _types(topic):
    values = topic.get('types')
    if not isinstance(values, list) or not values or any(not isinstance(item, str) or not item for item in values):
        return None
    return frozenset(values)


def project_transport_activity(topics, activity):
    """Return independent activity records with strict, explicitly scoped projections.

An endpoint GID must occur once across *all* discovered publisher entries and
belong to the matching buffer topic. The logical source must be watching now.
Source subsets can provide attributable counts, but never an aggregate rate or
history. Direct, currently watching activity always takes precedence.
"""
    result = copy.deepcopy(activity)
    by_name, publishers = defaultdict(list), defaultdict(list)
    for topic in topics:
        by_name[topic.get('name')].append(topic)
        for endpoint in topic.get('publishers', []):
            gid = _gid(endpoint.get('gid'))
            if gid:
                publishers[gid].append((topic, endpoint))
    for target in topics:
        name = target.get('name', '')
        match = TRANSPORT_TOPIC.fullmatch(name)
        if not match or len(by_name[name]) != 1:
            continue
        direct = activity.get(name, {})
        if direct.get('state') == 'watching':
            continue
        base_name = match['base']
        if len(by_name[base_name]) != 1:
            continue
        base = by_name[base_name][0]
        types = _types(target)
        if not types or _types(base) != types:
            continue
        source = activity.get(base_name, {})
        epoch = source.get('sample_id')
        if (source.get('state') != 'watching' or source.get('source') == 'transport_gid_projection'
                or not isinstance(epoch, str) or not epoch or not _count(source.get('messages'))):
            continue
        counters = source.get('publisher_counts')
        if not isinstance(counters, list):
            continue
        by_gid = defaultdict(list)
        valid_source = True
        for counter in counters:
            if not isinstance(counter, dict):
                valid_source = False
                continue
            gid = _gid(counter.get('publisher_gid'))
            if not gid or not _count(counter.get('messages')):
                valid_source = False
                continue
            by_gid[gid].append(counter)
        matched = []
        for gid, items in sorted(by_gid.items()):
            endpoints = publishers.get(gid, [])
            if len(items) != 1 or len(endpoints) != 1:
                continue
            endpoint_topic, endpoint = endpoints[0]
            if endpoint_topic is not target or endpoint.get('type', next(iter(types))) not in types:
                continue
            counter = items[0]
            age = counter.get('last_message_age_ms')
            matched.append({'publisher_gid': gid, 'messages': counter['messages'],
                            'last_message_age_ms': age if _age(age) else None})
        if not matched:
            continue
        messages = sum(item['messages'] for item in matched)
        if messages > source['messages']:
            continue
        complete = (valid_source and len(matched) == len(counters) and messages == source['messages']
                    and _count(source.get('unknown_publisher_messages'))
                    and source['unknown_publisher_messages'] == 0)
        gids = [item['publisher_gid'] for item in matched]
        identity = json.dumps([base_name, epoch, name, gids], separators=(',', ':'), ensure_ascii=True)
        sample_id = 'transport:' + hashlib.sha256(identity.encode('utf-8')).hexdigest()
        ages = [item['last_message_age_ms'] for item in matched if item['last_message_age_ms'] is not None]
        rate = source.get('rate_hz')
        history = []
        if complete:
            buckets = source.get('history')
            for bucket in (buckets if isinstance(buckets, list) else [])[-20:]:
                if isinstance(bucket, dict) and _count(bucket.get('at_ns')) and _count(bucket.get('messages')):
                    history.append({'at_ns': bucket['at_ns'], 'messages': bucket['messages'], 'bytes': None})
        result[name] = {
            'state': 'watching', 'sample_id': sample_id, 'source': 'transport_gid_projection',
            'measurement_scope': 'logical_delivery_attributed_to_transport_gid',
            'messages': messages, 'bytes': None, 'bytes_per_second': None,
            'rate_hz': rate if complete and _age(rate) else None,
            'last_message_age_ms': min(ages) if ages else None,
            'history': history, 'publisher_counts': matched, 'unknown_publisher_messages': 0,
            'payload_export': False,
            'reason': 'Existing logical subscription receipts attributed by exact transport publisher GID; not transport packet or subscriber-delivery counts',
            'projection': {
                'kind': 'exact_publisher_gid', 'source_topic': base_name,
                'source_sample_id': epoch, 'publisher_gids': gids,
                'complete_source_coverage': complete, 'original_state': direct.get('state'),
                'delivery_scope': 'local_logical_subscription', 'subscriber_delivery_confirmed': False,
                'packet_count': False,
            },
        }
    return result
