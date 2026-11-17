"""One isolated capture owner per camera; immutable JPEGs and latest metadata."""
import ctypes
import json
import math
import multiprocessing
import os
from pathlib import Path
import signal
import sys
import threading
import time
import uuid
from .hardware import DeviceLock

METADATA_BYTES=16384
RETAINED_FRAMES=4
STARTUP_DISCARD_LIMIT=8


def boottime():
    return time.clock_gettime(time.CLOCK_BOOTTIME) if hasattr(time,'CLOCK_BOOTTIME') else time.monotonic()


def requested_mode(config):
    mock=config.get('backend')=='mock'
    mode={'width':config.get('width',320 if mock else 640),'height':config.get('height',240 if mock else 480),
        'fps':config.get('fps',30),'fourcc':config.get('fourcc','MJPG') if not mock else 'SIMULATED'}
    for key in ('width','height'):
        if type(mode[key])!=int or mode[key]<=0:raise ValueError('Camera '+key+' must be a positive integer')
    if isinstance(mode['fps'],bool) or not isinstance(mode['fps'],(int,float)) or not math.isfinite(mode['fps']) or mode['fps']<=0:
        raise ValueError('Camera fps must be finite and positive')
    if not mock and (not isinstance(mode['fourcc'],str) or len(mode['fourcc'])!=4):raise ValueError('Camera fourcc must have four characters')
    return mode


class LatestMetadata:
    """Fixed-size replacement slot, with no queue of old frame notifications."""
    def __init__(self,context):
        self.data=context.Array(ctypes.c_ubyte,METADATA_BYTES,lock=False)
        self.length=context.Value(ctypes.c_uint32,0,lock=False);self.lock=context.Lock()
    def write(self,value):
        encoded=json.dumps(value,allow_nan=False,separators=(',',':')).encode()
        if len(encoded)>METADATA_BYTES:raise ValueError('Camera metadata exceeds fixed slot')
        with self.lock:self.data[:len(encoded)]=encoded;self.length.value=len(encoded)
    def read(self):
        # If a killed writer held this lock, the supervisor replaces its slot.
        if not self.lock.acquire(False):return None
        try:
            length=self.length.value
            return json.loads(bytes(self.data[:length])) if 0<length<=METADATA_BYTES else None
        finally:self.lock.release()


class CaptureStop:
    """A process-shared flag; setting it cannot wait on a dying child's lock."""
    def __init__(self,context):self.value=context.Value(ctypes.c_bool,False,lock=False)
    def is_set(self):return self.value.value
    def set(self):self.value.value=True
    def wait(self,seconds):
        deadline=time.monotonic()+seconds
        while not self.is_set():
            remaining=deadline-time.monotonic()
            if remaining<=0:break
            time.sleep(min(.05,remaining))
        return self.is_set()


def _guard_parent(parent_pid):
    if sys.platform.startswith('linux'):
        if ctypes.CDLL(None).prctl(1,signal.SIGTERM)!=0:raise OSError('Cannot bind camera lifetime to worker')
        if os.getppid()!=parent_pid:raise RuntimeError('Camera parent exited before capture startup')


def _reported_number(cap,prop):
    try:value=float(cap.get(prop))
    except Exception:return None
    return value if math.isfinite(value) and value>0 else None


def _configure_capture(cap,cv2,state):
    mode=state['requested_mode'];results={}
    setters=[('fourcc',cv2.CAP_PROP_FOURCC,cv2.VideoWriter_fourcc(*mode['fourcc'])),
        ('width',cv2.CAP_PROP_FRAME_WIDTH,mode['width']),('height',cv2.CAP_PROP_FRAME_HEIGHT,mode['height']),
        ('fps',cv2.CAP_PROP_FPS,mode['fps']),('buffer_size',cv2.CAP_PROP_BUFFERSIZE,1)]
    for name,prop,value in setters:
        try:results[name]={'accepted':bool(cap.set(prop,value))}
        except Exception as exc:results[name]={'accepted':False,'error':str(exc)[:256]}
    state['setter_results']=results
    code=_reported_number(cap,cv2.CAP_PROP_FOURCC)
    fourcc=''.join(chr((int(code)>>(8*i))&255) for i in range(4)) if code is not None else None
    readback={'width':_reported_number(cap,cv2.CAP_PROP_FRAME_WIDTH),'height':_reported_number(cap,cv2.CAP_PROP_FRAME_HEIGHT),
        'fps':_reported_number(cap,cv2.CAP_PROP_FPS),'fourcc':fourcc}
    state['negotiated_mode']=readback
    state['warnings']=['Setter rejected: '+name for name,result in results.items() if not result['accepted']]
    for name in ('width','height','fps','fourcc'):
        if readback[name]!=mode[name]:state['warnings'].append('Driver reports '+name+'='+str(readback[name]))
    if fourcc is not None and fourcc!=mode['fourcc']:raise RuntimeError('Camera negotiated '+fourcc+' instead of requested '+mode['fourcc'])


