import asyncio
import hmac
import os
import ssl
import threading
import time
import uuid
from pathlib import Path
from aiohttp import web
from rclpy.action import ActionClient
from rclpy.callback_groups import ReentrantCallbackGroup
from rosidl_runtime_py.convert import message_to_ordereddict
from sensor_msgs.msg import JointState,CompressedImage
from diagnostic_msgs.msg import DiagnosticArray
from soarm_station_interfaces.msg import OperationStatus,OperationInput,Event,UploadStatus,WorkflowStatus,WorkflowDecision
from soarm_station_interfaces.srv import SetControlPermission,Hold,QueryHistory
from soarm_station_interfaces.action import ExecuteOperation,ExecuteWorkflow
from .conversion import goal_from_dict,stream_from_dict
from .common import BaseNode,STATE,SENSOR,run,topic

def asdict(m):return dict(message_to_ordereddict(m))
async def resolved(f,timeout=5):
    deadline=time.monotonic()+timeout
    while not f.done():
        if time.monotonic()>deadline:raise TimeoutError('ROS response timed out')
        await asyncio.sleep(.005)
    return f.result()
class DashboardNode(BaseNode):
    def __init__(self):
        super().__init__('dashboard_node');self.states={};self.inference={};self.joints={};self.images={};self.events=[];self.upload={};self.workflows={};self.decisions=[];self.goals={};self.stopping=False
        group=ReentrantCallbackGroup()
        self.permission_client=self.create_client(SetControlPermission,'/soarm_station/lerobot/set_permission',callback_group=group)
        self.hold_client=self.create_client(Hold,'/soarm_station/lerobot/hold',callback_group=group)
        self.history_client=self.create_client(QueryHistory,'/soarm_station/telemetry/query_history',callback_group=group)
        self.operation=ActionClient(self,ExecuteOperation,'/soarm_station/lerobot/operate',callback_group=group)
        self.workflow=ActionClient(self,ExecuteWorkflow,'/soarm_station/workflows/execute',callback_group=group)
        self.command=self.create_publisher(OperationInput,'/soarm_station/lerobot/command_stream',SENSOR)
        for a in self.config['arms']:
            n=a['id'];self.create_subscription(JointState,topic(n,'joint_states'),lambda m,n=n:self.joints.__setitem__(n,asdict(m)),SENSOR)
            self.create_subscription(OperationStatus,topic(n,'lerobot/status'),lambda m,n=n:self.states.__setitem__(n,asdict(m)),STATE)
            self.create_subscription(DiagnosticArray,topic(n,'lerobot/inference_status'),lambda m,n=n:self.inference.__setitem__(n,asdict(m)),STATE)
        for c in self.config['cameras']:
            self.create_subscription(CompressedImage,'/soarm_station/camera/'+c['id']+'/image_raw/compressed',lambda m,n=c['id']:self.images.__setitem__(n,(bytes(m.data),m.header.stamp.sec*10**9+m.header.stamp.nanosec)),SENSOR)
        self.create_subscription(Event,'/soarm_station/events',self.event,STATE)
        self.create_subscription(UploadStatus,'/soarm_station/telemetry/upload_status',lambda m:setattr(self,'upload',asdict(m)),STATE)
        self.create_subscription(WorkflowDecision,'/soarm_station/workflow/decision',lambda m:self.decisions.append(asdict(m)) or self.decisions.__delitem__(slice(None,-50)),STATE)
        self.create_subscription(WorkflowStatus,'/soarm_station/workflow/status',lambda m:self.workflows.__setitem__(m.arm_id,asdict(m)),STATE)
        self.loop=asyncio.new_event_loop();self.thread=threading.Thread(target=self.serve,daemon=True);self.thread.start()
    def event(self,m):self.events.append(asdict(m));self.events=self.events[-100:]
    def serve(self):
        asyncio.set_event_loop(self.loop)
        try:self.loop.run_until_complete(self.start_web());self.loop.run_forever()
        except Exception as exc:self.get_logger().error('Dashboard failed: '+str(exc))
        finally:
            if hasattr(self,'runner'):self.loop.run_until_complete(self.runner.cleanup())
            self.loop.close()
    async def start_web(self):
        cfg=self.config['web'];token=os.environ.get(cfg.get('token_env','SOARM_STATION_TOKEN'),'')
        if len(token)<24:raise ValueError('Set an operator token of at least 24 characters')
        @web.middleware
        async def auth(request,handler):
            # Static login page contains no state or credentials. Every API/media route authenticates.
            if request.path=='/':return await handler(request)
            supplied=request.headers.get('Authorization','')
            if request.path.startswith('/ws/') and not supplied:
                protocols=request.headers.get('Sec-WebSocket-Protocol','').split(',')
                supplied='Bearer '+protocols[1].strip() if len(protocols)==2 and protocols[0].strip()=='soarm_station' else ''
                origin=request.headers.get('Origin')
                if origin and origin!='https://'+request.host:raise web.HTTPForbidden(text='Origin mismatch')
            if not hmac.compare_digest(supplied,'Bearer '+token):raise web.HTTPUnauthorized()
            try:return await handler(request)
            except (ValueError,KeyError,TimeoutError) as exc:raise web.HTTPBadRequest(text=str(exc))
        app=web.Application(middlewares=[auth],client_max_size=262144)
        app.router.add_get('/',self.index);app.router.add_get('/api/status',self.status);app.router.add_get('/api/history',self.history)
        app.router.add_post('/api/permission',self.permission);app.router.add_post('/api/hold',self.hold)
        app.router.add_post('/api/stream',self.stream_input);app.router.add_post('/api/operate',self.operate);app.router.add_get('/api/operations/{id}',self.operation_status)
        app.router.add_post('/api/workflows',self.start_workflow);app.router.add_get('/api/cameras/{id}.jpg',self.image)
        app.router.add_get('/ws/control',self.control);app.router.add_get('/ws/camera/{id}',self.camera_stream)
        self.runner=web.AppRunner(app,access_log=None);await self.runner.setup()
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        parent=Path(self.config['_path']).parent
        context.load_cert_chain(parent/cfg['cert'],parent/cfg['key'])
        await web.TCPSite(self.runner,cfg.get('host','0.0.0.0'),cfg.get('port',8443),ssl_context=context).start()
    async def index(self,r):return web.Response(text=(Path(os.environ['SOARM_STATION_ROOT'])/'web/index.html').read_text(),content_type='text/html')
    def state(self):return {'device_id':self.config['device_id'],'arms':dict(self.states),'inference':dict(self.inference),'joints':dict(self.joints),'cameras':list(self.images),'upload':self.upload,'events':list(self.events),'workflows':dict(self.workflows),'decisions':list(self.decisions)}
    async def status(self,r):return web.json_response(self.state())
    async def history(self,r):
        q=QueryHistory.Request();q.arm_id=r.query.get('arm','');q.after_sequence=int(r.query.get('after',0));q.page_size=min(500,int(r.query.get('limit',100)))
        return web.json_response(asdict(await resolved(self.history_client.call_async(q))))
    async def permission(self,r):
        b=await r.json();q=SetControlPermission.Request();q.request_id=uuid.uuid4().hex;q.arm_id=b['arm_id'];q.operator_session_id=b.get('operator_session_id','browser')
        q.expected_controller_session=b['controller_session'];q.enable=bool(b['enable'])
        return web.json_response(asdict(await resolved(self.permission_client.call_async(q))))
    async def hold(self,r):
        b=await r.json();return web.json_response(asdict(await resolved(self.hold_client.call_async(Hold.Request(arm_id=b['arm_id'])))))
    async def submit(self,b,principal,feedback=None):
        g=goal_from_dict(b,principal);latest={}
        def receive_feedback(m):
            latest.clear();latest.update(asdict(m.feedback.status))
            if feedback:feedback(m)
        h=await resolved(self.operation.send_goal_async(g,feedback_callback=receive_feedback))
        if not h.accepted:raise ValueError('Operation rejected')
        request_id=uuid.uuid4().hex;self.goals[request_id]={'handle':h,'future':h.get_result_async(),'feedback':latest}
        if len(self.goals)>128:
            for k in list(self.goals):
                if self.goals[k]['future'].done():self.goals.pop(k)
                if len(self.goals)<=128:break
        return request_id,h
    async def operate(self,r):
        b=await r.json();i,h=await self.submit(b,b.get('operator_session_id','browser'));return web.json_response({'request_id':i})
    async def operation_status(self,r):
        g=self.goals[r.match_info['id']];return web.json_response({'done':g['future'].done(),'result':asdict(g['future'].result().result) if g['future'].done() else None,'feedback':dict(g['feedback'])})
    async def stream_input(self,r):
        b=await r.json();self.command.publish(stream_from_dict(b,b['operator_session_id']));return web.json_response({'forwarded':True})
    async def start_workflow(self,r):
        b=await r.json();g=ExecuteWorkflow.Goal();g.context.schema_version=1;g.context.arm_id=b['arm_id'];g.context.operator_session_id=b.get('operator_session_id','browser');g.definition_id=b['definition_id']
        h=await resolved(self.workflow.send_goal_async(g));return web.json_response({'accepted':h.accepted})
    async def image(self,r):
        n=r.match_info['id']
        if n not in self.images:raise web.HTTPNotFound()
        b,t=self.images[n]
        return web.Response(body=b,content_type='image/jpeg',headers={'X-Source-Time-Ns':str(t),'Cache-Control':'no-store'})
    async def camera_stream(self,r):
        n=r.match_info['id'];ws=web.WebSocketResponse(max_msg_size=1024,heartbeat=10);await ws.prepare(r);last=0
        try:
            while not ws.closed and not self.stopping:
                frame=self.images.get(n)
                if frame and frame[1]!=last:last=frame[1];await ws.send_bytes(frame[0])
                await asyncio.sleep(1/30)
        finally:await ws.close()
        return ws
    async def control(self,r):
        ws=web.WebSocketResponse(protocols=['soarm_station'],max_msg_size=262144,heartbeat=10,receive_timeout=2);await ws.prepare(r)
        arm='';h=None;feedback={};principal=uuid.uuid4().hex
        try:
            b=await ws.receive_json(timeout=5);arm=b['arm_id']
            if b['operation_type']!='DIRECT' or b['mode']!='STREAM':raise ValueError('Remote control uses DIRECT STREAM')
            state=self.states[arm]
            # Opening a socket is not consent to arm: permission must already be granted to this requested operator ID.
            principal=b['operator_session_id']
            def on_feedback(m):feedback.clear();feedback.update(asdict(m.feedback.status))
            i,h=await self.submit(b,principal,on_feedback);result=self.goals[i]['future'];last_feedback=0.;last_state=0.
            while not ws.closed and not self.stopping and not result.done():
                t=time.monotonic()
                if feedback and t-last_feedback>=.05:
                    await ws.send_json({'type':'grant','grant':dict(feedback)});last_feedback=t
                if t-last_state>=.01:
                    await ws.send_json({'type':'state','state':self.states.get(arm),'joints':self.joints.get(arm)});last_state=t
                try:m=await ws.receive(timeout=.005)
                except asyncio.TimeoutError:continue
                if m.type==web.WSMsgType.TEXT:
                    import json
                    d=json.loads(m.data)
                    if d.get('arm_id')!=arm:raise ValueError('Arm mismatch')
                    self.command.publish(stream_from_dict(d,principal))
                elif m.type in (web.WSMsgType.CLOSE,web.WSMsgType.CLOSED,web.WSMsgType.ERROR):break
            if result.done() and not ws.closed:
                await ws.send_json({'type':'result','result':asdict(result.result().result)})
        except Exception as exc:
            if not ws.closed:await ws.send_json({'type':'error','reason':str(exc)[:512]})
        finally:
            if h:h.cancel_goal_async()
            if arm:self.hold_client.call_async(Hold.Request(arm_id=arm))
            await ws.close()
        return ws
    def destroy_node(self):
        self.stopping=True;self.loop.call_soon_threadsafe(self.loop.stop);self.thread.join(timeout=4);super().destroy_node()
def main():run(DashboardNode)
