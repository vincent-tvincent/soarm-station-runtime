import asyncio
import copy
import json
import inspect
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
from soarm_station.dashboard_data import resources,render_motors,validate_motor_record
from soarm_station.graph_observer import GraphObserver
from soarm_station.rpc_trace import RpcTraceClient,rpc_trace_socket
from soarm_station.subscription_activity import SubscriptionActivity,observe_subscription
from soarm_station.web_access import dashboard_access_middleware
from soarm_station.web_streams import stream_camera_frames
from soarm_station.manual_http import ManualRoutes
from soarm_station.leader_gateway import LeaderHostRegistry,native_start_state
from soarm_station.policy_manager import PolicyManager
from soarm_station.policy_http import mount_policy_routes
from soarm_station.ipc import Client

ASSET_NAMES=frozenset(('app.js','graph.js','transport.js','styles.css','camera-layout.js','payload-model.js','manual-panel.js','gamepad-panel.js','gamepad-input.js','fk-view.js','policy-management.js','policy-management.css','policy-draft.js','policy-api.js','yaml-vendor.js','LICENSE-yaml.txt'))

def asdict(m):return dict(message_to_ordereddict(m))
def telemetry_view(config,upload):
    """Describe local durable history without opening SQLite or exposing paths."""
    monitoring=config.get('monitoring',{})
    remote=bool(config.get('collector',{}).get('url'))
    storage_bytes=None
    database=config.get('database')
    if database:
        try:
            storage_bytes=Path(database).stat().st_size
            for suffix in ('-wal','-shm'):
                try:storage_bytes+=Path(str(database)+suffix).stat().st_size
                except FileNotFoundError:pass
        except OSError:storage_bytes=None
    value={**upload,'delivery_mode':'remote' if remote else 'local',
        'sample_period_s':monitoring.get('period_s',10),
        'retention_days':monitoring.get('retention_days',30),
        'storage_limit_mb':monitoring.get('storage_limit_mb',512),'storage_bytes':storage_bytes}
    if not remote:
        value['queued']=0
        if value.get('error')=='Collector endpoint not configured':value['error']=''
    return value
async def resolved(f,timeout=5):
    deadline=time.monotonic()+timeout
    while not f.done():
        if time.monotonic()>deadline:raise TimeoutError('ROS response timed out')
        await asyncio.sleep(.005)
    return f.result()

async def bind_control_owner(socket_path,binding):
    """Keep an operation-scoped native connection alive for the WebSocket.

    Closing this connection (including process death) is an event-based hold.
    A canceled coroutine must also close a bind that finishes later in a worker.
    """
    client=Client(socket_path)
    pending=asyncio.create_task(asyncio.to_thread(client.call,'bind_control_owner',**binding))
    try:
        response=await asyncio.shield(pending)
        if response.get('accepted') is not True or any(response.get(key)!=binding[key]
                for key in ('arm_id','controller_session','operation_id')):
            raise ValueError('Native control owner binding was not confirmed')
        return client
    except BaseException:
        client.close()
        def close_late(future):
            try:future.result()
            except (Exception,asyncio.CancelledError):pass
            finally:client.close()
        pending.add_done_callback(close_late)
        raise

