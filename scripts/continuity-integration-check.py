#!/usr/bin/env python3
"""Mock-only authenticated ROS/web continuity qualification, including >120s."""
import argparse
import asyncio
import json
import math
import os
import ssl
import time
import uuid
from pathlib import Path
from aiohttp import ClientSession

async def run(args):
    tls=ssl.create_default_context(cafile=args.ca);principal='continuity-'+uuid.uuid4().hex
    report={'physical_actuation':False,'passed':False};sequence=0
    async with ClientSession(headers={'Authorization':'Bearer '+os.environ['SOARM_TOKEN']}) as session:
        async def api(path,body=None,expect_error=False):
            async with session.request('POST' if body is not None else 'GET',args.url+path,json=body,ssl=tls) as response:
                if expect_error:
                    assert response.status==400,(response.status,await response.text());return
                response.raise_for_status();return await response.json()
        async def state():return await api('/api/status')
        initial=await state()
        assert initial['device_id']=='linux-workstation' and set(initial['arms'])=={'arm1','arm2'},'MOCK PROFILE REQUIRED'
        arm='arm1';base=initial['arms'][arm]
        async def enable():
            s=(await state())['arms'][arm]
            r=await api('/api/permission',{'arm_id':arm,'controller_session':s['controller_session'],'operator_session_id':principal,'enable':True})
            assert r['accepted'],r
            return s
        async def goal(kind,mode,**kwargs):
            s=(await state())['arms'][arm]
            return await api('/api/operate',{'arm_id':arm,'runtime_boot_id':s['runtime_boot_id'],'controller_session':s['controller_session'],
                'operator_session_id':principal,'operation_type':kind,'mode':mode,**kwargs})
        async def wait_result(identifier):
            deadline=time.monotonic()+6
            while time.monotonic()<deadline:
                result=await api('/api/operations/'+identifier)
                if result['done']:return result['result']
                await asyncio.sleep(.02)
            raise TimeoutError('Operation did not finish')
        await enable()
        joints=initial['joints'][arm];q={n.split('/')[-1]:v for n,v in zip(joints['name'],joints['position'])}
        for value in (.04,.08):
            target=dict(q);target['shoulder_pan']=value
            request=await goal('TRAJECTORY','INLINE',points=[{'positions':target,'time_s':.4}])
            result=await wait_result(request['request_id']);assert result['execution_completed'],result
            assert (await state())['arms'][arm]['permission']
        report['consecutive_inline_one_grant']=True
        request=await goal('TRAJECTORY','STREAM');identifier=request['request_id']
        async def feedback():
            deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                op=await api('/api/operations/'+identifier)
                if op['feedback'].get('stream_ready'):return op['feedback']
                await asyncio.sleep(.01)
            raise TimeoutError('No feedback grant')
        async def send(kind,revision=0,body=None,input_id=''):
            nonlocal sequence
            grant=await feedback();sequence+=1
            frame={k:grant[k] for k in ('runtime_boot_id','controller_session','operation_id','operation_epoch','window_id')}
            frame.update(arm_id=arm,operator_session_id=principal,sequence=sequence,frame_kind=kind,input_id=input_id,
                         semantic_revision=revision,payload_kind='JOINT_TRAJECTORY' if kind=='COMMAND' else 'NONE')
            if body is not None:frame['body']=body
            return await api('/api/stream',frame)
        target=dict(q);target['shoulder_pan']=.10;one={'points':[{'positions':target,'time_s':.4}]}
        await send('COMMAND',1,one,'one')
        end=time.monotonic()+3.2
        while time.monotonic()<end:await send('HEARTBEAT');await asyncio.sleep(.06)
        op=await api('/api/operations/'+identifier)
        assert not op['done'] and op['feedback']['last_accepted_semantic_revision']==1
        report['stream_endpoint_remains_active']=True
        target2=dict(q);target2['shoulder_pan']=.12;two={'points':[{'positions':target2,'time_s':.4}]}
        await send('COMMAND',2,two,'two');await asyncio.sleep(.08)
        await send('COMMAND',1,one,'one');await asyncio.sleep(.08)
        fb=await feedback()
        assert fb['last_accepted_semantic_revision']==2 and 'STALE_SEMANTIC_REVISION' in fb['reason'],fb
        report['semantic_revision_wire_and_old_retry_rejection']=True
        await send('END');result=await wait_result(identifier);assert result['execution_completed']
        await asyncio.sleep(.15) # Status publication is asynchronous to action result delivery.
        assert not (await state())['arms'][arm]['permission']
        s=await enable();grant={};sent=0;states=0;alive=True;terminal=None;last_grant=0.;max_grant_gap=0.;max_send_gap=0.;last_send=0.
        async with session.ws_connect(args.url+'/ws/control',ssl=tls) as ws:
            await ws.send_json({'arm_id':arm,'runtime_boot_id':s['runtime_boot_id'],'controller_session':s['controller_session'],
                               'operator_session_id':principal,'operation_type':'DIRECT','mode':'STREAM'})
            async def receive():
                nonlocal grant,states,alive,terminal,last_grant,max_grant_gap
                async for message in ws:
                    if message.type.value==1:
                        d=json.loads(message.data)
                        if d['type']=='grant':
                            current=time.monotonic()
                            if last_grant:max_grant_gap=max(max_grant_gap,current-last_grant)
                            last_grant=current;grant=d['grant']
                        elif d['type']=='state':states+=1
                        elif d['type']=='error':raise RuntimeError(d['reason'])
                        elif d['type']=='result':terminal=d['result']
                alive=False
            receiver=asyncio.create_task(receive());start=time.monotonic();deadline=start;next_log=start+30
            try:
                while time.monotonic()-start<args.seconds:
                    if receiver.done():
                        await receiver
                        print(json.dumps({'terminal':terminal,'elapsed_s':time.monotonic()-start,'sent':sent,'last_acked_sequence':grant.get('last_sequence'),'max_grant_gap_ms':max_grant_gap*1000,'max_send_gap_ms':max_send_gap*1000}),flush=True)
                        raise RuntimeError('Healthy stream ended early: '+json.dumps(terminal))
                    if grant:
                        t=time.monotonic()
                        if last_send:max_send_gap=max(max_send_gap,t-last_send)
                        last_send=t;target=dict(q);target['shoulder_pan']=.10+.03*math.sin((t-start)*.6)
                        sent+=1
                        await ws.send_json({**grant,'arm_id':arm,'frame_kind':'COMMAND','sequence':sent,'input_id':str(sent),
                                           'body':{'positions':target,'gripper':s['gripper_percent'],'gripper_unit':'percent'}})
                    if time.monotonic()>=next_log:
                        print('Healthy mock stream elapsed',round(time.monotonic()-start,1),'seconds; commands',sent,flush=True);next_log+=30
                    deadline+=.01
                    if deadline<time.monotonic():deadline=time.monotonic()
                    await asyncio.sleep(max(0,deadline-time.monotonic()))
                assert alive and grant['phase']=='ACTIVE'
                report['long_stream']={'duration_s':time.monotonic()-start,'commands_sent':sent,'state_frames':states,
                                       'requested_duration':None,'healthy_past_120s':True,'max_grant_gap_ms':max_grant_gap*1000,'max_send_gap_ms':max_send_gap*1000}
            finally:receiver.cancel()
        await asyncio.sleep(.6)
        stopped=(await state())['arms'][arm]
        assert not stopped['permission'] and stopped['phase']=='HOLD' and stopped['measured_torque']=='ON'
        report['disconnect_revokes_and_preserves_torque']=True
        report['independent_arm_unchanged']=(await state())['arms']['arm2']['phase']=='IDLE'
        report['passed']=True
    Path(args.output).write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--url',default='https://localhost:8443');p.add_argument('--ca',required=True)
    p.add_argument('--seconds',type=float,default=130.);p.add_argument('--output',required=True);a=p.parse_args()
    if a.seconds<=120:p.error('This qualification must exceed120 seconds')
    asyncio.run(run(a))
