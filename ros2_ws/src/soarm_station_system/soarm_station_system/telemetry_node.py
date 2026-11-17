import json
import os
import ssl
import threading
import time
import urllib.request
from uuid import uuid4
from rclpy.callback_groups import ReentrantCallbackGroup
from sensor_msgs.msg import JointState
from soarm_station_interfaces.msg import TelemetrySample,UploadStatus,Event,OperationStatus
from soarm_station_interfaces.srv import QueryHistory,RecordEvent
from soarm_station.journal import Journal
from soarm_station.telemetry_storage import (RecoveringJournal,StorageUnavailable,
    configure_monitoring_schedule,resume_monitoring_schedule)
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
        super().__init__('telemetry_node')
        self.cache={};self.states={};self.health={};self.error='';self.storage_error='';self.maintenance_error=''
        self.stop=threading.Event();self.boot=uuid4().hex
        monitoring=self.config.get('monitoring',{})
        self.collector_enabled=bool(self.config.get('collector',{}).get('url'))
        self.period_ns=int(monitoring.get('period_s',10)*1e9)
        self.run_id=self.boot;self.next_ns=time.time_ns()
        self.maintenance_due=0.;self.maintenance_status=None
        self.discarded_slots=0;self.rejected_events=0;self.gap_start_ns=None
        self.pending_storage_events=()
        self.pub=self.create_publisher(TelemetrySample,'/soarm_station/telemetry/sample',STATE)
        self.upload_pub=self.create_publisher(UploadStatus,'/soarm_station/telemetry/upload_status',STATE)
        self.event_pub=self.create_publisher(Event,'/soarm_station/events',STATE)
        self.storage=RecoveringJournal(self.config,self.initialize_journal,factory=Journal)
        self.storage.poll()
        self.publish_storage_events()
        for a in self.config['arms']:
            self.create_subscription(JointState,topic(a['id'],'joint_states'),lambda m,n=a['id']:self.cache.__setitem__(n,(m,time.monotonic())),SENSOR)
            self.create_subscription(OperationStatus,topic(a['id'],'lerobot/status'),lambda m,n=a['id']:self.states.__setitem__(n,m),STATE)
        from diagnostic_msgs.msg import DiagnosticArray
        self.create_subscription(DiagnosticArray,'/soarm_station/health',self.on_health,STATE)
        self.create_service(RecordEvent,'/soarm_station/telemetry/record_event',self.record_event)
        self.create_service(QueryHistory,'/soarm_station/telemetry/query_history',self.history)
        self.create_timer(.1,self.sample);self.create_timer(1.,self.publish_upload)
        self.uploader=threading.Thread(target=self.upload,daemon=True);self.uploader.start()

    def initialize_journal(self,journal):
        wall=time.time_ns()
        self.maintenance_status=journal.maintain(wall)
        self.maintenance_due=time.monotonic()+3600.
        run_id=journal.meta('monitoring_run_id') or uuid4().hex
        journal.set_meta('monitoring_run_id',run_id)
        next_ns,events=resume_monitoring_schedule(journal,self.period_ns,wall,run_id,self.boot,
            gap_start_ns=self.gap_start_ns,rejected_events=self.rejected_events,
            stale_ns=int(self.config.get('monitoring',{}).get('stale_ms',500)*1e6))
        journal.set_meta('start_ns',journal.meta('start_ns',next_ns))
        self.run_id=run_id;self.next_ns=next_ns;self.pending_storage_events=tuple(events)
        # The gap is already durably committed even if a subsequent counts read
        # fails; do not record the same interval again on another reopen.
        self.gap_start_ns=None;self.rejected_events=0

    def publish_storage_events(self):
        if not self.storage.status()['available']:return
        from rosidl_runtime_py.set_message import set_message_fields
        events=self.pending_storage_events;self.pending_storage_events=()
        self.gap_start_ns=None;self.rejected_events=0;self.storage_error='';self.maintenance_error=''
        for event in events:
            message=Event();set_message_fields(message,event);self.event_pub.publish(message)

    def storage_message(self):
        status=self.storage.status()
        if status['available']:return ''
        return ('TELEMETRY_STORAGE_'+status['state'].upper()+': '+status['reason'])[:512]

    def discard_due_slots(self,wall):
        if self.gap_start_ns is None:self.gap_start_ns=self.next_ns
        if self.next_ns<=wall:
            count=(wall-self.next_ns)//self.period_ns+1
            self.discarded_slots+=count*len(self.config['arms'])
            self.next_ns+=count*self.period_ns
        self.storage_error=self.storage_message()

    def on_health(self,m):
        names={arm['id'] for arm in self.config['arms']}
        for d in m.status:
            if d.hardware_id in names and d.name==d.hardware_id:self.health[d.hardware_id]={x.key:x.value for x in d.values}

    def sample(self):
        wall=time.time_ns()
        if not self.storage.poll():self.discard_due_slots(wall);return
        self.publish_storage_events()
        self.maintain_if_due(wall)
        # No unbounded catch-up loop. Recovery records the missing interval as
        # one durable event and resumes the configured cadence.
        if self.next_ns>wall:return
        samples=[]
        for arm in self.config['arms']:
            name=arm['id'];cached=self.cache.get(name);state=self.states.get(name);health=self.health.get(name,{})
            source_ns=(cached[0].header.stamp.sec*10**9+cached[0].header.stamp.nanosec) if cached else 0
            age=max(0,(wall-source_ns)//10**6) if source_ns else 2**32-1
            stale=self.config.get('monitoring',{}).get('stale_ms',500)
            valid=bool(cached and age<=stale and abs(source_ns-self.next_ns)<=min(self.period_ns,stale*10**6) and state and state.feedback_valid)
            samples.append({'schema_version':1,'device_id':self.config['device_id'],'runtime_boot_id':state.runtime_boot_id if state else self.boot,
                'monitoring_run_id':self.run_id,'scheduled_ns':self.next_ns,'arm_id':name,
                'positions':dict(zip(cached[0].name,cached[0].position)) if cached and valid else {},
                'source_ns':source_ns if valid else 0,'source_age_ms':min(2**32-1,int(age)),'valid':valid,
                'voltage_v':float(health.get('voltage_v',0)),'temperature_c':float(health.get('temperature_c',0)),
                'phase':state.phase if state else 'UNAVAILABLE','measured_torque':state.measured_torque if state else 'UNKNOWN',
                'reason':'' if valid else 'MISSING_STALE_OR_SCHEDULE_GAP'})
        try:committed=self.storage.call('append',samples,self.next_ns+self.period_ns)
        except StorageUnavailable:self.discard_due_slots(wall);return
        self.next_ns+=self.period_ns;self.storage_error=''
        for sample in committed:self.pub.publish(to_msg(sample))

    def maintain_if_due(self,wall_ns):
        monotonic=time.monotonic()
        if monotonic<self.maintenance_due:return
        self.maintenance_due=monotonic+3600.
        try:
            self.maintenance_status=self.storage.call('maintain',wall_ns);self.maintenance_error=''
        except StorageUnavailable as exc:self.maintenance_error='Telemetry maintenance unavailable: '+str(exc)

    def record_event(self,request,response):
        from rosidl_runtime_py.convert import message_to_ordereddict
        response.committed=False;response.event_id=request.event.event_id
        try:
            event=self.storage.call('event',dict(message_to_ordereddict(request.event)))
            response.committed=True;response.event_id=event['event_id'];self.event_pub.publish(request.event)
        except StorageUnavailable:
            self.rejected_events+=1;response.reason=self.storage_message()
        except Exception as exc:response.reason=str(exc)[:512]
        return response

    def history(self,request,response):
        from soarm_station_interfaces.msg import HistoryRecord
        response.available=False;response.reason='';response.records=[];response.events=[]
        response.next_sequence=request.after_sequence;response.truncated=False
        start=request.start_time.sec*10**9+request.start_time.nanosec
        end=request.end_time.sec*10**9+request.end_time.nanosec or 9223372036854775807
        try:
            rows,truncated=self.storage.call('history',request.arm_id,request.after_sequence,request.page_size or 100,start,end)
            events=self.storage.call('events')
            response.records=[HistoryRecord(sample=to_msg(x['sample']),remote_committed=x['remote_committed']) for x in rows]
            response.next_sequence=rows[-1]['sample']['sequence'] if rows else request.after_sequence
            response.truncated=truncated
            from rosidl_runtime_py.set_message import set_message_fields
            for event in events:
                message=Event();set_message_fields(message,event);response.events.append(message)
            response.available=True
        except StorageUnavailable:response.reason=self.storage_message()
        except Exception as exc:response.reason=('Telemetry history unavailable: '+str(exc))[:512]
        if not response.available:response.records=[];response.events=[]
        return response

    def upload(self):
        config=self.config.get('collector',{});url=config.get('url','')
        if not url:return
        context=None
        while not self.stop.wait(1.):
            try:
                generation,pending=self.storage.pending()
                if not pending:continue
                if context is None:context=ssl.create_default_context(cafile=config.get('ca') or None)
                token=os.environ[config.get('token_env','SOARM_STATION_COLLECTOR_TOKEN')]
                request=urllib.request.Request(url.rstrip('/')+'/v1/samples',data=json.dumps({'samples':pending}).encode(),
                    headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
                with urllib.request.urlopen(request,timeout=3,context=context) as response:acks=json.load(response)['committed']
                expected={(x['device_id'],x['runtime_boot_id'],x['sequence']) for x in pending}
                if any((a['device_id'],a['runtime_boot_id'],a['sequence']) not in expected for a in acks):raise ValueError('Unexpected ACK key')
                self.storage.call('acknowledge',acks,generation=generation);self.error=''
            except StorageUnavailable:pass
            except Exception as exc:self.error=str(exc)[:512]

    def publish_upload(self):
        message=UploadStatus();message.header.stamp=self.get_clock().now().to_msg()
        try:counts=self.storage.call('counts')
        except StorageUnavailable:counts=self.storage.status()['counts']
        counts=dict(counts)
        if not self.collector_enabled:counts['queued']=0
        for key,value in counts.items():setattr(message,key,value)
        message.error='; '.join(value for value in (self.storage_message(),self.maintenance_error,self.error) if value)[:512]
        self.upload_pub.publish(message)

    def diagnose(self):
        from diagnostic_msgs.msg import DiagnosticArray,DiagnosticStatus,KeyValue
        message=DiagnosticArray();message.header.stamp=self.get_clock().now().to_msg()
        status=self.storage.status() if hasattr(self,'storage') else {'state':'waiting','available':False,'reason':'Telemetry storage is initializing','counts_known':False}
        level=DiagnosticStatus.OK if status['state']=='ready' else DiagnosticStatus.WARN if status['state']=='waiting' else DiagnosticStatus.ERROR
        details={'config_revision':self.config.get('_revision',''),'storage_state':status['state'],
            'storage_available':str(status['available']).lower(),'storage_reason':status['reason'],
            'discarded_slots':getattr(self,'discarded_slots',0),'counts_known':str(status['counts_known']).lower()}
        message.status=[DiagnosticStatus(name='/soarm_station/telemetry_node/storage',
            hardware_id=self.config.get('device_id','telemetry'),level=level,
            message='Telemetry storage '+status['state'],values=[KeyValue(key=key,value=str(value)) for key,value in details.items()])]
        self.diagnostics.publish(message)

    def destroy_node(self):
        self.stop.set();self.uploader.join(timeout=4);self.storage.close();super().destroy_node()

def main():run(TelemetryNode)
