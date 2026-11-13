import threading
import time
import uuid
import numpy as np
import cv2
from rclpy.action import ActionServer,ActionClient,CancelResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from sensor_msgs.msg import JointState,CompressedImage
from soarm_station_interfaces.msg import WorkflowStatus,WorkflowDecision,OperationStatus,Event
from soarm_station_interfaces.action import ExecuteWorkflow,ExecuteOperation
from soarm_station_interfaces.srv import RecordEvent,Hold
from .common import BaseNode,STATE,SENSOR,run,topic
from .conversion import goal_from_dict

def wait(future,timeout=5):
    deadline=time.monotonic()+timeout
    while not future.done() and time.monotonic()<deadline:time.sleep(.01)
    if not future.done():raise TimeoutError('ROS response timed out')
    return future.result()
class WorkflowNode(BaseNode):
    def __init__(self):
        super().__init__('workflow_manager');self.joints={};self.states={};self.frames={};self.references={};self.pending={};self.alerts={};self.boot=uuid.uuid4().hex;self.event_pending={};self.claimed=set();self.claim_lock=threading.Lock()
        self.status_pub=self.create_publisher(WorkflowStatus,'/soarm_station/workflow/status',STATE)
        self.decision_pub=self.create_publisher(WorkflowDecision,'/soarm_station/workflow/decision',STATE)
        group=ReentrantCallbackGroup();self.event_client=self.create_client(RecordEvent,'/soarm_station/telemetry/record_event',callback_group=group)
        self.hold_client=self.create_client(Hold,'/soarm_station/lerobot/hold',callback_group=group)
        self.operation=ActionClient(self,ExecuteOperation,'/soarm_station/lerobot/operate',callback_group=group)
        for a in self.config['arms']:
            n=a['id'];self.create_subscription(JointState,topic(n,'joint_states'),lambda m,n=n:self.joints.__setitem__(n,(m,time.monotonic())),SENSOR)
            self.create_subscription(OperationStatus,topic(n,'lerobot/status'),lambda m,n=n:self.states.__setitem__(n,m),STATE)
        for c in self.config['cameras']:
            self.create_subscription(CompressedImage,'/soarm_station/camera/'+c['id']+'/image_raw/compressed',lambda m,n=c['id']:self.frames.__setitem__(n,(m,time.monotonic())),SENSOR)
        self.create_timer(.1,self.rules)
        self.action=ActionServer(self,ExecuteWorkflow,'/soarm_station/workflows/execute',execute_callback=self.execute,
            cancel_callback=lambda _:CancelResponse.ACCEPT,callback_group=group)
    def emit(self,arm,code,detail):
        e=Event();e.event_id=uuid.uuid4().hex;e.source_role='R02';e.source_boot_id=self.boot;e.arm_id=arm;e.time=self.get_clock().now().to_msg()
        e.severity='WARNING';e.code=code;e.detail=detail[:512]
        d=WorkflowDecision();d.header.stamp=e.time;d.arm_id=arm;d.decision_code=code;d.reason=e.detail;self.decision_pub.publish(d)
        self.event_pending[e.event_id]={'event':e,'future':None,'retry':0.}
    def rules(self):
        t=time.monotonic();cfg=self.config.get('monitoring',{})
        for identifier,entry in list(self.event_pending.items()):
            f=entry['future']
            if f and f.done():
                try:committed=f.result().committed
                except Exception:committed=False
                if committed:self.event_pending.pop(identifier,None);continue
                entry['future']=None;entry['retry']=t+1
            if entry['future'] is None and t>=entry['retry'] and self.event_client.service_is_ready():
                entry['future']=self.event_client.call_async(RecordEvent.Request(event=entry['event']))
        for a in self.config['arms']:
            n=a['id'];cached=self.joints.get(n);state=self.states.get(n)
            stale=not cached or t-cached[1]>cfg.get('stale_ms',500)/1000 or not state or not state.feedback_valid
            condition='FEEDBACK_UNAVAILABLE' if stale else ''
            if not stale:
                q=np.asarray(cached[0].position)
                if state.phase in ('ACTIVE','READY','PREPARING') or n not in self.references:self.references[n]=q
                elif np.max(np.abs(q-self.references[n]))>cfg.get('drift_rad',.15):condition='UNEXPECTED_POSE_CHANGE'
            if condition:
                key=(n,condition);self.pending.setdefault(key,t)
                if t-self.pending[key]>=cfg.get('debounce_s',1) and key not in self.alerts:
                    self.alerts[key]=t;self.emit(n,condition,'Fresh encoder deviation exceeded threshold' if not stale else 'Measured feedback is unavailable or stale')
            else:
                for key in list(self.pending):
                    if key[0]==n:self.pending.pop(key,None);self.alerts.pop(key,None)
    def classify(self,camera,regions=None):
        m,received=self.frames[camera]
        source=m.header.stamp.sec+m.header.stamp.nanosec/1e9
        if time.monotonic()-received>.5 or time.time()-source>.5:raise ValueError('Fresh camera observation unavailable')
        image=cv2.imdecode(np.frombuffer(m.data,dtype=np.uint8),cv2.IMREAD_COLOR)
        if regions:
            x,y,w,h=regions;image=image[y:y+h,x:x+w]
        if not image.size:raise ValueError('Empty configured image region')
        hsv=cv2.cvtColor(image,cv2.COLOR_BGR2HSV)
        masks={'red':cv2.inRange(hsv,np.array([0,90,50]),np.array([12,255,255]))|cv2.inRange(hsv,np.array([168,90,50]),np.array([179,255,255])),
               'green':cv2.inRange(hsv,np.array([35,80,50]),np.array([85,255,255])),
               'blue':cv2.inRange(hsv,np.array([90,80,50]),np.array([130,255,255]))}
        coverage={n:float(np.count_nonzero(v))/v.size for n,v in masks.items()};label=max(coverage,key=coverage.get)
        return label,coverage[label],m.header.stamp
    def execute(self,handle):
        g=handle.request;arm=g.context.arm_id;result=ExecuteWorkflow.Result();result.outcome='UNKNOWN';claimed=False;operation_handle=None
        run_id=uuid.uuid4().hex
        def progress(phase,p):
            s=WorkflowStatus();s.run_id=run_id;s.arm_id=arm;s.definition_id=g.definition_id;s.phase=phase;s.progress=float(p);s.header.stamp=self.get_clock().now().to_msg()
            self.status_pub.publish(s);handle.publish_feedback(ExecuteWorkflow.Feedback(status=s))
        try:
            recipes=self.config.get('workflows',{})
            if g.definition_id not in recipes:raise ValueError('Unknown registered workflow')
            recipe=recipes[g.definition_id]
            if recipe['arm_id']!=arm:raise ValueError('Workflow resource mismatch')
            with self.claim_lock:
                if arm in self.claimed:raise ValueError('BUSY')
                self.claimed.add(arm);claimed=True
            progress('OBSERVING',.1)
            label,score,stamp=self.classify(recipe['camera_id'],recipe.get('pickup_region'))
            if score<recipe.get('min_coverage',.03):raise ValueError('No supported object in pickup cell')
            if label not in recipe['routes']:raise ValueError('No registered route for detected class')
            route=recipe['routes'][label];before_label,before_score,_=self.classify(recipe['camera_id'],route['destination_region']);state=self.states[arm]
            progress('DECIDING',.2)
            decision=WorkflowDecision();decision.run_id=run_id;decision.arm_id=arm;decision.decision_code='SORT_'+label.upper();decision.reason=f'Color coverage {score:.3f}';decision.header.stamp=stamp;self.decision_pub.publish(decision)
            d={'arm_id':arm,'runtime_boot_id':state.runtime_boot_id,'controller_session':state.controller_session,
               'operation_type':'DIRECT','mode':'INLINE','duration_s':route.get('duration_s',30),'points':route['points']}
            operation_handle=wait(self.operation.send_goal_async(goal_from_dict(d,g.context.operator_session_id)))
            if not operation_handle.accepted:raise RuntimeError('Operation rejected')
            future=operation_handle.get_result_async();progress('EXECUTING',.3);deadline=time.monotonic()+d['duration_s']+5
            while not future.done():
                if handle.is_cancel_requested or time.monotonic()>deadline:
                    operation_handle.cancel_goal_async();self.hold_client.call_async(Hold.Request(arm_id=arm));raise RuntimeError('Workflow canceled or timed out')
                time.sleep(.05)
            r=future.result().result
            if not r.execution_completed:raise RuntimeError(r.reason)
            progress('VERIFYING',.9);time.sleep(.3)
            target_label,target_score,_=self.classify(recipe['camera_id'],route['destination_region'])
            pickup_label,pickup_score,_=self.classify(recipe['camera_id'],recipe.get('pickup_region'))
            delta=recipe.get('verification_delta',.02)
            if target_label==label and target_score>=max(recipe.get('min_coverage',.03),before_score+delta if before_label==label else 0) and (pickup_label!=label or pickup_score<score-delta):
                result.outcome='VERIFIED_SUCCESS';result.evidence_id=uuid.uuid4().hex;handle.succeed();progress('COMPLETED',1.)
            else:result.outcome='NEEDS_REVIEW';result.reason='Trajectory completed; fresh destination observation did not verify placement';handle.abort()
            return result
        except Exception as exc:
            result.reason=str(exc)[:512]
            if handle.is_cancel_requested:handle.canceled();result.outcome='CANCELED'
            else:handle.abort();result.outcome='FAILED'
            return result
        finally:
            if claimed:
                with self.claim_lock:self.claimed.discard(arm)
    def destroy_node(self):self.action.destroy();super().destroy_node()
def main():run(WorkflowNode)
