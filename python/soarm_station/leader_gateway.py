"""Small input-host registry mounted under the dashboard's existing access policy.

Only route/state glue: no device access and no permission-enable calls. The
native host opens its selected leader and uses the existing DIRECT stream.
"""
import asyncio
import copy
import inspect
import math
import time
import uuid


def native_start_state(snapshot,resources,elapsed_ms=0.):
    """Use current native admission state, including IPC age, instead of ROS publication lag."""
    arms={}
    for name,value in snapshot.get('arms',{}).items():
        arm=dict(value);age=arm.get('age_ms')
        valid_age=(type(age) in (int,float) and math.isfinite(age) and age>=0
                   and type(elapsed_ms) in (int,float) and math.isfinite(elapsed_ms) and elapsed_ms>=0)
        arm['feedback_valid']=bool(arm.get('positions')) and valid_age and age+elapsed_ms<150
        arm['feedback_age_ms']=age+elapsed_ms if valid_age else None
        arms[name]=arm
    return {'arms':arms,'resources':resources}


class LeaderHostRegistry:
    def __init__(self, state_provider, hold_callback, lease_s=2.5,start_state_provider=None):
        self.state_provider, self.hold_callback, self.lease_s = state_provider, hold_callback, lease_s
        self.start_state_provider=start_state_provider
        self.hosts={};self.closed=False;self.watch_task=None

    def snapshot(self):
        now=time.monotonic();result=[]
        for host in self.hosts.values():
            active=host.get('active')
            result.append({'id':host['id'],'label':host['label'],'platform':host.get('platform',''),
                'connected':host.get('ws') is not None and not host['ws'].closed,
                'state':host['state'],'reason':host.get('reason',''),
                'devices':copy.deepcopy(host['devices']),
                'active':{k:v for k,v in active.items() if k not in ('last_browser','stop_waiter')} if active else None,
                'latest_pose':copy.deepcopy(host.get('latest_pose')),
                'sample_age_ms':host.get('sample_age_ms'),
                'report_age_ms':None if not host.get('last_sample_report') else (now-host['last_sample_report'])*1000,
                'measured_hz':host.get('measured_hz'),'last_seen_age_ms':(now-host['last_seen'])*1000})
        return {'schema_version':1,'hosts':result,'browser_lease_ms':int(self.lease_s*1000),
                'discovery':'connected native input hosts; OS inventory only, no serial probing'}

    @staticmethod
    def _inventory(body):
        if not isinstance(body,dict) or not isinstance(body.get('id'),str) or not 1<=len(body['id'])<=128:
            raise ValueError('Bounded host ID required')
        devices=body.get('devices',[])
        if not isinstance(devices,list) or len(devices)>128:raise ValueError('Too many serial devices')
        clean=[];seen=set()
        for device in devices:
            if not isinstance(device,dict):raise ValueError('Device metadata must be an object')
            identifier=device.get('id')
            if not isinstance(identifier,str) or not 1<=len(identifier)<=128 or identifier in seen:raise ValueError('Unique bounded device IDs required')
            if type(device.get('configured')) is not bool:raise ValueError('Device configured flag must be boolean')
            joints=device.get('joint_names',[])
            if not isinstance(joints,list) or len(joints)>32 or any(not isinstance(n,str) or not 1<=len(n)<=128 for n in joints):
                raise ValueError('Bounded joint name list required')
            for key,limit in (('label',128),('reason',512),('port',1024),('serial',256),('profile_id',128)):
                value=device.get(key)
                if value is not None and (not isinstance(value,str) or len(value)>limit):raise ValueError('Invalid device '+key)
            seen.add(identifier)
            clean.append({k:device.get(k) for k in ('id','label','configured','reason','port','serial','vid','pid','profile_id','joint_names')})
        return {'id':body['id'],'label':str(body.get('label',body['id']))[:128],
                'platform':str(body.get('platform',''))[:64],'devices':clean}

    async def _hold_current(self, active):
        if active is None:return {'success':False,'reason':'No registry-owned run'}
        state=self.state_provider().get('arms',{}).get(active['arm_id'])
        if not state or state.get('controller_session')!=active['controller_session']:
            return {'success':False,'reason':'Controller session changed; no unrelated hold issued'}
        current=state.get('operation_id','');owned=active.get('operation_id','')
        if current and current!=owned:
            return {'success':False,'reason':'Current operation ownership not confirmed; no unrelated hold issued'}
        if not current and not state.get('permission',False):
            return {'success':True,'reason':'Run already stopped and permission disabled'}
        result=self.hold_callback(active['arm_id'],active['controller_session'],active['operator_session_id'],active.get('operation_id') or None)
        result=await result if inspect.isawaitable(result) else result
        return result if isinstance(result,dict) else {'success':bool(result)}

    async def stop_host(self, host, run_id, principal, reason='OPERATOR_STOP'):
        active=host.get('active')
        if active is None:
            return {'accepted':False,'stopped':False,'reason':'No active run owned by this input host'}
        if active['run_id']!=run_id or active['operator_session_id']!=principal:
            raise ValueError('Leader stop session does not match the active run')
        if host['state']=='STOPPING':
            return {'accepted':True,'stopped':False,'state':'STOPPING','reason':'Stop is already pending'}
        host.update(state='STOPPING',reason=reason)
        waiter=asyncio.get_running_loop().create_future();active['stop_waiter']=waiter
        ws=host.get('ws');confirmed=False;hold=None
        if ws is not None and not ws.closed:
            await ws.send_json({'type':'stop','run_id':run_id})
            try:hold=await asyncio.wait_for(asyncio.shield(waiter),3.5);confirmed=True
            except TimeoutError:pass
        if hold is None:hold=await self._hold_current(active)
        # A missing stop ACK does not permit another device open or a new grant.
        if confirmed or ws is None or ws.closed:
            if host.get('active') is active:host['active']=None
            host['state']='IDLE' if ws is not None and not ws.closed else 'OFFLINE'
        else:host['reason']='Input-host cleanup not confirmed; follower hold requested'
        return {'accepted':True,'stopped':confirmed,'hold_confirmed':bool(hold.get('success')),
                'state':host['state'],'reason':host.get('reason',''),'run_id':run_id}

    async def hosts_route(self,request):
        from aiohttp import web
        return web.json_response(self.snapshot())

    @staticmethod
    async def _body(request):
        body=await request.json()
        if not isinstance(body,dict):raise ValueError('JSON object required')
        return body

    async def start_route(self,request):
        from aiohttp import web
        body=await self._body(request);host=self.hosts.get(request.match_info['host_id'])
        if not host or host.get('ws') is None or host['ws'].closed:raise ValueError('Input host is offline')
        if host.get('active'):raise ValueError('Input host is busy or cleanup is pending')
        device=next((d for d in host['devices'] if d['id']==body.get('device_id')),None)
        if not device or not device['configured']:raise ValueError('Select a connected configured leader device')
        arm_id=body.get('arm_id')
        if not isinstance(arm_id,str) or not 1<=len(arm_id)<=128:raise ValueError('Explicit known follower arm required')
        if self.start_state_provider is None:
            state=self.state_provider()
        else:
            connection=host['ws']
            state=self.start_state_provider()
            if inspect.isawaitable(state):state=await state
            # Native reads yield the HTTP loop. Recheck ownership/inventory before
            # reserving a run so a competing Start or reconnect cannot be overwritten.
            if self.closed or self.hosts.get(request.match_info['host_id']) is not host or host.get('ws') is not connection or connection.closed:
                raise ValueError('Input host changed while checking follower state')
            if host.get('active'):raise ValueError('Input host is busy or cleanup is pending')
            device=next((d for d in host['devices'] if d['id']==body.get('device_id')),None)
            if not device or not device['configured']:raise ValueError('Select a connected configured leader device')
        arm=state.get('arms',{}).get(arm_id)
        if not arm:raise ValueError('Explicit known follower arm required')
        if any(h.get('active') and h['active']['arm_id']==arm_id for h in self.hosts.values()):raise ValueError('Follower already has an input-host run')
        if not arm.get('permission'):raise ValueError('Follower control permission is not enabled')
        if not arm.get('feedback_valid',False):raise ValueError('Fresh follower feedback is unavailable')
        if arm.get('operation_id'):raise ValueError('Follower has an active operation')
        for key in ('runtime_boot_id','controller_session'):
            if body.get(key)!=arm.get(key):raise ValueError('Follower boot/controller session mismatch')
        principal=body.get('operator_session_id')
        if not isinstance(principal,str) or not 1<=len(principal)<=128:raise ValueError('Operator session required')
        resource=next((r for r in state.get('resources',{}).get('arms',[]) if r['id']==arm_id),None)
        if not resource:raise ValueError('Follower resource metadata unavailable')
        joints=[j['name'] for j in resource['joints']]
        if set(joints)!=set(device.get('joint_names') or []):raise ValueError('Configured leader mapping does not match follower joints')
        hz=body.get('hz',100)
        if type(hz) not in (int,float) or not 50<=hz<=100:raise ValueError('Leader input rate must be 50..100 Hz')
        follow=body.get('follow_gripper',True)
        if type(follow) is not bool:raise ValueError('follow_gripper must be boolean')
        reference=body.get('gripper_reference')
        if type(reference) in (int,float) and math.isfinite(reference):
            if not 0<=reference<=100:raise ValueError('gripper_reference must be percent in [0,100]')
            reference=float(reference)
        else:reference=None # Native bridge uses fresh follower feedback, never a guessed value.
        run_id=uuid.uuid4().hex
        active={'run_id':run_id,'device_id':device['id'],'arm_id':arm_id,'operator_session_id':principal,
                'runtime_boot_id':arm['runtime_boot_id'],'controller_session':arm['controller_session'],
                'operation_id':'','hz':float(hz),'follow_gripper':follow,'gripper_reference':reference,
                'last_browser':time.monotonic()}
        host.update(active=active,state='STARTING',reason='Opening selected leader and checking calibration',latest_pose=None,last_sample_report=None)
        try:
            await host['ws'].send_json({'type':'start',**{k:v for k,v in active.items() if k!='last_browser'},'joint_names':joints})
        except Exception:
            host['active']=None;host['state']='OFFLINE';raise
        return web.json_response({'accepted':True,'run_id':run_id,'state':'STARTING','reason':host['reason'],
                                  'follow_gripper':follow,'heartbeat_interval_ms':500,'browser_lease_ms':int(self.lease_s*1000)})

    async def settings_route(self,request):
        from aiohttp import web
        body=await self._body(request);host=self.hosts.get(request.match_info['host_id']);active=host.get('active') if host else None
        if not active or active['run_id']!=body.get('run_id') or active['operator_session_id']!=body.get('operator_session_id'):
            raise ValueError('Leader settings do not match the active run')
        follow=body.get('follow_gripper')
        if type(follow) is not bool:raise ValueError('follow_gripper must be boolean')
        if host['state'] in ('STOPPING','OFFLINE','ERROR') or host.get('ws') is None or host['ws'].closed:
            raise ValueError('Leader run is stopping or disconnected')
        # Same operation/run only. This changes no permission, liveness deadline,
        # body target, coordinate offset or runtime clutch state.
        async with host.setdefault('_settings_lock',asyncio.Lock()):
            if host.get('active') is not active or host['state'] in ('STOPPING','OFFLINE','ERROR'):
                raise ValueError('Leader run ended before forwarding settings')
            await host['ws'].send_json({'type':'settings','run_id':active['run_id'],
                'operator_session_id':active['operator_session_id'],'follow_gripper':follow})
            if host.get('active') is not active or host['state'] in ('STOPPING','OFFLINE','ERROR'):
                raise ValueError('Leader run ended while forwarding settings')
            active['follow_gripper']=follow
        return web.json_response({'accepted':True,'run_id':active['run_id'],'follow_gripper':follow})

    async def heartbeat_route(self,request):
        from aiohttp import web
        body=await self._body(request);host=self.hosts.get(request.match_info['host_id']);active=host.get('active') if host else None
        if not active or active['run_id']!=body.get('run_id') or active['operator_session_id']!=body.get('operator_session_id'):
            raise ValueError('Leader heartbeat does not match the active run')
        if host['state'] in ('STOPPING','OFFLINE','ERROR'):raise ValueError('Leader session is not renewable')
        if time.monotonic()-active['last_browser']>self.lease_s:raise ValueError('Leader browser lease expired')
        active['last_browser']=time.monotonic()
        return web.json_response({'accepted':True,'run_id':active['run_id']})

    async def stop_route(self,request):
        from aiohttp import web
        body=await self._body(request);host=self.hosts.get(request.match_info['host_id'])
        if not host:return web.json_response({'accepted':False,'stopped':False,'reason':'Unknown input host'})
        return web.json_response(await self.stop_host(host,body.get('run_id'),body.get('operator_session_id')))

    async def register_route(self,request):
        from aiohttp import web
        ws=web.WebSocketResponse(heartbeat=5,max_msg_size=262144);await ws.prepare(request);host=None
        try:
            first=await ws.receive_json(timeout=5)
            if not isinstance(first,dict) or first.get('type')!='hello':raise ValueError('Input host hello required')
            inventory=self._inventory(first['host']);previous=self.hosts.get(inventory['id'])
            if previous and previous.get('ws') is not None and not previous['ws'].closed:raise ValueError('Host ID already connected')
            if previous and previous.get('active'):raise ValueError('Previous input-host cleanup has not completed')
            host={**inventory,'ws':ws,'state':'IDLE','active':None,'last_seen':time.monotonic(),'reason':''}
            self.hosts[host['id']]=host
            await ws.send_json({'type':'registered','host_id':host['id']})
            async for message in ws:
                if message.type!=web.WSMsgType.TEXT:continue
                body=message.json()
                if not isinstance(body,dict):raise ValueError('Input-host message must be an object')
                host['last_seen']=time.monotonic()
                if body.get('type')=='inventory':
                    inventory=self._inventory(body['host'])
                    if inventory['id']!=host['id']:raise ValueError('Input host identity changed')
                    host.update(inventory)
                elif body.get('type') in ('state','stopped'):
                    active=host.get('active')
                    if not active or body.get('run_id')!=active['run_id']:continue
                    if body.get('operation_id'):active['operation_id']=body['operation_id']
                    if body['type']=='stopped':
                        hold=await self._hold_current(active)
                        if host.get('active') is active:host.update(active=None,state='IDLE')
                        waiter=active.get('stop_waiter')
                        if waiter is not None and not waiter.done():waiter.set_result(hold)
                    else:
                        if host['state']!='STOPPING':host['state']=body.get('state','ERROR')
                        host['reason']=str(body.get('reason',''))[:512]
                        reference=body.get('gripper_reference')
                        if type(reference) in (int,float) and math.isfinite(reference) and 0<=reference<=100:
                            active['gripper_reference']=float(reference)
                        if body.get('pose') is not None:
                            pose=body['pose'];positions=pose.get('positions') if isinstance(pose,dict) else None
                            if not isinstance(positions,dict) or len(positions)>32 or any(not isinstance(n,str) or type(v) not in (int,float) or not math.isfinite(v) for n,v in positions.items()):
                                raise ValueError('Invalid reported leader pose')
                            host.update(latest_pose=body['pose'],sample_age_ms=body.get('sample_age_ms'),
                                        measured_hz=body.get('measured_hz'),last_sample_report=time.monotonic())
        except Exception as exc:
            if not ws.closed:await ws.send_json({'type':'error','reason':str(exc)[:512]})
        finally:
            if host is not None and self.hosts.get(host['id']) is host:
                active=host.get('active');host['ws']=None;host['state']='OFFLINE'
                if active:await self._hold_current(active)
                host['active']=None;host['reason']='Input host disconnected; explicit Start is required after reconnect'
            await ws.close()
        return ws

    async def _watch(self):
        while not self.closed:
            for host in list(self.hosts.values()):
                active=host.get('active')
                if active and host['state']!='STOPPING' and time.monotonic()-active['last_browser']>self.lease_s:
                    asyncio.create_task(self.stop_host(host,active['run_id'],active['operator_session_id'],'BROWSER_LEASE_EXPIRED'))
            await asyncio.sleep(.25)

    async def close(self):
        self.closed=True
        if self.watch_task:self.watch_task.cancel()
        for host in list(self.hosts.values()):
            active=host.get('active')
            if active and host['state']!='STOPPING':
                await self.stop_host(host,active['run_id'],active['operator_session_id'],'GATEWAY_SHUTDOWN')
            ws=host.get('ws')
            if ws is not None and not ws.closed:await ws.close()

    def register_routes(self,app):
        from aiohttp import web
        app.add_routes([web.get('/api/input-hosts',self.hosts_route),
            web.post('/api/input-hosts/{host_id}/start',self.start_route),
            web.post('/api/input-hosts/{host_id}/heartbeat',self.heartbeat_route),
            web.post('/api/input-hosts/{host_id}/settings',self.settings_route),
            web.post('/api/input-hosts/{host_id}/stop',self.stop_route),
            web.get('/ws/input-host',self.register_route)])
        async def startup(app):self.watch_task=asyncio.create_task(self._watch())
        async def shutdown(app):await self.close()
        app.on_startup.append(startup);app.on_shutdown.append(shutdown)
