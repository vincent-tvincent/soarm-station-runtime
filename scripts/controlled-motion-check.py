#!/usr/bin/env python3
"""Explicit, bounded real-arm qualification through the production HTTPS API."""
import argparse
import asyncio
import json
import math
import os
import ssl
import statistics
import threading
import time
import uuid
from pathlib import Path

from aiohttp import ClientSession
from soarm.config import load
from soarm.ipc import Client


class Sampler:
    def __init__(self,config,arm):
        self.client=Client(config['socket']);self.arm=arm;self.samples=[];self.stop=threading.Event()
        self.thread=threading.Thread(target=self.run,daemon=True)
    def run(self):
        deadline=time.monotonic();last=-1
        while not self.stop.is_set():
            try:
                s=self.client.call('snapshot')['arms'][self.arm]
                if s['sequence']!=last:
                    last=s['sequence'];self.samples.append({k:s[k] for k in ('time_ns','sequence','positions','gripper','phase','permission','health','age_ms','error')})
            except Exception as exc:self.error=str(exc)
            deadline+=.01
            if deadline<time.monotonic():deadline=time.monotonic()
            self.stop.wait(max(0,deadline-time.monotonic()))
    def close(self):self.stop.set();self.thread.join(timeout=3);self.client.close()


async def exercise(args):
    c=load(args.config);config=next(a for a in c['arms'] if a['id']==args.arm)
    if config['backend']!='so101' or config.get('read_only'):raise ValueError('An explicit writable SO101 profile is required')
    tls=ssl.create_default_context(cafile=str(Path(c['_path']).parent/c['web']['cert']))
    token=os.environ[c['web'].get('token_env','SOARM_TOKEN')]
    principal='physical-check-'+uuid.uuid4().hex
    report={'arm_id':args.arm,'kind':'controlled_native_runtime_motion','phases':[],'passed':False}
    sampler=Sampler(c,args.arm);sampler.thread.start()
    async with ClientSession(headers={'Authorization':'Bearer '+token}) as session:
        async def api(path,body=None):
            async with session.request('POST' if body is not None else 'GET',args.url+path,json=body,ssl=tls) as response:
                response.raise_for_status();return await response.json()
        async def measured():
            state=await api('/api/status');s=state['arms'][args.arm];j=state['joints'][args.arm]
            if not s['feedback_valid'] or s['phase']=='FAULT':raise RuntimeError('Feedback is unavailable: '+s['fault'])
            return s,{n.split('/')[-1]:v for n,v in zip(j['name'],j['position'])}
        async def enable():
            s,_=await measured()
            permission=await api('/api/permission',{'arm_id':args.arm,'controller_session':s['controller_session'],'operator_session_id':principal,'enable':True})
            if not permission['accepted']:raise RuntimeError(permission['reason'])
            return s
        async def path(label,points,operation_type='DIRECT',cancel_after=None):
            s=await enable();index=len(sampler.samples);start=time.monotonic()
            goal={'arm_id':args.arm,'runtime_boot_id':s['runtime_boot_id'],'controller_session':s['controller_session'],
                  'operator_session_id':principal,'operation_type':operation_type,'mode':'INLINE',
                  'duration_s':max(5.,points[-1]['time_s']+3),'points':points}
            request=await api('/api/operate',goal);outcome=None;stop_result=None;deadline=time.monotonic()+goal['duration_s']+2
            while time.monotonic()<deadline:
                outcome=await api('/api/operations/'+request['request_id'])
                state,_=await measured()
                if state['phase']=='FAULT':raise RuntimeError(state['fault'])
                if cancel_after is not None and time.monotonic()-start>=cancel_after and stop_result is None:
                    before,_=await measured();hold_started=time.monotonic()
                    stop_result=await api('/api/hold',{'arm_id':args.arm})
                    hold_elapsed=(time.monotonic()-hold_started)*1000
                    if not stop_result['success']:raise RuntimeError('Hold failed: '+stop_result['message'])
                if outcome['done']:break
                await asyncio.sleep(.03)
            if not outcome or not outcome['done']:raise TimeoutError(label+' did not finish')
            result=outcome['result']
            if cancel_after is None and not result['execution_completed']:raise RuntimeError(label+': '+result['reason'])
            await asyncio.sleep(.35)
            end,q=await measured();trace=sampler.samples[index:]
            phase={'label':label,'duration_s':time.monotonic()-start,'result':result,'measured_end_rad':q,
                   'gripper_end_percent':end['gripper_percent'],'samples':len(trace),
                   'max_excursion_rad':{n:max(abs(v['positions'][n]-trace[0]['positions'][n]) for v in trace) if trace else 0. for n in q}}
            if cancel_after is None:
                phase['endpoint_error_rad']={n:abs(q[n]-points[-1]['positions'][n]) for n in q}
                phase['gripper_error_percent']=abs(end['gripper_percent']-points[-1]['gripper'])
                if any(phase['endpoint_error_rad'][j['name']]>j.get('goal_tolerance_rad',.03)+.01 for j in config['joints']):raise RuntimeError(label+' settled error exceeds the declared joint tolerance plus 0.01 rad sampling margin')
                if phase['gripper_error_percent']>4:raise RuntimeError(label+' gripper settled error exceeds 4 percent')
            else:
                phase['hold_response_ms']=hold_elapsed;phase['hold_success']=stop_result['success']
                if end['permission']:raise RuntimeError('Permission survived hold')
            report['phases'].append(phase)
            print(label+': '+result['result_code']+'; torque='+end['measured_torque'],flush=True)
            return q,end['gripper_percent']
        try:
            baseline,initial=await measured();gripper=baseline['gripper_percent'];report['initial_positions_rad']=initial;report['initial_gripper_percent']=gripper
            await path('measured_pose_arm',[{'positions':initial,'gripper':gripper,'time_s':.35}])
            state,_=await measured()
            if state['measured_torque']!='ON':raise RuntimeError('All-motor torque-on was not confirmed')
            for joint in config['joints']:
                name=joint['name']
                if args.joint and name not in args.joint:continue
                delta=args.amplitude_rad
                if initial[name]+delta>joint['max_rad']-.015:delta=-delta
                if not joint['min_rad']+.015<=initial[name]+delta<=joint['max_rad']-.015:raise ValueError('No configured margin for '+name)
                target=dict(initial);target[name]+=delta
                await path(name+'_out',[{'positions':target,'gripper':gripper,'time_s':.8}])
                await path(name+'_return',[{'positions':initial,'gripper':gripper,'time_s':.8}])
            if args.joint:
                report['passed']=True
                return
            opened=min(90.,gripper+10.)
            await path('gripper_open',[{'positions':initial,'gripper':opened,'time_s':.8}])
            await path('gripper_return',[{'positions':initial,'gripper':gripper,'time_s':.8}])
            coordinated=dict(initial)
            coordinated['shoulder_pan']+=.12;coordinated['shoulder_lift']+=.10;coordinated['elbow_flex']-=.10
            await path('coordinated_joint_trajectory',[{'positions':coordinated,'gripper':gripper,'time_s':1.2},
                {'positions':initial,'gripper':gripper,'time_s':2.4}],operation_type='TRAJECTORY')
            farther=dict(initial);farther['shoulder_pan']+=.16
            await path('hold_during_trajectory',[{'positions':farther,'gripper':gripper,'time_s':3.}],cancel_after=.5)
            await path('restore_baseline',[{'positions':initial,'gripper':gripper,'time_s':1.}])
            report['passed']=True
        finally:
            try:report['cleanup_hold']=await api('/api/hold',{'arm_id':args.arm})
            finally:
                await asyncio.sleep(.2);sampler.close()
                report['samples']=sampler.samples
                stamps=[s['time_ns'] for s in sampler.samples];gaps=[(b-a)/1e6 for a,b in zip(stamps,stamps[1:])]
                report['feedback_observed_hz']=(len(stamps)-1)/((stamps[-1]-stamps[0])/1e9) if len(stamps)>1 else 0.
                report['max_observed_feedback_gap_ms']=max(gaps) if gaps else None
                Path(args.output).write_text(json.dumps(report,indent=2))
    print(json.dumps({k:v for k,v in report.items() if k!='samples'},indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--arm',required=True)
    p.add_argument('--url',default='https://localhost:8443');p.add_argument('--output',required=True)
    p.add_argument('--joint',action='append');p.add_argument('--amplitude-rad',type=float,default=.08);p.add_argument('--execute',action='store_true');args=p.parse_args()
    if not args.execute:p.error('Pass --execute for the explicitly authorized physical test')
    if not .03<=args.amplitude_rad<=.15:p.error('Qualification amplitude must be 0.03..0.15 rad')
    asyncio.run(exercise(args))
