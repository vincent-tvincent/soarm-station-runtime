#!/usr/bin/env python3
"""Explicit real DIRECT stream/disconnect/freshness test. Never loads a policy."""
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
from soarm_station.config import load
from soarm_station.ipc import Client

async def run(args):
    c=load(args.config);arm=next(a for a in c['arms'] if a['id']==args.arm)
    if arm['backend']!='so101' or arm.get('read_only'):raise ValueError('An explicit writable native profile is required')
    context=ssl.create_default_context(cafile=str(Path(c['_path']).parent/c['web']['cert']))
    principal='stream-check-'+uuid.uuid4().hex;report={'arm_id':args.arm,'passed':False};local=Client(c['socket'])
    async with ClientSession(headers={'Authorization':'Bearer '+os.environ[c['web'].get('token_env','SOARM_STATION_TOKEN')]}) as session:
        async def api(path,body=None):
            async with session.request('POST' if body is not None else 'GET',args.url+path,json=body,ssl=context) as r:
                r.raise_for_status();return await r.json()
        async def snapshot():return local.call('snapshot')['arms'][args.arm]
        async def connect():
            s=await snapshot()
            r=await api('/api/permission',{'arm_id':args.arm,'controller_session':s['controller_session'],'operator_session_id':principal,'enable':True})
            if not r['accepted']:raise RuntimeError(r['reason'])
            ws=await session.ws_connect(args.url+'/ws/control',ssl=context,max_msg_size=262144)
            await ws.send_json({'arm_id':args.arm,'runtime_boot_id':s['runtime_boot_id'],'controller_session':s['controller_session'],
                'operator_session_id':principal,'operation_type':'DIRECT','mode':'STREAM','duration_s':10})
            grant={}
            async def receiver():
                async for message in ws:
                    if message.type.value==1:
                        d=json.loads(message.data)
                        if d['type']=='grant':grant.clear();grant.update(d['grant'])
                        elif d['type']=='error':raise RuntimeError(d['reason'])
            task=asyncio.create_task(receiver());deadline=time.monotonic()+3
            while not grant and time.monotonic()<deadline:
                if task.done():await task
                await asyncio.sleep(.01)
            if not grant:raise TimeoutError('No READY grant')
            return ws,grant,task,s
        async def send(ws,g,sequence,kind,body=None):
            d={k:g[k] for k in ('runtime_boot_id','controller_session','operation_id','operation_epoch','window_id')}
            d.update(arm_id=args.arm,sequence=sequence,frame_kind=kind,input_id=uuid.uuid4().hex if kind=='COMMAND' else '')
            if body is not None:d['body']=body
            await ws.send_json(d)
        try:
            ws,grant,task,initial=await connect();source=dict(initial['positions']);g=initial['gripper'];seq=0;positions=[]
            start=time.monotonic();deadline=start
            while time.monotonic()-start<2.5:
                current=time.monotonic();q=dict(source);q['shoulder_pan']+=.04*math.sin((current-start)*3)
                seq+=1;await send(ws,grant,seq,'COMMAND',{'positions':q,'gripper':g,'gripper_unit':'percent'})
                positions.append((await snapshot())['positions']['shoulder_pan'])
                if task.done():await task
                deadline+=.01;await asyncio.sleep(max(0,deadline-time.monotonic()))
            await ws.close();task.cancel();await asyncio.sleep(.5)
            stopped=await snapshot()
            assert stopped['phase']=='HOLD' and not stopped['permission'] and stopped['health']['torque']=='ON'
            report['stream']={'sent':seq,'duration_s':2.5,'measured_pan_excursion_rad':max(positions)-min(positions),
                              'disconnect_hold':True,'torque_after_disconnect':stopped['health']['torque']}
            assert report['stream']['measured_pan_excursion_rad']>.015,'No physical stream response measured'
            ws,grant,task,base=await connect();operation=grant['operation_id'];seq=1
            await send(ws,grant,seq,'COMMAND',{'positions':dict(base['positions']),'gripper':base['gripper'],'gripper_unit':'percent'})
            heartbeat_start=time.monotonic()
            while time.monotonic()-heartbeat_start<.7 and not ws.closed and not task.done():
                seq+=1;await send(ws,grant,seq,'HEARTBEAT');await asyncio.sleep(.03)
            await ws.close();task.cancel();await asyncio.sleep(.2)
            result=local.call('result',arm_id=args.arm,operation_id=operation)
            assert result and result['reason']=='DIRECT_INPUT_EXPIRED',result
            stopped=await snapshot();assert not stopped['permission'] and stopped['health']['torque']=='ON'
            report['heartbeat_only']={'result_reason':result['reason'],'permission_disabled':True,'torque_preserved':True}
            report['passed']=True
        finally:
            report['cleanup_hold']=await api('/api/hold',{'arm_id':args.arm});local.close()
            Path(args.output).write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--arm',required=True)
    p.add_argument('--url',default='https://localhost:8443');p.add_argument('--output',required=True);p.add_argument('--execute',action='store_true');args=p.parse_args()
    if not args.execute:p.error('Pass --execute for the explicitly authorized physical test')
    asyncio.run(run(args))
