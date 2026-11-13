import copy
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from rclpy.action import ActionServer,GoalResponse,CancelResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from sensor_msgs.msg import JointState,Image,CompressedImage,CameraInfo
from diagnostic_msgs.msg import DiagnosticArray,DiagnosticStatus,KeyValue
from soarm_interfaces.msg import OperationStatus,OperationInput
from soarm_interfaces.action import ExecuteOperation
from soarm_interfaces.srv import SetControlPermission,Hold
from soarm.ipc import Client
from .common import BaseNode,STATE,SENSOR,run,stamp_ns,topic
from .conversion import request_from_goal,frame_to_dict

class RuntimeNode(BaseNode):
    def __init__(self):
        super().__init__('lerobot_node');c=self.config;self.stop=threading.Event();self.snapshot={};self.seq={};self.camera_seq={};self.rejected={}
        self.root=Path(os.environ['SOARM_ROOT']);runtime_python=os.environ.get('SOARM_RUNTIME_PYTHON',str(self.root/'.runtime-venv/bin/python'))
        self.process=subprocess.Popen([runtime_python,'-m','soarm.worker','--config',c['_path']],env={**os.environ,'PYTHONPATH':str(self.root/'python'),'SOARM_PARENT_PID':str(os.getpid())})
        self.client=Client(c['socket']);self.control_client=Client(c['socket'])
        self.joints={};self.status={};self.camera_pubs={}
        self.health=self.create_publisher(DiagnosticArray,'/soarm/health',STATE)
        self.inference={}
        for a in c['arms']:
            n=a['id'];self.joints[n]=self.create_publisher(JointState,topic(n,'joint_states'),SENSOR)
            self.status[n]=self.create_publisher(OperationStatus,topic(n,'lerobot/status'),STATE)
            self.inference[n]=self.create_publisher(DiagnosticArray,topic(n,'lerobot/inference_status'),STATE)
        for camera in c['cameras']:
            prefix='/soarm/camera/'+camera['id']
            self.camera_pubs[camera['id']]=(self.create_publisher(CompressedImage,prefix+'/image_raw/compressed',SENSOR),
                self.create_publisher(CameraInfo,prefix+'/camera_info',STATE),
                self.create_publisher(Image,prefix+'/image_raw',SENSOR))
        group=ReentrantCallbackGroup()
        self.create_service(SetControlPermission,'/soarm/lerobot/set_permission',self.permission,callback_group=group)
        self.create_service(Hold,'/soarm/lerobot/hold',self.hold,callback_group=group)
        self.create_subscription(OperationInput,'/soarm/lerobot/command_stream',self.input,SENSOR,callback_group=group)
        self.action=ActionServer(self,ExecuteOperation,'/soarm/lerobot/operate',execute_callback=self.execute,
            goal_callback=self.goal,cancel_callback=lambda _:CancelResponse.ACCEPT,callback_group=group)
        self.poller=threading.Thread(target=self.poll,daemon=True);self.poller.start()
        self.create_timer(1/c['control_hz'],self.publish_joints);self.create_timer(.02,self.publish_cameras);self.create_timer(.1,self.publish_status)
    def poll(self):
        while not self.stop.is_set():
            start=time.monotonic()
            try:
                self.snapshot=self.client.call('snapshot');self.received_snapshot=time.monotonic()
            except Exception as exc:self.poll_error=str(exc)
            self.stop.wait(max(.001,1/self.config['control_hz']-(time.monotonic()-start)))
    def publish_joints(self):
        for n,s in self.snapshot.get('arms',{}).items():
            if not s['positions'] or self.seq.get(n)==s['sequence']:continue
            self.seq[n]=s['sequence'];m=JointState();stamp_ns(m.header.stamp,s['time_ns']);m.header.frame_id=n+'_base'
            m.name=list(s['positions']);m.position=list(s['positions'].values());self.joints[n].publish(m)
    def publish_status(self):
        health=DiagnosticArray();health.header.stamp=self.get_clock().now().to_msg()
        for n,s in self.snapshot.get('arms',{}).items():
            m=OperationStatus();m.header.stamp=self.get_clock().now().to_msg();m.arm_id=n
            for k in ('runtime_boot_id','controller_session','operation_id','operation_epoch','phase','operation_type','permission'):setattr(m,k,s[k])
            m.measured_torque=s['health'].get('torque','UNKNOWN');m.feedback_age_ms=min(2**32-1,max(0,int(s['age_ms'])))
            m.feedback_valid=bool(s['positions']) and s['age_ms']<150 and time.monotonic()-getattr(self,'received_snapshot',0)<.15;m.sample_sequence=s['sequence'];m.measured_hz=float(s['measured_hz'])
            m.gripper_percent=float(s['gripper']);m.fault=s['error'][:512];self.status[n].publish(m)
            d=DiagnosticStatus();d.name=n;d.hardware_id=n;d.message=s['error'] or s['phase'];d.level=DiagnosticStatus.OK if m.feedback_valid else DiagnosticStatus.ERROR
            d.values=[KeyValue(key=k,value=str(v)) for k,v in {**s['health'],'input_rejected':self.rejected.get(n,0)}.items()];health.status.append(d)
            self.inference[n].publish(DiagnosticArray(header=health.header,status=[DiagnosticStatus(name=n+'/policy',message=s.get('model_state',s['operation_type']),values=[KeyValue(key='reason',value=s.get('model_reason',''))])]))
        self.health.publish(health)
    def publish_cameras(self):
        for c in self.config['cameras']:
            n=c['id'];s=self.snapshot.get('cameras',{}).get(n,{})
            if not s.get('path') or self.camera_seq.get(n)==s.get('sequence') or s.get('age_ms',1e9)>500:continue
            try:data=Path(s['path']).read_bytes()
            except FileNotFoundError:continue
            self.camera_seq[n]=s['sequence'];m=CompressedImage();stamp_ns(m.header.stamp,s['time_ns']);m.header.frame_id=s['frame_id'];m.format='jpeg';m.data=data
            compressed,info_pub,raw=self.camera_pubs[n];compressed.publish(m)
            info=CameraInfo();info.header=m.header;info.width=s['width'];info.height=s['height']
            calibration=c.get('intrinsics',{})
            if calibration:
                info.k=calibration['k'];info.d=calibration.get('d',[]);info.distortion_model=calibration.get('distortion_model','plumb_bob')
                info.r=calibration.get('r',[1.,0.,0.,0.,1.,0.,0.,0.,1.]);info.p=calibration.get('p',[0.]*12)
            # K[0]==0 is standard CameraInfo uncalibrated status; no guessed geometry.
            info_pub.publish(info)
            if raw.get_subscription_count():
                import cv2,numpy as np
                image=cv2.imdecode(np.frombuffer(data,dtype=np.uint8),cv2.IMREAD_COLOR)
                r=Image();r.header=m.header;r.height=image.shape[0];r.width=image.shape[1];r.encoding='bgr8';r.step=r.width*3;r.data=image.tobytes();raw.publish(r)
    def permission(self,req,res):
        try:
            r=self.control_client.call('permission',arm_id=req.arm_id,enable=req.enable,principal=req.operator_session_id,controller_session=req.expected_controller_session)
            res.accepted=r.get('accepted',r.get('success',False));res.permission=req.enable and res.accepted;res.controller_session=req.expected_controller_session
        except Exception as exc:res.reason=str(exc)[:512]
        return res
    def hold(self,req,res):
        try:r=self.control_client.call('hold',arm_id=req.arm_id);res.success=r['success'];res.message=r['message'][:512]
        except Exception as exc:res.message=str(exc)[:512]
        return res
    def input(self,m):
        try:self.control_client.call('input',arm_id=m.arm_id,frame=frame_to_dict(m))
        except Exception:self.rejected[m.arm_id]=self.rejected.get(m.arm_id,0)+1
    def goal(self,request):
        return GoalResponse.ACCEPT if request.context.arm_id in self.status else GoalResponse.REJECT
    def execute(self,handle):
        arm=handle.request.context.arm_id;result=ExecuteOperation.Result();result.arm_id=arm;client=Client(self.config['socket'])
        try:
            request=request_from_goal(handle.request)
            feedback=client.call('begin',arm_id=arm,request=request);operation=feedback['operation_id'];result.operation_id=operation
            while not self.stop.is_set():
                if handle.is_cancel_requested:
                    client.call('hold',arm_id=arm);handle.canceled();result.result_code='CANCELED';return result
                r=client.call('result',arm_id=arm,operation_id=operation)
                if r:
                    for k,v in r.items():setattr(result,k,v)
                    if r['execution_completed']:handle.succeed()
                    else:handle.abort()
                    return result
                fb=client.call('feedback',arm_id=arm);m=ExecuteOperation.Feedback()
                for k,v in fb.items():
                    if hasattr(m.status,k):setattr(m.status,k,max(0,v) if k=='last_sequence' else v)
                handle.publish_feedback(m);time.sleep(.05)
            handle.abort();result.result_code='UNAVAILABLE';return result
        except Exception as exc:
            handle.abort();result.result_code='REJECTED';result.reason=str(exc)[:512];return result
        finally:client.close()
    def destroy_node(self):
        self.stop.set();self.poller.join(timeout=3);self.client.close();self.control_client.close()
        self.process.terminate()
        try:self.process.wait(timeout=6)
        except subprocess.TimeoutExpired:self.process.kill();self.process.wait()
        self.action.destroy();super().destroy_node()
def main():run(RuntimeNode)
