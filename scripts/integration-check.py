#!/usr/bin/env python3
"""End-to-end mock control over the same authenticated HTTPS/WSS interface."""
import argparse,asyncio,json,math,os,ssl,statistics,time,uuid
from pathlib import Path
from aiohttp import ClientSession
async def main(a):
    context=ssl.create_default_context(cafile=a.ca)
    token=os.environ['SOARM_TOKEN'];principal='integration-'+uuid.uuid4().hex
    async with ClientSession(headers={'Authorization':'Bearer '+token}) as session:
        async def request(path,body=None):
            async with session.request('POST' if body is not None else 'GET',a.url+path,json=body,ssl=context) as r:
                r.raise_for_status();return await r.json()
        async with ClientSession() as anonymous:
            async with anonymous.get(a.url+'/api/status',ssl=context) as r:assert r.status==401
        state=await request('/api/status')
        # Test configuration explicitly selects the mock-only IDs; this script is never a real-arm exercise.
        assert state['device_id']=='linux-workstation', 'This integration test is only for the mock profile'
        assert set(state['arms'])=={'arm1','arm2'}
        arm='arm1';s=state['arms'][arm]
        assert not s['permission']
        async with session.get(a.url+'/api/cameras/forward.jpg',ssl=context) as r:assert r.status==200 and (await r.read())[:2]==b'\xff\xd8'
        permission=await request('/api/permission',{'arm_id':arm,'controller_session':s['controller_session'],'operator_session_id':principal,'enable':True})
        assert permission['accepted'],permission
        j=state['joints'][arm];initial={n.split('/')[-1]:p for n,p in zip(j['name'],j['position'])}
        grant={};sequence=0;sent={};latencies=[];received_states=0;accepted=set();start=time.monotonic()
        async with session.ws_connect(a.url+'/ws/control',ssl=context) as ws:
            await ws.send_json({'arm_id':arm,'runtime_boot_id':s['runtime_boot_id'],'controller_session':s['controller_session'],
                'operator_session_id':principal,'operation_type':'DIRECT','mode':'STREAM','duration_s':10})
            async def receive():
                nonlocal grant,received_states
                async for message in ws:
                    if message.type.value!=1:continue
                    d=json.loads(message.data)
                    if d['type']=='error':raise AssertionError(d)
                    if d['type']=='state':received_states+=1
                    if d['type']=='grant':
                        grant=d['grant'];identifier=grant.get('last_accepted_input_id')
                        if identifier in sent and identifier not in accepted:latencies.append((time.monotonic()-sent[identifier])*1000);accepted.add(identifier)
            receiver=asyncio.create_task(receive())
            try:
                while time.monotonic()-start<3:
                    if grant:
                        sequence+=1;identifier=uuid.uuid4().hex;sample_time=time.monotonic()
                        target={n:p+.03*math.sin((sample_time-start)*2) for n,p in initial.items()}
                        sent[identifier]=sample_time
                        await ws.send_json({**grant,'arm_id':arm,'frame_kind':'COMMAND','sequence':sequence,'input_id':identifier,
                            'body':{'positions':target,'gripper':s['gripper_percent'],'gripper_unit':'percent'}})
                    if receiver.done():await receiver
                    await asyncio.sleep(.01)
            finally:receiver.cancel()
        await asyncio.sleep(.8);after=await request('/api/status')
        assert after['arms'][arm]['phase']=='HOLD' and not after['arms'][arm]['permission'],after['arms'][arm]
        assert after['arms']['arm2']['phase']=='IDLE' and not after['arms']['arm2']['permission']
        h=await request('/api/history?arm=arm1&limit=500');assert h['records'] and any(r['sample']['valid'] for r in h['records'])
        await request('/api/permission',{'arm_id':arm,'controller_session':s['controller_session'],'operator_session_id':principal,'enable':True})
        current=after['joints'][arm];q={n.split('/')[-1]:p+.02 for n,p in zip(current['name'],current['position'])}
        result=await request('/api/operate',{'arm_id':arm,'runtime_boot_id':s['runtime_boot_id'],'controller_session':s['controller_session'],
            'operator_session_id':principal,'operation_type':'DIRECT','mode':'INLINE','duration_s':2,'points':[{'positions':q,'gripper':s['gripper_percent'],'time_s':.25}]})
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            outcome=await request('/api/operations/'+result['request_id'])
            if outcome['done']:break
            await asyncio.sleep(.05)
        assert outcome['result']['execution_completed'],outcome
        values=sorted(latencies)
        def percentile(p):return values[min(len(values)-1,int((len(values)-1)*p))] if values else None
        report={'mock_arms':2,'mock_cameras':3,'sent_commands':sequence,'acknowledgements_observed':len(accepted),
            'ack_ms':{'p50':percentile(.5),'p95':percentile(.95),'p99':percentile(.99),'max':max(values) if values else None},
            'state_frames':received_states,'disconnect_latched_hold':True,'independent_arm_unchanged':True,
            'inline_completed':True,'anonymous_rejected':True,'durable_history_records':len(h['records']),
            'note':'ACK feedback is decimated; observed ACK count is not a packet loss metric. Timing uses one client clock.'}
        assert len(accepted)>5,report
        if a.output:Path(a.output).write_text(json.dumps(report,indent=2))
        print(json.dumps(report,indent=2))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--url',default='https://localhost:8443');p.add_argument('--ca',required=True);p.add_argument('--output');asyncio.run(main(p.parse_args()))
