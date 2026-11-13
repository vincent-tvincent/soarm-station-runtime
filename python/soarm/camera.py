"""One capture and one JPEG encoding per source frame, shared through tmpfs."""
import os
import threading
import time
from pathlib import Path
from .hardware import DeviceLock

class Camera:
    def __init__(self,config,directory,lock_dir):
        self.config=config; self.directory=Path(directory); self.directory.mkdir(parents=True,exist_ok=True)
        self.path=self.directory/(config['id']+'.jpg')
        self.lock_dir=lock_dir; self.latest={}; self.error=''; self.stop=threading.Event(); self.sequence=0
        self.thread=threading.Thread(target=self.run,daemon=True,name='camera-'+config['id'])
    def start(self): self.thread.start()
    def run(self):
        import cv2
        import numpy as np
        cap=None; lock=None; c=self.config
        try:
            if c.get('backend')!='mock':
                lock=DeviceLock(self.lock_dir,c['device'])
                cap=cv2.VideoCapture(c['device'],cv2.CAP_V4L2)
                cap.set(cv2.CAP_PROP_FOURCC,cv2.VideoWriter_fourcc(*c.get('fourcc','MJPG')))
                for prop,key,default in [(cv2.CAP_PROP_FRAME_WIDTH,'width',640),(cv2.CAP_PROP_FRAME_HEIGHT,'height',480),(cv2.CAP_PROP_FPS,'fps',30)]:
                    cap.set(prop,c.get(key,default))
                cap.set(cv2.CAP_PROP_BUFFERSIZE,1)
                if not cap.isOpened(): raise RuntimeError('Cannot open camera')
            while not self.stop.is_set():
                start=time.monotonic()
                if cap is None:
                    image=np.zeros((c.get('height',240),c.get('width',320),3),dtype=np.uint8)
                    cv2.putText(image,f"{c['id']} {self.sequence}",(10,50),cv2.FONT_HERSHEY_SIMPLEX,.7,(0,220,100),2)
                    ok=True
                else: ok,image=cap.read()
                acquired=time.time_ns(); boottime=time.clock_gettime(time.CLOCK_BOOTTIME)
                if not ok: raise RuntimeError('Camera read failed')
                ok,data=cv2.imencode('.jpg',image,[cv2.IMWRITE_JPEG_QUALITY,c.get('jpeg_quality',75)])
                if not ok: raise RuntimeError('JPEG encode failed')
                frame_path=self.directory/(c['id']+'-'+str(self.sequence+1)+'.jpg')
                tmp=self.path.with_suffix('.tmp'); tmp.write_bytes(data.tobytes()); os.replace(tmp,frame_path)
                (self.directory/(c['id']+'-'+str(self.sequence-3)+'.jpg')).unlink(missing_ok=True)
                self.sequence+=1
                self.latest={'id':c['id'],'sequence':self.sequence,'time_ns':acquired,'boottime':boottime,
                             'width':image.shape[1],'height':image.shape[0],'path':str(frame_path),
                             'frame_id':c.get('frame_id',c['id']+'_optical_frame'),'error':''}
                if cap is None: self.stop.wait(max(0,1/c.get('fps',30)-(time.monotonic()-start)))
        except Exception as exc: self.error=str(exc)
        finally:
            if cap is not None: cap.release()
            if lock is not None: lock.close()
    def snapshot(self):
        return {**self.latest,'id':self.config['id'],'error':self.error,
                'age_ms':(time.clock_gettime(time.CLOCK_BOOTTIME)-self.latest.get('boottime',0))*1000}
    def close(self): self.stop.set(); self.thread.join(timeout=2)
