"""Non-ROS remote leader/JSON operator. Samples only after a host window arrives."""
import argparse
import asyncio
import json
import os
import ssl
import sys
import time
import uuid
from aiohttp import ClientSession

async def run(args):
    context=ssl.create_default_context(cafile=args.ca)
    token=os.environ.get('SOARM_STATION_TOKEN','')
    headers={'Authorization':'Bearer '+token} if token else {}
    principal=uuid.uuid4().hex;grant=None;sequence=0;leader=None;owned_operation=None
    if args.leader:
        import draccus,yaml
        from lerobot.teleoperators.config import TeleoperatorConfig
        from lerobot.teleoperators.utils import make_teleoperator_from_config
        config=draccus.decode(TeleoperatorConfig,yaml.safe_load(open(args.leader)))
        leader=make_teleoperator_from_config(config);leader.connect(calibrate=False)
    async with ClientSession(headers=headers) as session:
        async with session.get(args.url+'/api/status',ssl=context) as r:
            r.raise_for_status();status=await r.json()
        state=status['arms'][args.arm]
        body={'arm_id':args.arm,'controller_session':state['controller_session'],'operator_session_id':principal,'enable':True}
        try:
            async with session.post(args.url+'/api/permission',json=body,ssl=context) as r:
                r.raise_for_status();permission=await r.json()
                if not permission['accepted']:raise RuntimeError(permission['reason'])
            async with session.ws_connect(args.url+'/ws/control',ssl=context,max_msg_size=262144) as ws:
                await ws.send_json({**body,'runtime_boot_id':state['runtime_boot_id'],'operation_type':'DIRECT','mode':'STREAM',
                    'input_semantics':'persistent_target','duration_s':args.seconds})
                deadline=None if args.seconds is None else time.monotonic()+args.seconds
                async def receive():
                    nonlocal grant,owned_operation
                    async for m in ws:
                        if m.type.value==1:
                            d=json.loads(m.data)
                            if d['type']=='grant':
                                grant=d['grant'];owned_operation=grant.get('operation_id') or owned_operation
                            elif d['type']=='error':raise RuntimeError(d['reason'])
                receiver=asyncio.create_task(receive())
                try:
                    while deadline is None or time.monotonic()<deadline:
                        if receiver.done():await receiver;break
                        start=time.monotonic()
                        if grant:
                            # Never read a cached pose file and rewrap it in a fresh grant.
                            if leader:
                                import math
                                sample=leader.get_action();positions={n.removesuffix('.pos'):math.radians(v) for n,v in sample.items() if n!='gripper.pos'}
                                target={'positions':positions,'gripper':sample['gripper.pos'],'gripper_unit':'percent'}
                            elif args.hold_measured:
                                # This explicit test input holds the latest measured follower pose.
                                async with session.get(args.url+'/api/status',ssl=context) as r:current=await r.json()
                                joint=current['joints'][args.arm]
                                target={'positions':{n.split('/')[-1]:v for n,v in zip(joint['name'],joint['position'])},'gripper':current['arms'][args.arm]['gripper_percent']}
                            else:
                                line=await asyncio.to_thread(sys.stdin.readline)
                                if not line:break
                                target=json.loads(line)
                            sequence+=1
                            await ws.send_json({**grant,'arm_id':args.arm,'frame_kind':'COMMAND','sequence':sequence,'input_id':uuid.uuid4().hex,'body':target})
                        if receiver.done():await receiver
                        await asyncio.sleep(max(0,1/args.hz-(time.monotonic()-start)))
                finally:
                    receiver.cancel()
                    try:await receiver
                    except asyncio.CancelledError:pass
        finally:
            try:
                async with session.post(args.url+'/api/hold',json={'arm_id':args.arm,
                    'controller_session':state['controller_session'],'operator_session_id':principal,
                    'operation_id':owned_operation},ssl=context):pass
            finally:
                if leader:leader.disconnect()
def main():
    p=argparse.ArgumentParser();p.add_argument('--url',required=True);p.add_argument('--ca',required=True);p.add_argument('--arm',required=True)
    p.add_argument('--leader');p.add_argument('--hold-measured',action='store_true');p.add_argument('--hz',type=float,default=100);p.add_argument('--seconds',type=float,default=None,help='Optional explicit session limit; default runs until stopped')
    a=p.parse_args()
    if not 50<=a.hz<=120:p.error('Remote input rate must be 50..120 Hz')
    asyncio.run(run(a))
if __name__=='__main__':main()
