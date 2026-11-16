import argparse
import logging
import os
import signal
import socketserver
import threading
from pathlib import Path
from dataclasses import dataclass,field
from .config import load
from .control import ArmController,uid
from .camera import Camera
from .ipc import receive,send
from .hardware import DeviceLock
from .policy_runtime import PolicyRuntime
from .policy_common import PolicyError


@dataclass(frozen=True)
class ControlOwnerBinding:
    """One connection owns one admitted operation, never its future replacement."""
    arm: object=field(repr=False)
    controller_session: str
    principal: str
    operation_id: str

    def close(self):
        return self.arm.hold_owned(self.controller_session,self.principal,self.operation_id,
            reason='CONTROL_OWNER_DISCONNECTED')


class RuntimeRequestHandler(socketserver.BaseRequestHandler):
    def handle(self):
        binding=None
        try:
            # A bound owner connection deliberately has no idle deadline. Its
            # lifetime, not periodic messages, owns the persistent operation.
            while True:
                try:
                    request=receive(self.request)
                    try:
                        if request.get('op')=='bind_control_owner':
                            candidate=self.server.runtime.bind_control_owner(request)
                            if binding is not None and binding!=candidate:
                                raise ValueError('This connection is already bound to another control operation')
                            binding=candidate  # Install before acknowledgement, including failed sends.
                            data={'accepted':True,'arm_id':binding.arm.id,
                                'controller_session':binding.controller_session,'operation_id':binding.operation_id}
                        else:data=self.server.runtime.call(request)
                        response={'ok':True,'data':data}
                    except PolicyError as exc:
                        response={'ok':False,'error':exc.message,'code':exc.code,'status':exc.status,'details':exc.details}
                    except Exception as exc:response={'ok':False,'error':str(exc)}
                    send(self.request,response)
                except (OSError,EOFError,ValueError):return
        finally:
            if binding is not None:
                try:binding.close()
                except Exception:
                    logging.getLogger(__name__).exception('Control owner cleanup failed for arm %s',binding.arm.id)


class RuntimeIPCServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads=True

    def __init__(self,path,runtime):
        self.runtime=runtime
        super().__init__(path,RuntimeRequestHandler)

class Runtime:
    def __init__(self,c):
        self.config=c;self.boot=uid()
        self.cameras={x['id']:Camera(x,c['frame_dir'],c['lock_dir']) for x in c['cameras']}
        self.arms={x['id']:ArmController(x,c,self.boot,self.cameras) for x in c['arms']}
        self.policy_runtime=PolicyRuntime(self)
        self.recovery_stop=threading.Event()
        self.recovery=threading.Thread(target=self.recover,daemon=True)
    def start(self):
        self.recovery.start()
        for x in [*self.arms.values(),*self.cameras.values()]: x.start()
        self.policy_runtime.start()
    def recover(self):
        from .hardware import make_arm
        while not self.recovery_stop.wait(2.):
            for n,cam in list(self.cameras.items()):
                if cam.error and not cam.thread.is_alive():
                    replacement=Camera(cam.config,self.config['frame_dir'],self.config['lock_dir'])
                    replacement.sequence=cam.sequence;self.cameras[n]=replacement;replacement.start()
            for arm in self.arms.values():
                if arm.phase=='FAULT' and not arm.thread.is_alive() and not arm.stop.is_set():
                    with arm.lock:
                        arm.backend=make_arm(arm.config);arm.permission=False;arm.op=None;arm.phase='HOLD'
                        arm.session=uid();arm.last_write=0.;arm.commanded_q=None;arm.commanded_gripper=None
                        arm.hold_pending=False;arm.hold_event.set()
                    arm.thread=threading.Thread(target=arm.run,daemon=True,name='arm-'+arm.id);arm.start()
    def close(self):
        self.policy_runtime.close()
        self.recovery_stop.set();self.recovery.join(timeout=3)
        for x in self.arms.values(): x.close()
        for x in self.cameras.values(): x.close()
    def bind_control_owner(self,request):
        for key in ('arm_id','controller_session','principal','operation_id'):
            if not isinstance(request.get(key),str) or not request[key]:
                raise ValueError(f'Control owner binding requires {key}')
        arm=self.arms.get(request['arm_id'])
        if arm is None:raise ValueError('Unknown arm resource')
        with arm.lock:
            op=arm.op
            if (not arm.permission or arm.hold_pending or arm.session!=request['controller_session']
                    or arm.principal!=request['principal'] or not op
                    or op.get('operation_id')!=request['operation_id']):
                raise ValueError('Control operation ownership changed or is unavailable')
            if op.get('persistent_target') is not True or op.get('operation_type')!='DIRECT' or op.get('mode')!='STREAM':
                raise ValueError('Control owner binding requires a persistent DIRECT STREAM operation')
            return ControlOwnerBinding(arm,arm.session,arm.principal,op['operation_id'])
    def call(self,r):
        op=r['op']
        if op=='policy_capabilities':return {'runtime_boot_id':self.boot,'local_prepare':True,'remote_inspect':True}
        if op=='policy_runtime':return self.policy_runtime.snapshot()
        if op=='policy_job':return self.policy_runtime.job(r['job_id'])
        if op=='policy_inspect':return self.policy_runtime.inspect(r['execution'],r.get('settings'))
        if op=='policy_prepare':return self.policy_runtime.prepare(**{k:r[k] for k in ('document','profile_revision','runtime_profile','mode','source_revision') if k in r})
        if op=='snapshot': return {'runtime_boot_id':self.boot,'config_revision':self.config['_revision'],
            'arms':{n:a.snapshot() for n,a in self.arms.items()},'cameras':{n:a.snapshot() for n,a in self.cameras.items()}}
        if r.get('arm_id') not in self.arms: raise ValueError('Unknown arm resource')
        a=self.arms[r['arm_id']]
        if op=='permission': return a.set_permission(r['enable'],r['principal'],r['controller_session'])
        if op=='set_torque': return a.set_torque(r['enabled'],r['expected_controller_session'])
        if op=='begin': return self.policy_runtime.begin(a,r['request'])
        if op=='input': return a.input(r['frame'])
        if op=='hold': return a.hold()
        if op=='hold_owned': return a.hold_owned(r['controller_session'],r['principal'],r.get('operation_id'))
        if op=='feedback': return a.feedback()
        if op=='result':
            value=a.completed.get(r['operation_id'])
            return value if r['operation_id'] in a.result_ready else None
        raise ValueError('Unknown IPC operation')

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);args=parser.parse_args()
    if os.environ.get('SOARM_STATION_PARENT_PID'):
        import ctypes
        ctypes.CDLL(None).prctl(1,signal.SIGTERM)
        if os.getppid()!=int(os.environ['SOARM_STATION_PARENT_PID']):raise RuntimeError('Hardware owner parent exited')
    c=load(args.config);path=Path(c['socket']);path.parent.mkdir(parents=True,exist_ok=True)
    process_lock=DeviceLock(c['lock_dir'],str(path));path.unlink(missing_ok=True)
    runtime=Runtime(c)
    server=RuntimeIPCServer(str(path),runtime);os.chmod(path,0o600)
    runtime.start()
    def stop(*_): threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    try: server.serve_forever(poll_interval=.1)
    finally: runtime.close();server.server_close();path.unlink(missing_ok=True);process_lock.close()
if __name__=='__main__': main()
