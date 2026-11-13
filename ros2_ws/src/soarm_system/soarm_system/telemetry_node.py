import json
import os
import ssl
import threading
import time
import urllib.request
from uuid import uuid4
from rclpy.callback_groups import ReentrantCallbackGroup
from sensor_msgs.msg import JointState
from soarm_interfaces.msg import TelemetrySample,UploadStatus,Event,OperationStatus
from soarm_interfaces.srv import QueryHistory,RecordEvent
from soarm.journal import Journal
from .common import BaseNode,STATE,SENSOR,run,stamp_ns,topic

def to_msg(s):
    m=TelemetrySample();m.schema_version=1
    for k in ('device_id','runtime_boot_id','sequence','monitoring_run_id','arm_id','valid','source_age_ms','phase','reason'):
        setattr(m,k,s[k])
    stamp_ns(m.scheduled_time,s['scheduled_ns']);stamp_ns(m.measured_joints.header.stamp,s.get('source_ns',0))
    m.measured_joints.name=list(s['positions']);m.measured_joints.position=list(s['positions'].values())
    m.voltage_v=float(s.get('voltage_v',0));m.temperature_c=float(s.get('temperature_c',0));m.measured_torque=s.get('measured_torque','UNKNOWN')
    return m
class TelemetryNode(BaseNode):
    def __init__(self):
        super().__init__('telemetry_node');self.cache={};self.states={};self.health={};self.error='' if self.config.get('collector',{}).get('url') else 'Collector endpoint not configured';self.stop=threading.Event()
        self.journal=Journal(self.config['database'],self.config.get('monitoring',{}).get('storage_limit_mb',2048))
        self.run_id=self.journal.meta('monitoring_run_id') or uuid4().hex;self.journal.set_meta('monitoring_run_id',self.run_id)
        self.boot=uuid4().hex;self.period_ns=int(self.config.get('monitoring',{}).get('period_s',1)*1e9)
        self.next_ns=self.journal.meta('next_ns',time.time_ns());self.journal.set_meta('start_ns',self.journal.meta('start_ns',self.next_ns))
        self.pub=self.create_publisher(TelemetrySample,'/soarm/telemetry/sample',STATE)
        self.upload_pub=self.create_publisher(UploadStatus,'/soarm/telemetry/upload_status',STATE)
        self.event_pub=self.create_publisher(Event,'/soarm/events',STATE)
        for a in self.config['arms']:
            self.create_subscription(JointState,topic(a['id'],'joint_states'),lambda m,n=a['id']:self.cache.__setitem__(n,(m,time.monotonic())),SENSOR)
            self.create_subscription(OperationStatus,topic(a['id'],'lerobot/status'),lambda m,n=a['id']:self.states.__setitem__(n,m),STATE)
        from diagnostic_msgs.msg import DiagnosticArray
        self.create_subscription(DiagnosticArray,'/soarm/health',self.on_health,STATE)
        self.create_service(RecordEvent,'/soarm/telemetry/record_event',self.record_event)
        self.create_service(QueryHistory,'/soarm/telemetry/query_history',self.history)
        self.create_timer(.1,self.sample);self.create_timer(1.,self.publish_upload)
        self.uploader=threading.Thread(target=self.upload,daemon=True);self.uploader.start()
    def on_health(self,m):
        for d in m.status:self.health[d.hardware_id]={x.key:x.value for x in d.values}
    def sample(self):
        wall=time.time_ns();count=0
        while self.next_ns<=wall and count<1000:
            samples=[]
            for arm in self.config['arms']:
                name=arm['id'];cached=self.cache.get(name);state=self.states.get(name);health=self.health.get(name,{})
                source_ns=(cached[0].header.stamp.sec*10**9+cached[0].header.stamp.nanosec) if cached else 0
                age=max(0,(wall-source_ns)//10**6) if source_ns else 2**32-1
                stale=self.config.get('monitoring',{}).get('stale_ms',500)
                # Historical schedule gaps cannot be filled with a current observation.
                valid=bool(cached and age<=stale and abs(source_ns-self.next_ns)<self.period_ns and state and state.feedback_valid)
                sample={'schema_version':1,'device_id':self.config['device_id'],'runtime_boot_id':state.runtime_boot_id if state else self.boot,
                    'monitoring_run_id':self.run_id,'scheduled_ns':self.next_ns,'arm_id':name,
                    'positions':dict(zip(cached[0].name,cached[0].position)) if cached and valid else {},
                    'source_ns':source_ns if valid else 0,'source_age_ms':min(2**32-1,int(age)), 'valid':valid,
                    'voltage_v':float(health.get('voltage_v',0)),'temperature_c':float(health.get('temperature_c',0)),
                    'phase':state.phase if state else 'UNAVAILABLE','measured_torque':state.measured_torque if state else 'UNKNOWN',
                    'reason':'' if valid else 'MISSING_STALE_OR_SCHEDULE_GAP'}
                samples.append(sample)
            try:
                committed=self.journal.append(samples,self.next_ns+self.period_ns)
                for s in committed:self.pub.publish(to_msg(s))
                self.next_ns+=self.period_ns;count+=1
            except Exception as exc:self.error=str(exc);break
    def record_event(self,request,response):
        from rosidl_runtime_py.convert import message_to_ordereddict
        try:
            e=self.journal.event(dict(message_to_ordereddict(request.event)))
            response.committed=True;response.event_id=e['event_id'];self.event_pub.publish(request.event)
        except Exception as exc:response.reason=str(exc)[:512]
        return response
    def history(self,request,response):
        from soarm_interfaces.msg import HistoryRecord
        start=request.start_time.sec*10**9+request.start_time.nanosec
        end=request.end_time.sec*10**9+request.end_time.nanosec or 9223372036854775807
        rows,truncated=self.journal.history(request.arm_id,request.after_sequence,request.page_size or 100,start,end)
        response.records=[HistoryRecord(sample=to_msg(x['sample']),remote_committed=x['remote_committed']) for x in rows]
        response.next_sequence=rows[-1]['sample']['sequence'] if rows else request.after_sequence;response.truncated=truncated
        from rosidl_runtime_py.set_message import set_message_fields
        for e in self.journal.events():
            m=Event();set_message_fields(m,e);response.events.append(m)
        return response
    def upload(self):
        c=self.config.get('collector',{});url=c.get('url','')
        if not url:return
        context=ssl.create_default_context(cafile=c.get('ca') or None)
        while not self.stop.wait(1.):
            pending=self.journal.pending()
            if not pending:continue
            try:
                token=os.environ[c.get('token_env','SOARM_COLLECTOR_TOKEN')]
                request=urllib.request.Request(url.rstrip('/')+'/v1/samples',data=json.dumps({'samples':pending}).encode(),
                    headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
                with urllib.request.urlopen(request,timeout=3,context=context) as r:acks=json.load(r)['committed']
                expected={(x['device_id'],x['runtime_boot_id'],x['sequence']) for x in pending}
                if any((a['device_id'],a['runtime_boot_id'],a['sequence']) not in expected for a in acks):raise ValueError('Unexpected ACK key')
                self.journal.acknowledge(acks);self.error=''
            except Exception as exc:self.error=str(exc)
    def publish_upload(self):
        m=UploadStatus();m.header.stamp=self.get_clock().now().to_msg()
        for k,v in self.journal.counts().items():setattr(m,k,v)
        m.error=self.error[:512];self.upload_pub.publish(m)
    def destroy_node(self):
        self.stop.set();self.uploader.join(timeout=4);self.journal.close();super().destroy_node()
def main():run(TelemetryNode)