def _capture(config,directory,lock_dir,sequence,stop,slot,parent_pid,cv2_module=None):
    """Only this child opens/releases the device, captures and encodes frames."""
    cap=None;lock=None;state={'id':config['id'],'sequence':sequence,'state':'STARTING','ready':False,'error':'',
        'requested_mode':requested_mode(config),'negotiated_mode':{},'actual_mode':{},'setter_results':{},
        'warnings':[],'capture_pid':os.getpid(),'discarded_startup_frames':0}
    directory=Path(directory);files=[];capture_id=uuid.uuid4().hex
    try:
        _guard_parent(parent_pid);slot.write(state)
        if cv2_module is None:
            import cv2 as cv2_module
        cv2=cv2_module
        if hasattr(cv2,'setNumThreads'):cv2.setNumThreads(1)
        lock=DeviceLock(lock_dir,config.get('device','mock-camera-'+config['id']))
        for old in [*directory.glob('frame-*.jpg'),*directory.glob('frame-*.tmp')]:old.unlink(missing_ok=True)
        if config.get('backend')!='mock':
            cap=cv2.VideoCapture(config['device'],cv2.CAP_V4L2)
            if not cap.isOpened():raise RuntimeError('Cannot open camera')
            _configure_capture(cap,cv2,state);slot.write(state)
        mode=state['requested_mode'];valid_seen=False
        while not stop.is_set():
            started=time.monotonic()
            if cap is None:
                import numpy as np
                image=np.zeros((mode['height'],mode['width'],3),dtype=np.uint8)
                cv2.putText(image,f"{config['id']} {sequence}",(10,50),cv2.FONT_HERSHEY_SIMPLEX,.7,(0,220,100),2);ok=True
            else:ok,image=cap.read()
            acquired=time.time_ns();received=boottime()
            if not ok or image is None:raise RuntimeError('Camera read failed')
            shape=image.shape
            if len(shape)!=3 or shape[2]!=3:raise RuntimeError('Camera did not decode a three-channel BGR frame')
            state['actual_mode']={'width':int(shape[1]),'height':int(shape[0])}
            if (shape[1],shape[0])!=(mode['width'],mode['height']):
                reason=f"Decoded frame {shape[1]}x{shape[0]} differs from requested {mode['width']}x{mode['height']}"
                if valid_seen or state['discarded_startup_frames']>=STARTUP_DISCARD_LIMIT:raise RuntimeError(reason)
                state.update(state='NEGOTIATING',error=reason);state['discarded_startup_frames']+=1;slot.write(state)
                continue # Drain stale startup buffers; never resize or publish them.
            valid_seen=True
            ok,data=cv2.imencode('.jpg',image,[cv2.IMWRITE_JPEG_QUALITY,config.get('jpeg_quality',75)])
            if not ok:raise RuntimeError('JPEG encode failed')
            sequence+=1;frame_path=directory/f'frame-{capture_id}-{sequence}.jpg'
            tmp=frame_path.with_suffix('.tmp');tmp.write_bytes(data.tobytes());os.replace(tmp,frame_path)
            files.append(frame_path)
            if len(files)>RETAINED_FRAMES:files.pop(0).unlink(missing_ok=True)
            state.update(sequence=sequence,time_ns=acquired,boottime=received,width=int(shape[1]),height=int(shape[0]),
                path=str(frame_path),frame_id=config.get('frame_id',config['id']+'_optical_frame'),state='READY',ready=True,error='')
            slot.write(state)
            if cap is None:stop.wait(max(0,1/mode['fps']-(time.monotonic()-started)))
    except Exception as exc:
        state.update(state='ERROR',ready=False,error=str(exc)[:512],path='');slot.write(state)
    finally:
        # release() can hang too; the supervisor can terminate this process.
        if cap is not None:cap.release()
        if lock is not None:lock.close()


