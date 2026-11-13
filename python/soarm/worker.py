import argparse
import os
import signal
import socketserver
import threading
from pathlib import Path
from .config import load
from .control import ArmController,uid
from .camera import Camera
from .ipc import receive,send
from .hardware import DeviceLock

class Runtime:
    def __init__(self,c):
        self.config=c;self.boot=uid()
        self.cameras={x['id']:Camera(x,c['frame_dir'],c['lock_dir']) for x in c['cameras']}
        self.arms={x['id']:ArmController(x,c,self.boot,self.cameras) for x in c['arms']}
        self.recovery_stop=threading.Event()
        self.recovery=threading.Thread(target=self.recover,daemon=True)
    def start(self):
        self.recovery.start()
        for x in [*self.arms.values(),*self.cameras.values()]: x.start()
    def recover(self):
        from .hardware import make_arm
        while not self.recovery_stop.wait(2.):
            for n,cam in list(self.cameras.items()):
                if cam.error and not cam.thread.is_alive():
                    replacement=Camera(cam.config,self.config['frame_dir'],self.config['lock_dir'])
                    replacement.sequence=cam.sequence;self.cameras[n]=replacement;replacement.start()
            for arm in self.arms.values():
                if arm.phase=='FAULT' and not arm.thread.is_alive() and not arm.stop.is_set():
                    arm.backend=make_arm(arm.config);arm.permission=False;arm.op=None;arm.phase='HOLD'
                    arm.thread=threading.Thread(target=arm.run,daemon=True,name='arm-'+arm.id);arm.start()
    def close(self):
        self.recovery_stop.set();self.recovery.join(timeout=3)
        for x in self.arms.values(): x.close()
        for x in self.cameras.values(): x.close()
    def call(self,r):
        op=r['op']
        if op=='snapshot': return {'runtime_boot_id':self.boot,'config_revision':self.config['_revision'],
            'arms':{n:a.snapshot() for n,a in self.arms.items()},'cameras':{n:a.snapshot() for n,a in self.cameras.items()}}
        if r.get('arm_id') not in self.arms: raise ValueError('Unknown arm resource')
        a=self.arms[r['arm_id']]
        if op=='permission': return a.set_permission(r['enable'],r['principal'],r['controller_session'])
        if op=='begin': return a.begin(r['request'])
        if op=='input': return a.input(r['frame'])
        if op=='hold': return a.hold()
        if op=='feedback': return a.feedback()
        if op=='result':
            value=a.completed.get(r['operation_id'])
            return value if value and a.hold_event.is_set() else None
        raise ValueError('Unknown IPC operation')

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);args=parser.parse_args()
    if os.environ.get('SOARM_PARENT_PID'):
        import ctypes
        ctypes.CDLL(None).prctl(1,signal.SIGTERM)
        if os.getppid()!=int(os.environ['SOARM_PARENT_PID']):raise RuntimeError('Hardware owner parent exited')
    c=load(args.config);path=Path(c['socket']);path.parent.mkdir(parents=True,exist_ok=True)
    process_lock=DeviceLock(c['lock_dir'],str(path));path.unlink(missing_ok=True)
    runtime=Runtime(c)
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            while True:
                try:
                    r=receive(self.request)
                    try: response={'ok':True,'data':runtime.call(r)}
                    except Exception as exc: response={'ok':False,'error':str(exc)}
                    send(self.request,response)
                except (OSError,EOFError,ValueError): return
    class Server(socketserver.ThreadingUnixStreamServer): daemon_threads=True
    server=Server(str(path),Handler);os.chmod(path,0o600)
    runtime.start()
    def stop(*_): threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    try: server.serve_forever(poll_interval=.1)
    finally: runtime.close();server.server_close();path.unlink(missing_ok=True);process_lock.close()
if __name__=='__main__': main()
