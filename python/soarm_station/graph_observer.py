"""Read-only ROS discovery adapter and bounded traffic counters.

Accepts any rclpy-compatible node. No robot configuration, command clients,
payload decoding or application topic-name assumptions are used here.

Snapshot timing is additive: captured_at_ns remains the topology's wall-clock
capture. activity_captured_at_ns is a decimal-string monotonic timestamp taken
when live counter snapshotting completes, scoped by activity_clock_id. Compare
its differences only within that observer and the same counter/sample epoch;
it is neither a packet timestamp nor a browser/network-latency measurement.
"""
import copy
import math
import threading
import time
import uuid
from collections import Counter,OrderedDict
from .graph_watch import GraphWatch, action_activity, empty_activity
from .graph_transport import project_transport_activity


def node_name(name,namespace):
    return (namespace.rstrip('/')+'/'+name).replace('//','/')


def node_record(name,namespace,count=1):
    full=node_name(name,namespace)
    return {'id':'node:'+full,'name':name,'namespace':namespace,'fully_qualified_name':full,'instance_count':count}


def qos_dict(profile):
    if profile is None:return None
    result={}
    for key in ('reliability','durability','history','liveliness'):
        value=getattr(profile,key,None);result[key]=getattr(value,'name',None) if value is not None else None
    result['depth']=getattr(profile,'depth',None)
    for key in ('deadline','lifespan','liveliness_lease_duration'):
        value=getattr(getattr(profile,key,None),'nanoseconds',None)
        result[key+'_ns']=str(value) if value is not None else None
    return result