class Camera:
    def __init__(self,config,directory,lock_dir,*,_capture_target=_capture):
        self.config=config;self.directory=Path(directory)/config['id'];self.directory.mkdir(parents=True,exist_ok=True)
        self.lock_dir=lock_dir;self.latest={};self.error='';self.sequence=0
        self.stop=threading.Event();self.lock=threading.RLock();self.process=None;self.restart_count=0
        self.context=multiprocessing.get_context('spawn');self.capture_target=_capture_target
        self.startup_timeout=self._seconds('startup_timeout_s',10.)
        self.frame_timeout=self._seconds('frame_timeout_s',max(3.,3/requested_mode(config)['fps']))
        self.retry_initial=self._seconds('retry_initial_s',1.);self.retry_max=self._seconds('retry_max_s',15.)
        if self.retry_max<self.retry_initial:raise ValueError('Camera retry_max_s must be >= retry_initial_s')
        self.state={'id':config['id'],'state':'STOPPED','ready':False,'requested_mode':requested_mode(config),
            'negotiated_mode':{},'actual_mode':{},'setter_results':{},'warnings':[]}
        self.thread=threading.Thread(target=self.run,daemon=True,name='camera-supervisor-'+config['id'])
    def _seconds(self,key,default):
        value=self.config.get(key,default)
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not .05<=value<=300:
            raise ValueError('Camera '+key+' must be between 0.05 and 300 seconds')
        return float(value)
    def start(self):self.thread.start()
    def _update(self,**values):
        with self.lock:self.state={**self.state,**values};self.error=self.state.get('error','')
    def _retire(self,process,child_stop):
        child_stop.set();process.join(.2)
        if process.is_alive():process.terminate();process.join(.5)
        if process.is_alive():process.kill();process.join(.5)
        return not process.is_alive()
    def run(self):
        try:self._supervise()
        except Exception as exc:self._update(state='ERROR',ready=False,error='Camera supervisor failed: '+str(exc)[:400])
    def _supervise(self):
        failures=0
        while not self.stop.is_set():
            slot=LatestMetadata(self.context);child_stop=CaptureStop(self.context)
            process=self.context.Process(target=self.capture_target,args=(self.config,str(self.directory),self.lock_dir,
                self.sequence,child_stop,slot,os.getpid()),daemon=True,name='camera-'+self.config['id'])
            self.process=process
            self._update(state='STARTING',ready=False,error='',capture_pid=None,negotiated_mode={},actual_mode={},
                setter_results={},warnings=[],discarded_startup_frames=0,retry_delay_s=0.)
            started=time.monotonic();last_frame=started;seen_sequence=self.sequence;ready_since=None
            try:
                process.start();self._update(capture_pid=process.pid)
                while not self.stop.is_set():
                    record=slot.read();current=time.monotonic()
                    if record:
                        with self.lock:
                            self.state={**self.state,**record};self.error=record.get('error','')
                            if record.get('ready') and record.get('sequence',0)>seen_sequence:
                                seen_sequence=record['sequence'];self.sequence=max(self.sequence,seen_sequence)
                                self.latest=dict(record);last_frame=current
                                if ready_since is None:ready_since=current
                        if record.get('state')=='ERROR':break
                    if not process.is_alive():
                        self._update(state='ERROR',ready=False,error=f'Capture process exited ({process.exitcode})');break
                    if ready_since is None and current-started>self.startup_timeout:
                        self._update(state='ERROR',ready=False,error='Camera startup timed out');break
                    if ready_since is not None and current-last_frame>self.frame_timeout:
                        self._update(state='ERROR',ready=False,error='Camera capture stopped producing frames');break
                    self.stop.wait(.01)
            except Exception as exc:self._update(state='ERROR',ready=False,error=str(exc)[:512])
            finally:
                exited=self._retire(process,child_stop) if process.pid is not None else True
                # Never overlap owners, even if the kernel cannot yet reap one.
                while not exited and not self.stop.is_set():
                    self._update(state='RECOVERY_BLOCKED',ready=False,error='Capture owner has not exited; replacement is waiting')
                    self.stop.wait(.1);exited=not process.is_alive()
                if exited:process.close();self.process=None;self._update(capture_pid=None)
            if self.stop.is_set():break
            failures=1 if ready_since is not None and time.monotonic()-ready_since>=2 else min(failures+1,16)
            delay=min(self.retry_max,self.retry_initial*2**(failures-1))
            self.restart_count+=1;self._update(state='RECOVERING',ready=False,retry_delay_s=delay);self.stop.wait(delay)
        self._update(state='STOPPED' if self.process is None else 'RECOVERY_BLOCKED',ready=False)
    def snapshot(self):
        with self.lock:
            result={**self.latest,**self.state,'id':self.config['id'],'sequence':self.sequence,
                'error':self.error,'restart_count':self.restart_count}
            ready=result.get('ready',False) and result.get('state')=='READY' and not self.stop.is_set()
            result['ready']=ready;result['age_ms']=max(0.,(boottime()-result.get('boottime',0))*1000) if ready else 1e9
            if not ready:result['path']=''
            return result
    def close(self):
        self.stop.set()
        if self.thread.ident is not None:self.thread.join(timeout=2.)
