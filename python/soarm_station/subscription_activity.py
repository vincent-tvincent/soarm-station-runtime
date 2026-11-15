"""Passive receipt counters for subscriptions the dashboard already owns.

No message payload is inspected, copied or serialized. Publisher attribution
uses ROS MessageInfo metadata; absent metadata remains unattributed.
"""
import threading
import time
import uuid
from collections import OrderedDict,deque
from collections.abc import Mapping


def publisher_gid(info):
    """Return a bounded canonical GID, or None when attribution is unavailable."""
    # Lyrical's MessageInfo is a TypedDict passed directly by the executor.
    value=info.get('publisher_gid') if isinstance(info,Mapping) else getattr(info,'publisher_gid',None)
    try:
        # PublisherGID itself is a TypedDict in Lyrical: only its data field
        # identifies the endpoint; the middleware identifier is not GID data.
        if isinstance(value,Mapping):value=value.get('data')
        if isinstance(value,Mapping):return None
        if isinstance(value,str):
            if not value or len(value)>128:return None
            value=bytes.fromhex(value)
        else:
            if value is None or not 0<len(value)<=64:return None
            value=bytes(value)
    except (TypeError,ValueError,OverflowError):return None
    return value.hex() if value and len(value)<=64 and any(value) else None


class SubscriptionActivity:
    """Thread-safe, bounded counters keyed by resolved ROS topic name."""
    def __init__(self,clock=time.monotonic,window_s=2.,max_publishers=64,max_receipts=4096,wall=time.time_ns):
        self.clock=clock;self.wall=wall;self.window_s=window_s
        self.max_publishers=max_publishers;self.max_receipts=max_receipts
        self._lock=threading.Lock();self._topics={}

    def register(self,topic):
        """Track one existing subscription per topic, avoiding duplicate receipts."""
        with self._lock:
            if topic in self._topics:return False
            self._topics[topic]={'sample_id':uuid.uuid4().hex,'created':self.clock(),
                'created_at_ns':self.wall(),'buckets':OrderedDict(),
                'messages':0,'last':None,'recent':deque(maxlen=self.max_receipts),
                'history_overflow_until':0.,'publishers':{},'unattributed_messages':0}
            return True

    def receive(self,topic,info):
        gid=publisher_gid(info)
        with self._lock:
            entry=self._topics.get(topic)
            if entry is None:return
            now=self.clock()
            entry['messages']+=1;entry['last']=now
            bucket=int(max(0.,now-entry['created'])/.5)
            buckets=entry['buckets'];buckets[bucket]=buckets.get(bucket,0)+1
            while len(buckets)>20:buckets.popitem(last=False)
            recent=entry['recent'];cutoff=now-self.window_s
            while recent and recent[0]<=cutoff:recent.popleft()
            if len(recent)==self.max_receipts:
                entry['history_overflow_until']=now+self.window_s
            recent.append(now)
            publishers=entry['publishers']
            if gid is not None and (gid in publishers or len(publishers)<self.max_publishers):
                counter=publishers.setdefault(gid,{'messages':0,'last':None})
                counter['messages']+=1;counter['last']=now
            else:entry['unattributed_messages']+=1

    def snapshot(self):
        """Copy counters/history; only local receipt clocks determine ages/rates."""
        result={}
        with self._lock:
            now=self.clock()
            for topic,entry in self._topics.items():
                recent=entry['recent'];cutoff=now-self.window_s
                while recent and recent[0]<=cutoff:recent.popleft()
                observed=min(self.window_s,max(0.,now-entry['created']))
                rate=(len(recent)/observed if entry['last'] is not None and observed>0
                      and now>=entry['history_overflow_until'] else None)
                end=int(max(0.,now-entry['created'])/.5);start=max(0,end-19)
                buckets=entry['buckets']
                while buckets and next(iter(buckets))<start:buckets.popitem(last=False)
                # Zero buckets mean this existing subscription observed no receipts
                # in that interval. No history is invented before registration.
                history=[{'at_ns':entry['created_at_ns']+index*500_000_000,
                          'messages':buckets.get(index,0),'bytes':None}
                         for index in range(start,end+1)]
                result[topic]={'source':'dashboard_subscription','sample_id':entry['sample_id'],
                    'messages':entry['messages'],'bytes':None,'rate_hz':rate,
                    'history':history,
                    'window_s':self.window_s,'observed_window_s':observed,
                    'last_message_age_ms':None if entry['last'] is None else max(0.,now-entry['last'])*1000,
                    'unattributed_messages':entry['unattributed_messages'],
                    'publisher_counts':[
                        {'publisher_gid':gid,'messages':counter['messages'],
                         'last_message_age_ms':max(0.,now-counter['last'])*1000}
                        for gid,counter in entry['publishers'].items()]}
        return result


def observe_subscription(activity,create_subscription,message_type,topic,callback,qos):
    """Wrap a message-only handler without changing its subscription or errors.

    Two required callback parameters tell rclpy to supply MessageInfo. Register
    the returned subscription's resolved/remapped name, not a guessed graph name.
    """
    observed_topic=None
    def receive(message,message_info):
        if observed_topic is not None:activity.receive(observed_topic,message_info)
        return callback(message)
    subscription=create_subscription(message_type,topic,receive,qos)
    resolved=getattr(subscription,'topic_name',topic)
    if activity.register(resolved):observed_topic=resolved
    return subscription