class TrafficSampler:
    BLOCKED_TYPES={'sensor_msgs/msg/Image','sensor_msgs/msg/CompressedImage','sensor_msgs/msg/PointCloud',
        'sensor_msgs/msg/PointCloud2','nav_msgs/msg/OccupancyGrid','std_msgs/msg/ByteMultiArray','std_msgs/msg/UInt8MultiArray'}

    def __init__(self,node,config=None,resolver=None,qos_factory=None,clock=time.monotonic,wall=time.time_ns):
        c=config or {};self.node=node;self.clock=clock;self.wall=wall;self.resolver=resolver;self.qos_factory=qos_factory
        self.enabled=c.get('enabled',True);self.max_topics=min(2,max(1,int(c.get('max_topics',2))))
        self.max_duration_s=min(10.,max(.1,float(c.get('max_duration_s',10))))
        self.lock=threading.RLock();self.request_lock=threading.Lock();self.cleanup_lock=threading.Lock();self.closed=False;self.cleanup_errors={}
        self.samples=OrderedDict();self.subscriptions={}

    def request(self,topic,types,duration=5.,stop=False):
        with self.request_lock:
            if self.closed:return self._denied(topic,'unavailable','Observer stopped')
            return self._request(topic,types,duration,stop)

    def _request(self,topic,types,duration,stop):
        if stop:
            with self.lock:
                sample=self.samples.get(topic)
                if sample:self._finish(sample,'Stopped by request')
            self.expire()
            return {'schema_version':1,'accepted':True,'topic':topic,'sample_id':sample['sample_id'] if sample else None,'state':'complete'}
        if not isinstance(duration,(int,float)) or isinstance(duration,bool) or not math.isfinite(duration) or not 0<duration<=self.max_duration_s:
            raise ValueError(f'duration_s must be positive and at most {self.max_duration_s:g}')
        if not self.enabled:return self._denied(topic,'blocked','Traffic sampling is disabled')
        if len(types)!=1:return self._denied(topic,'unavailable','A single discoverable message type is required')
        kind=types[0]
        if kind in self.BLOCKED_TYPES or kind.startswith(('audio_msgs/','audio_common_msgs/')):
            return self._denied(topic,'blocked','High-bandwidth message type is not sampled')
        with self.lock:
            if topic in self.subscriptions:self._finish(self.samples[topic],'Replaced by a new sample')
        self.expire()
        with self.lock:
            if topic in self.subscriptions:return self._denied(topic,'blocked','Previous sample subscription cleanup is pending')
            if len(self.subscriptions)>=self.max_topics:return self._denied(topic,'blocked','Concurrent sampling limit reached')
        try:
            if self.resolver is None:
                from rosidl_runtime_py.utilities import get_message
                message_type=get_message(kind)
            else:message_type=self.resolver(kind)
            if self.qos_factory is None:
                from rclpy.qos import QoSProfile,ReliabilityPolicy,DurabilityPolicy
                qos=QoSProfile(depth=1,reliability=ReliabilityPolicy.BEST_EFFORT,durability=DurabilityPolicy.VOLATILE)
            else:qos=self.qos_factory()
            start=self.clock();sample={'sample_id':uuid.uuid4().hex,'state':'sampling','start':start,'end':start+duration,
                'started_at_ns':self.wall(),'ended_at_ns':None,'messages':0,'bytes':0,'last':None,'buckets':OrderedDict(),'reason':''}
            def receive(data):self._receive(topic,sample,data)
            subscription=self.node.create_subscription(message_type,topic,receive,qos,raw=True)
        except Exception as exc:return self._denied(topic,'unavailable','Cannot subscribe to this type: '+type(exc).__name__)
        with self.lock:
            self.samples[topic]=sample;self.samples.move_to_end(topic);self.subscriptions[topic]=subscription
            while len(self.samples)>64:
                key=next((k for k in self.samples if k not in self.subscriptions),None)
                if key is None:break
                self.samples.pop(key)
        return {'schema_version':1,'accepted':True,'topic':topic,'sample_id':sample['sample_id'],'state':'sampling'}

    def _denied(self,topic,state,reason):
        with self.lock:
            # A denied replacement must not overwrite the counters of an active sample.
            if topic not in self.subscriptions:
                self.samples[topic]={'state':state,'reason':reason,'sample_id':None}
                while len(self.samples)>64:
                    key=next((k for k in self.samples if k not in self.subscriptions),None)
                    if key is None:break
                    self.samples.pop(key)
        return {'schema_version':1,'accepted':False,'topic':topic,'sample_id':None,'state':state,'reason':reason}

    def _finish(self,sample,reason):
        if sample.get('state')=='sampling':
            sample['state']='complete';sample['end']=min(self.clock(),sample['end']);sample['ended_at_ns']=self.wall();sample['reason']=reason

    def _receive(self,topic,sample,data):
        size=len(data)
        with self.lock:
            if self.samples.get(topic) is not sample or sample['state']!='sampling':return
            tick=self.clock()
            if tick>=sample['end']:self._finish(sample,'Sampling duration elapsed');return
            sample['messages']+=1;sample['bytes']+=size;sample['last']=tick
            bucket=int((tick-sample['start'])/.5)
            entry=sample['buckets'].setdefault(bucket,{'at_ns':sample['started_at_ns']+int(bucket*.5e9),'messages':0,'bytes':0})
            entry['messages']+=1;entry['bytes']+=size
            while len(sample['buckets'])>20:sample['buckets'].popitem(last=False)
            if size>65536:self._finish(sample,'Message exceeds 64 KiB observation budget')
            elif sample['messages']>=2000 or sample['bytes']>=1048576:self._finish(sample,'Sample message/byte budget reached')

    def expire(self):
        # Keep ownership until destroy succeeds; one failed handle must not lose
        # other handles or consume untracked subscriptions beyond the hard cap.
        with self.cleanup_lock:
            removed=[]
            with self.lock:
                for topic,subscription in self.subscriptions.items():
                    sample=self.samples[topic]
                    if self.clock()>=sample.get('end',float('inf')):self._finish(sample,'Sampling duration elapsed')
                    if sample['state']!='sampling':removed.append((topic,subscription))
            for topic,subscription in removed:
                try:self.node.destroy_subscription(subscription)
                except Exception as exc:
                    with self.lock:self.cleanup_errors[topic]=type(exc).__name__
                    continue
                with self.lock:
                    if self.subscriptions.get(topic) is subscription:self.subscriptions.pop(topic)
                    self.cleanup_errors.pop(topic,None)

    def status(self):
        with self.lock:
            return {'enabled':self.enabled,'max_topics':self.max_topics,'max_duration_s':self.max_duration_s,
                'payload_export':False,'cleanup_pending':len(self.cleanup_errors),
                'cleanup_errors':[{'topic':topic,'error':error} for topic,error in self.cleanup_errors.items()]}

    def snapshot(self,topic):
        with self.lock:
            sample=self.samples.get(topic)
            empty={'state':'not_sampled','sample_id':None,'messages':None,'bytes':None,'rate_hz':None,
                'bytes_per_second':None,'last_message_age_ms':None,'history':[],'reason':'No sampling requested'}
            if not sample:return empty
            if 'start' not in sample:return {**empty,**sample}
            elapsed=max(0.,min(self.clock(),sample['end'])-sample['start'])
            return {'state':sample['state'],'sample_id':sample['sample_id'],'started_at_ns':sample['started_at_ns'],
                'ended_at_ns':sample['ended_at_ns'],'messages':sample['messages'],'bytes':sample['bytes'],
                'rate_hz':sample['messages']/elapsed if elapsed else None,'bytes_per_second':sample['bytes']/elapsed if elapsed else None,
                'last_message_age_ms':max(0.,(self.clock()-sample['last'])*1000) if sample['last'] is not None else None,
                'history':copy.deepcopy(list(sample['buckets'].values())),
                'measurement_scope':'local_observer_subscription','payload_export':False,'reason':sample['reason'],
                'cleanup_error':self.cleanup_errors.get(topic)}

    def close(self):
        with self.request_lock:
            self.closed=True
            with self.lock:
                for sample in self.samples.values():self._finish(sample,'Observer stopped')
            self.expire()


