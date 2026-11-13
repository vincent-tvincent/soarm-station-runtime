#!/usr/bin/env python3
"""Small explicitly authorized real-arm continuity confirmation via ROS/HTTPS."""
import argparse
import asyncio
import json
import os
import ssl
import time
import uuid
from pathlib import Path
from aiohttp import ClientSession
from soarm_station.config import load
from soarm_station.ipc import Client

async def run(a):
    c=load(a.config);cfg=next(x for x in c['arms'] if x['id']==a.arm)
    assert cfg['backend']=='so101' and not cfg.get('read_only')
    joint=next(j for j in cfg['joints'] if j['name']==a.joint)
    tls=ssl.create_default_context(cafile=str(Path(c['_path']).parent/c['web']['cert']))
    principal='physical-continuity-'+uuid.uuid4().hex;local=Client(c['socket']);report={'passed':False,'enables':0,'phases':[]}
    async with ClientSession(headers={'Authorization':'Bearer '+os.environ[c['web'].get('token_env','SOARM_STATION_TOKEN')]}) as session:
        async def api(path,body=None):
            async with session.request('POST' if body is not None else 'GET','https://localhost:8443'+path,json=body,ssl=tls) as r:
                r.raise_for_status();return await r.json()
        def state():return local.call('snapshot')['arms'][a.arm]
        async def enable():
            s=state();r=await api('/api/permission',{'arm_id':a.arm,'controller_session':s['controller_session'],'operator_session_id':principal,'enable':True})
            assert r['accepted'],r;report['enables']+=1
        async def goal(mode,points=None):
            s=state();body={'arm_id':a.arm,'runtime_boot_id':s['runtime_boot_id'],'controller_session':s['controller_session'],
                 'operator_session_id':principal,'operation_type':'TRAJECTORY','mode':mode}
            if points is not None:body['points']=points
            return (await api('/api/operate',body))['request_id']
        async def wait(identifier):
            end=time.monotonic()+5
            while time.monotonic()<end:
                result=await api('/api/operations/'+identifier)
                if result['done']:return result['result']
                await asyncio.sleep(.025)
            raise TimeoutError('Physical operation did not finish')
        original=state();base=dict(original['positions']);g=original['gripper']
        assert joint['min_rad']<base[a.joint]-.01 and base[a.joint]+.06<joint['max_rad']
        def point(offset,seconds=.8):
            target=dict(base);target[a.joint]+=offset;return {'positions':target,'time_s':seconds}
        try:
            await enable()
            for label,offset in [('inline_out',.04),('inline_return',0.)]:
                result=await wait(await goal('INLINE',[point(offset)]));assert result['execution_completed'],result
                s=state();assert s['permission']
                report['phases'].append({'label':label,'permission_retained':True,'measured_joint_rad':s['positions'][a.joint]})
            assert report['enables']==1
            identifier=await goal('STREAM');sequence=0
            async def send(kind,revision=0,body=None,name=''):
                nonlocal sequence
                response=await api('/api/operations/'+identifier);f=response['feedback']
                if not f.get('stream_ready'):raise RuntimeError('Stream was not ready')
                frame={k:f[k] for k in ('runtime_boot_id','controller_session','operation_id','operation_epoch','window_id')}
                sequence+=1;frame.update(arm_id=a.arm,operator_session_id=principal,sequence=sequence,frame_kind=kind,
                    semantic_revision=revision,input_id=name,payload_kind='JOINT_TRAJECTORY' if kind=='COMMAND' else 'NONE')
                if body is not None:frame['body']=body
                await api('/api/stream',frame)
            start=time.monotonic();trace=[];second=False
            while time.monotonic()-start<3.5:
                elapsed=time.monotonic()-start
                response=await api('/api/operations/'+identifier)
                if response['done']:raise RuntimeError(response['result'])
                if not response['feedback'].get('stream_ready'):await asyncio.sleep(.02);continue
                revision=response['feedback'].get('last_accepted_semantic_revision',0)
                if revision<1:await send('COMMAND',1,{'points':[point(.04)]},'one')
                elif elapsed>.25 and revision<2:await send('COMMAND',2,{'points':[point(.06)]},'two');second=True
                else:await send('HEARTBEAT')
                s=state();trace.append({'time_ns':s['time_ns'],'joint_rad':s['positions'][a.joint],
                    'permission':s['permission'],'phase':s['phase'],'health':s['health']})
                await asyncio.sleep(.035)
            assert second and state()['permission']
            report['phases'].append({'label':'stream_replacement_and_endpoint','duration_s':time.monotonic()-start,
                'permission_retained':True,'measured_excursion_rad':max(x['joint_rad'] for x in trace)-min(x['joint_rad'] for x in trace)})
            await send('END');result=await wait(identifier);assert result['execution_completed'],result
            assert not state()['permission']
            await enable();result=await wait(await goal('INLINE',[point(0.,1.)]));assert result['execution_completed'],result
            report['phases'].append({'label':'restore_after_explicit_end','permission_retained':state()['permission']})
            report['trace']=trace;report['passed']=True
        finally:
            report['final_hold']=await api('/api/hold',{'arm_id':a.arm});await asyncio.sleep(.15)
            final=state();report['final']={k:final[k] for k in ('positions','gripper','permission','health','phase','measured_hz')}
            assert not final['permission'] and final['health']['torque']=='ON'
            local.close();Path(a.output).write_text(json.dumps(report,indent=2))
    print(json.dumps({k:v for k,v in report.items() if k!='trace'},indent=2))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--arm',required=True);p.add_argument('--joint',required=True)
    p.add_argument('--output',required=True);p.add_argument('--execute',action='store_true');args=p.parse_args()
    if not args.execute:p.error('Pass --execute for the already-authorized small physical confirmation')
    asyncio.run(run(args))