class DashboardNode(BaseNode):
    def __init__(self):
        super().__init__('dashboard_node');self.states={};self.inference={};self.joints={};self.images={};self.events=[];self.upload={};self.workflows={};self.decisions=[];self.goals={};self.stopping=False
        self.motor_records={};self.motor_record_errors={};self.public_resources=resources(self.config)
        self.policy_manager=PolicyManager(self.config,resources=lambda:self.public_resources,bridge=Client(self.config['socket']))
        self.subscription_activity=SubscriptionActivity()
        rpc_provider=RpcTraceClient(rpc_trace_socket(self.config)) if self.config.get('rpc_trace',{}).get('enabled',True) else None
        self.graph_observer=GraphObserver(self,self.config.get('graph_observer',{}),activity_provider=self.subscription_activity.snapshot,rpc_provider=rpc_provider)
        group=ReentrantCallbackGroup()
        self.permission_client=self.create_client(SetControlPermission,'/soarm_station/lerobot/set_permission',callback_group=group)
        self.hold_client=self.create_client(Hold,'/soarm_station/lerobot/hold',callback_group=group)
        self.history_client=self.create_client(QueryHistory,'/soarm_station/telemetry/query_history',callback_group=group)
        self.operation=ActionClient(self,ExecuteOperation,'/soarm_station/lerobot/operate',callback_group=group)
        self.workflow=ActionClient(self,ExecuteWorkflow,'/soarm_station/workflows/execute',callback_group=group)
        self.command=self.create_publisher(OperationInput,'/soarm_station/lerobot/command_stream',SENSOR)
        def subscribe(message_type,name,callback,qos):
            return observe_subscription(self.subscription_activity,self.create_subscription,message_type,name,callback,qos)
        for a in self.config['arms']:
            n=a['id'];subscribe(JointState,topic(n,'joint_states'),lambda m,n=n:self.joints.__setitem__(n,asdict(m)),SENSOR)
            subscribe(OperationStatus,topic(n,'lerobot/status'),lambda m,n=n:self.states.__setitem__(n,asdict(m)),STATE)
            subscribe(DiagnosticArray,topic(n,'lerobot/inference_status'),lambda m,n=n:self.inference.__setitem__(n,asdict(m)),STATE)
        for c in self.config['cameras']:
            subscribe(CompressedImage,'/soarm_station/camera/'+c['id']+'/image_raw/compressed',lambda m,n=c['id']:self.images.__setitem__(n,(bytes(m.data),m.header.stamp.sec*10**9+m.header.stamp.nanosec)),SENSOR)
        subscribe(Event,'/soarm_station/events',self.event,STATE)
        subscribe(UploadStatus,'/soarm_station/telemetry/upload_status',lambda m:setattr(self,'upload',asdict(m)),STATE)
        subscribe(WorkflowDecision,'/soarm_station/workflow/decision',lambda m:self.decisions.append(asdict(m)) or self.decisions.__delitem__(slice(None,-50)),STATE)
        subscribe(WorkflowStatus,'/soarm_station/workflow/status',lambda m:self.workflows.__setitem__(m.arm_id,asdict(m)),STATE)
        subscribe(DiagnosticArray,'/soarm_station/health',self.motor_health,STATE)
        self.graph_observer.start()
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
        app=self.create_app(token)
        self.runner=web.AppRunner(app,access_log=None);await self.runner.setup()
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        parent=Path(self.config['_path']).parent
        context.load_cert_chain(parent/cfg['cert'],parent/cfg['key'])
        await web.TCPSite(self.runner,cfg.get('host','0.0.0.0'),cfg.get('port',8443),ssl_context=context).start()
    def create_app(self,token=''):
        app=web.Application(middlewares=[dashboard_access_middleware(self.config.get('web',{}),token)],client_max_size=262144)
        app.router.add_get('/',self.index);app.router.add_get('/api/status',self.status);app.router.add_get('/api/history',self.history)
        app.router.add_post('/api/permission',self.permission);app.router.add_post('/api/hold',self.hold)
        app.router.add_post('/api/stream',self.stream_input);app.router.add_post('/api/operate',self.operate);app.router.add_get('/api/operations/{id}',self.operation_status)
        app.router.add_post('/api/workflows',self.start_workflow);app.router.add_get('/api/cameras/{id}.jpg',self.image)
        app.router.add_get('/ws/control',self.control);app.router.add_get('/ws/camera/{id}',self.camera_stream)
        app.router.add_get('/assets/{file}',self.asset)
        app.router.add_get('/api/resources',self.api_resources)
        app.router.add_get('/api/graph',self.graph_status);app.router.add_get('/api/v1/ros/graph',self.graph_status)
        app.router.add_post('/api/graph/sample',self.graph_sample);app.router.add_post('/api/v1/ros/sample',self.graph_sample)
        app.router.add_post('/api/v1/ros/watch',self.graph_watch)
        ManualRoutes(self.config).mount(app)
        async def conditional_hold(arm,session,principal,operation_id=None):
            def call():
                client=Client(self.config['socket'])
                try:return client.call('hold_owned',arm_id=arm,controller_session=session,principal=principal,operation_id=operation_id)
                finally:client.close()
            return await asyncio.to_thread(call)
        async def leader_start_state():
            started=time.monotonic()
            def read():
                client=Client(self.config['socket'])
                try:return client.call('snapshot')
                finally:client.close()
            snapshot=await asyncio.to_thread(read)
            return native_start_state(snapshot,self.public_resources,(time.monotonic()-started)*1000)
        # Admission uses authoritative native state: the permission service may
        # reply before the ROS cache sees its next status publication. Cleanup
        # retains the cached ROS view and never queries the policy store.
        self.leader_registry=LeaderHostRegistry(lambda:{'arms':dict(self.states),'resources':self.public_resources},
            conditional_hold,start_state_provider=leader_start_state)
        self.leader_registry.register_routes(app)
        if getattr(self,'policy_manager',None) is not None:
            mount_policy_routes(app,self.policy_manager)
            async def close_policy(app):await asyncio.to_thread(self.policy_manager.close)
            app.on_cleanup.append(close_policy)
        return app
    async def index(self,r):return web.Response(text=(Path(os.environ['SOARM_STATION_ROOT'])/'web/index.html').read_text(),content_type='text/html')
    def motor_health(self,message):
        expected={a['id']+'/motors':a for a in self.config['arms']}
        for status in message.status:
            arm=expected.get(status.name)
            if arm is None:continue
            try:
                values=[value.value for value in status.values if value.key=='status']
                if len(values)!=1 or len(values[0])>131072:raise ValueError('Invalid motor telemetry envelope')
                record=validate_motor_record(json.loads(values[0]),arm)
            except (ValueError,TypeError,OverflowError) as exc:
                self.motor_record_errors[arm['id']]='Rejected malformed motor telemetry: '+str(exc)[:128]
                continue
            self.motor_records[arm['id']]=record;self.motor_record_errors.pop(arm['id'],None)
    async def asset(self,request):
        name=request.match_info['file']
        if name not in ASSET_NAMES:raise web.HTTPNotFound()
        root=(Path(os.environ['SOARM_STATION_ROOT'])/'web').resolve();path=(root/name).resolve()
        if path.parent!=root or not path.is_file():raise web.HTTPNotFound()
        return web.FileResponse(path,headers={'Cache-Control':'no-cache'})
    def resource_view(self):
        value=copy.deepcopy(self.public_resources)
        if getattr(self,'policy_manager',None) is not None:
            prepared=self.policy_manager.public_runtime_profiles()
            prepared_ids={p['id'] for p in prepared}
            value['profiles']=[p for p in value['profiles'] if p['id'] not in prepared_ids]+prepared
            for arm in value['arms']:arm['profile_ids']=[p['id'] for p in value['profiles'] if p.get('arm_id')==arm['id']]
        return value
    async def api_resources(self,request):return web.json_response(await asyncio.to_thread(self.resource_view))
    async def graph_status(self,request):return web.json_response(self.graph_observer.snapshot(),headers={'Cache-Control':'no-store'})
    async def graph_sample(self,request):
        body=await request.json()
        if not isinstance(body,dict):raise ValueError('JSON object required')
        return web.json_response(self.graph_observer.sample(body))
    async def graph_watch(self,request):
        try:
            body=await request.json()
            if not isinstance(body,dict):raise ValueError('JSON object required')
            result=await asyncio.to_thread(self.graph_observer.watch,body)
        except (ValueError,KeyError) as exc:
            return web.json_response({'schema_version':1,'accepted':False,'reason':str(exc)},status=400)
        return web.json_response(result,headers={'Cache-Control':'no-store'})
    def state(self):return {'schema_version':1,'resources':self.resource_view(),
        'motor_status':render_motors(self.config,dict(self.motor_records),dict(self.joints),dict(self.states),time.time_ns(),dict(self.motor_record_errors)),'device_id':self.config['device_id'],'arms':dict(self.states),'inference':dict(self.inference),'joints':dict(self.joints),'cameras':list(self.images),'upload':telemetry_view(self.config,self.upload),'events':list(self.events),'workflows':dict(self.workflows),'decisions':list(self.decisions)}
    async def status(self,r):return web.json_response(await asyncio.to_thread(self.state))
    async def history(self,r):
        q=QueryHistory.Request();q.arm_id=r.query.get('arm','');q.after_sequence=int(r.query.get('after',0));q.page_size=min(500,int(r.query.get('limit',100)))
        return web.json_response(asdict(await resolved(self.history_client.call_async(q))))
    async def permission(self,r):
        b=await r.json();q=SetControlPermission.Request();q.request_id=uuid.uuid4().hex;q.arm_id=b['arm_id'];q.operator_session_id=b.get('operator_session_id','browser')
        q.expected_controller_session=b['controller_session'];q.enable=bool(b['enable'])
        return web.json_response(asdict(await resolved(self.permission_client.call_async(q))))
    async def hold(self,r):
        b=await r.json()
        if not isinstance(b,dict) or not isinstance(b.get('arm_id'),str) or not b['arm_id']:
            raise ValueError('Explicit arm_id required')
        scope=('controller_session','operator_session_id','operation_id')
        if any(key in b for key in scope):
            if not all(key in b for key in scope):raise ValueError('Complete hold ownership scope required')
            if any(not isinstance(b[key],str) or not 1<=len(b[key])<=128 for key in scope[:2]):
                raise ValueError('Controller and operator sessions are required')
            operation=b['operation_id']
            if operation is not None and (not isinstance(operation,str) or not 1<=len(operation)<=128):
                raise ValueError('operation_id must identify the owned operation or be null before admission')
            result=self.leader_registry.hold_callback(b['arm_id'],b['controller_session'],b['operator_session_id'],operation)
            if inspect.isawaitable(result):result=await result
            return web.json_response(result if isinstance(result,dict) else {'success':bool(result)})
        return web.json_response(asdict(await resolved(self.hold_client.call_async(Hold.Request(arm_id=b['arm_id'])))))
    async def submit(self,b,principal,feedback=None,*,persistent_target=False):
        g=goal_from_dict(b,principal,persistent_target=persistent_target);latest={}
        def receive_feedback(m):
            latest.clear();latest.update(asdict(m.feedback.status))
            if feedback:feedback(m)
        pending_goal=self.operation.send_goal_async(g,feedback_callback=receive_feedback)
        try:h=await resolved(pending_goal)
        except BaseException:
            if persistent_target:
                # A persistent target cannot rely on an idle timer to clean up
                # an accepted goal whose gateway response was lost/canceled.
                def retire_late(future):
                    try:late=future.result()
                    except (Exception,asyncio.CancelledError):return
                    if late is not None and late.accepted:late.cancel_goal_async()
                pending_goal.add_done_callback(retire_late)
            raise
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
        n=r.match_info['id']
        return await stream_camera_frames(r,lambda:self.images.get(n),lambda:self.stopping)
    async def control(self,r):
        ws=web.WebSocketResponse(protocols=['soarm_station'],max_msg_size=262144,heartbeat=10,receive_timeout=2);await ws.prepare(r)
        arm='';h=None;feedback={};principal=uuid.uuid4().hex;control_owner=None
        try:
            b=await ws.receive_json(timeout=5);arm=b['arm_id']
            if b['operation_type']!='DIRECT' or b['mode']!='STREAM':raise ValueError('Remote control uses DIRECT STREAM')
            semantics=b.get('input_semantics','live_sample')
            if semantics not in ('live_sample','persistent_target'):
                raise ValueError('Unknown DIRECT input semantics')
            state=self.states[arm]
            # Opening a socket is not consent to arm: permission must already be granted to this requested operator ID.
            principal=b['operator_session_id']
            def on_feedback(m):feedback.clear();feedback.update(asdict(m.feedback.status))
            # Browser target intent opts in explicitly. Native leader/operator
            # clients also use this route; UI producers explicitly request
            # persistent_target while unmarked external streams retain live_sample.
            # Socket close/error cancels this exact action for either contract.
            i,h=await self.submit(b,principal,on_feedback,persistent_target=semantics=='persistent_target');result=self.goals[i]['future'];last_feedback=0.;last_state=0.
            while not ws.closed and not self.stopping and not result.done():
                t=time.monotonic()
                if semantics=='persistent_target' and control_owner is None and feedback.get('operation_id'):
                    control_owner=await bind_control_owner(self.config['socket'],{
                        'arm_id':arm,'controller_session':b['controller_session'],
                        'principal':principal,'operation_id':feedback['operation_id']})
                if feedback and (semantics!='persistent_target' or control_owner is not None) and t-last_feedback>=.05:
                    await ws.send_json({'type':'grant','grant':dict(feedback)});last_feedback=t
                if t-last_state>=.01:
                    await ws.send_json({'type':'state','state':self.states.get(arm),'joints':self.joints.get(arm)});last_state=t
                try:m=await ws.receive(timeout=.005)
                except asyncio.TimeoutError:continue
                if m.type==web.WSMsgType.TEXT:
                    import json
                    d=json.loads(m.data)
                    if d.get('arm_id')!=arm:raise ValueError('Arm mismatch')
                    if semantics=='persistent_target' and control_owner is None:
                        raise ValueError('Persistent control ownership is not ready')
                    self.command.publish(stream_from_dict(d,principal))
                elif m.type in (web.WSMsgType.CLOSE,web.WSMsgType.CLOSED,web.WSMsgType.ERROR):break
            if result.done() and not ws.closed:
                await ws.send_json({'type':'result','result':asdict(result.result().result)})
        except Exception as exc:
            if not ws.closed:await ws.send_json({'type':'error','reason':str(exc)[:512]})
        finally:
            try:
                if control_owner is not None:control_owner.close()
            finally:
                try:
                    if h:h.cancel_goal_async()
                finally:
                    # Both cleanup paths remain operation-scoped. Even an action
                    # cancellation error must close the native lifetime and WS.
                    await ws.close()
        return ws
    def destroy_node(self):
        self.graph_observer.close()
        self.stopping=True;self.loop.call_soon_threadsafe(self.loop.stop);self.thread.join(timeout=4);super().destroy_node()
def main():run(DashboardNode)