class GraphObserver:
    def __init__(self,node,config=None,action_api=None,sampler=None,clock=time.monotonic,wall=time.time_ns,
                 activity_provider=None,watcher=None):
        self.node=node;self.config=config or {};self.clock=clock;self.wall=wall;self.lock=threading.RLock()
        self.activity_clock_id=uuid.uuid4().hex
        self.sampler=sampler or TrafficSampler(node,self.config.get('sampling',{}),clock=clock,wall=wall)
        self.watcher=watcher or GraphWatch(node,self.config.get('watch',{}),activity_provider=activity_provider,clock=clock,wall=wall)
        if action_api is None:
            try:from rclpy.action import graph as action_api
            except ImportError:action_api=False
        self.action_api=action_api;self.loop_errors={};self.stop=threading.Event();self.thread=None;self.sequence=0;self.current=None
        self.refresh_s=max(.2,min(30.,float(self.config.get('refresh_s',2))))

    def start(self):
        self.thread=threading.Thread(target=self._run,name='ros-graph-observer',daemon=True);self.thread.start()

    def _run(self):
        next_refresh=0.
        while not self.stop.is_set():
            if self.clock()>=next_refresh:
                try:
                    self.refresh()
                    with self.lock:self.loop_errors.pop('refresh',None)
                except Exception as exc:self._loop_error('refresh',exc)
                next_refresh=self.clock()+self.refresh_s
            try:
                self.sampler.expire()
                with self.lock:self.loop_errors.pop('cleanup',None)
            except Exception as exc:self._loop_error('cleanup',exc)
            try:
                self.watcher.reconcile()
                with self.lock:self.loop_errors.pop('watch',None)
            except Exception as exc:self._loop_error('watch',exc)
            self.stop.wait(.1)

    def _loop_error(self,method,error):
        with self.lock:self.loop_errors[method]={'section':'observer','method':method,'error':type(error).__name__,'message':str(error)[:256]}

    def refresh(self):
        coverage=dict.fromkeys(('nodes','topics','services','actions'),'complete');errors=[];nodes={};topics=[];services={};actions={};edges=[]
        def read(section,target,method,*args):
            try:return getattr(target,method)(*args)
            except Exception as exc:
                coverage[section]='partial';errors.append({'section':section,'method':method,'error':type(exc).__name__,'message':str(exc)[:256]});return []
        counts=Counter((name,namespace) for name,namespace in read('nodes',self.node,'get_node_names_and_namespaces'))
        for (name,namespace),count in counts.items():
            item=node_record(name,namespace,count);nodes[item['id']]=item
        for name,types in read('topics',self.node,'get_topic_names_and_types'):
            topic={'id':'topic:'+name,'name':name,'types':sorted(set(types)),'publishers':[],'subscriptions':[]}
            for method,key,kind in (('get_publishers_info_by_topic','publishers','publish'),('get_subscriptions_info_by_topic','subscriptions','subscribe')):
                for index,info in enumerate(read('topics',self.node,method,name)):
                    try:gid=bytes(info.endpoint_gid).hex() or None
                    except (TypeError,AttributeError,ValueError):gid=None
                    if info.node_namespace.startswith('/') and info.node_name not in ('','_NODE_NAME_UNKNOWN_'):
                        node=node_record(info.node_name,info.node_namespace,None)
                    else:
                        owner_key=gid or name+':'+kind+':'+str(index)
                        node={'id':'node:unresolved:'+owner_key,'name':info.node_name or None,'namespace':None,
                            'fully_qualified_name':None,'instance_count':None,'discovery_state':'unresolved_endpoint_owner'}
                        coverage['topics']='partial'
                        errors.append({'section':'topics','method':method,'error':'UnknownEndpointOwner','message':'Endpoint owner is not identified by the ROS graph API','topic':name,'gid':gid})
                    nodes.setdefault(node['id'],node)
                    endpoint_id=('publisher:' if kind=='publish' else 'subscription:')+(gid or name+':'+node['id']+':'+str(index))
                    topic[key].append({'id':endpoint_id,'node_id':node['id'],'type':info.topic_type,'gid':gid,'qos':qos_dict(getattr(info,'qos_profile',None))})
                    edges.append({'id':kind+':'+endpoint_id,'kind':kind,'from':node['id'] if kind=='publish' else topic['id'],
                        'to':topic['id'] if kind=='publish' else node['id'],'endpoint_id':endpoint_id})
            topics.append(topic)
        def resource(items,section,prefix):
            for name,types in items:
                entry=section.setdefault(name,{'id':prefix+':'+name,'name':name,'types':[],'servers':[],'clients':[]})
                entry['types']=sorted(set(entry['types'])|set(types))
        resource(read('services',self.node,'get_service_names_and_types'),services,'service')
        if self.action_api:resource(read('actions',self.action_api,'get_action_names_and_types',self.node),actions,'action')
        else:coverage['actions']='partial';errors.append({'section':'actions','method':'action_graph','error':'UnsupportedAPI','message':'Action graph API unavailable'})
        # Query each unique discovered FQN, including nodes appearing through endpoints.
        for node in list(nodes.values()):
            if node.get('discovery_state')=='unresolved_endpoint_owner':continue
            for method,role in (('get_service_names_and_types_by_node','servers'),('get_client_names_and_types_by_node','clients')):
                items=read('services',self.node,method,node['name'],node['namespace']);resource(items,services,'service')
                for name,_ in items:
                    if node['id'] not in services[name][role]:services[name][role].append(node['id'])
            if self.action_api:
                for method,role in (('get_action_server_names_and_types_by_node','servers'),('get_action_client_names_and_types_by_node','clients')):
                    items=read('actions',self.action_api,method,self.node,node['name'],node['namespace']);resource(items,actions,'action')
                    for name,_ in items:
                        if node['id'] not in actions[name][role]:actions[name][role].append(node['id'])
        for section,prefix in ((services,'service'),(actions,'action')):
            for entry in section.values():
                for role,kind in (('clients',prefix+'_client'),('servers',prefix+'_server')):
                    for node_id in entry[role]:edges.append({'id':kind+':'+node_id+':'+entry['id'],'kind':kind,
                        'from':node_id if role=='clients' else entry['id'],'to':entry['id'] if role=='clients' else node_id})
        with self.lock:
            self.sequence+=1;self.current={'schema_version':1,'sequence':self.sequence,'captured_at_ns':self.wall(),
                'state':'ready' if all(v=='complete' for v in coverage.values()) else 'partial',
                'scope':'observer_visible_ros_domain','consistency':'best_effort_discovery',
                'nodes':sorted(nodes.values(),key=lambda n:n['id']),'topics':sorted(topics,key=lambda t:t['name']),
                'services':sorted(services.values(),key=lambda s:s['name']),'actions':sorted(actions.values(),key=lambda a:a['name']),
                'edges':edges,'coverage':coverage,'errors':errors}
        self.watcher.discover(topics)

    def snapshot(self):
        with self.lock:
            result=copy.deepcopy(self.current) if self.current is not None else {'schema_version':1,'sequence':0,'captured_at_ns':None,
                'state':'initializing','nodes':[],'topics':[],'services':[],'actions':[],'edges':[],'errors':[],
                'scope':'observer_visible_ros_domain','consistency':'best_effort_discovery','coverage':dict.fromkeys(('nodes','topics','services','actions'),'partial')}
        result['age_ms']=max(0.,(self.wall()-result['captured_at_ns'])/1e6) if result['captured_at_ns'] else None
        if result['age_ms'] is not None and result['age_ms']>max(5000.,self.refresh_s*3000):result['state']='stale'
        result['sampling']=self.sampler.status()
        result['watch'],activity=self.watcher.snapshot()
        # Capture adjacent to counter collection, before response assembly and
        # transport/render delay. A string preserves the integer across JSON/JS.
        result.update(activity_clock_id=self.activity_clock_id,
            activity_clock_type='observer_monotonic',
            activity_captured_at_ns=str(int(self.clock()*1_000_000_000)))
        activity=project_transport_activity(result['topics'],activity)
        projected=sum(value.get('source')=='transport_gid_projection' for value in activity.values())
        result['watch']['projected_topics']=projected
        if projected:
            result['watch']['direct_coverage']=dict(result['watch'].get('coverage',{}))
            result['watch']['coverage']=dict(Counter(activity.get(topic['name'],{}).get('state','unavailable') for topic in result['topics']))
        with self.lock:loop_errors=list(self.loop_errors.values())
        if loop_errors:
            result['errors'].extend(loop_errors)
            if result['state']!='stale':result['state']='partial'
        for topic in result['topics']:
            topic['traffic']=self.sampler.snapshot(topic['name'])
            topic['activity']=activity.get(topic['name'],empty_activity('unavailable','Waiting for discovery reconciliation'))
        for action in result['actions']:action['activity']=action_activity(action['name'],activity)
        return result

    def watch(self,request):
        return self.watcher.request(request)

    def sample(self,request):
        topic=request.get('topic')
        if not isinstance(topic,str) or not topic.startswith('/') or len(topic)>256:raise ValueError('A fully qualified topic name is required')
        with self.lock:types=next((t['types'] for t in (self.current or {}).get('topics',[]) if t['name']==topic),None)
        if types is None:raise ValueError('Topic is not currently discovered')
        stop=request.get('stop',False)
        if not isinstance(stop,bool):raise ValueError('stop must be boolean')
        return self.sampler.request(topic,types,request.get('duration_s',5.),stop=stop)

    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=5)
        self.sampler.close()
        self.watcher.close()
