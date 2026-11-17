"""Camera mode and process recovery tests: fake captures and simulated frames only."""
import json
import multiprocessing
import os
from pathlib import Path
import signal
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from soarm_station.camera import Camera,LatestMetadata,METADATA_BYTES,RETAINED_FRAMES,_capture,_guard_parent,boottime
from soarm_station.config import load
from soarm_station.control import ArmController
from soarm_station.hardware import DeviceLock


class FakeCV:
    CAP_V4L2=1;CAP_PROP_FOURCC=2;CAP_PROP_FRAME_WIDTH=3;CAP_PROP_FRAME_HEIGHT=4
    CAP_PROP_FPS=5;CAP_PROP_BUFFERSIZE=6;IMWRITE_JPEG_QUALITY=7
    def __init__(self,stop,shapes,*,failed=(),fourcc='MJPG',stop_after=1):
        self.stop=stop;self.shapes=list(shapes);self.failed=failed;self.fourcc=fourcc
        self.stop_after=stop_after;self.encoded=[];self.reads=0;self.released=False;self.setters=[]
    def VideoWriter_fourcc(self,*chars):return sum(ord(c)<<(8*i) for i,c in enumerate(chars))
    def VideoCapture(self,device,backend):self.device=device;return self
    def isOpened(self):return True
    def set(self,prop,value):self.setters.append((prop,value));return prop not in self.failed
    def get(self,prop):return {2:self.VideoWriter_fourcc(*self.fourcc),3:640.,4:480.,5:30.}.get(prop,1)
    def read(self):
        shape=self.shapes[min(self.reads,len(self.shapes)-1)];self.reads+=1
        return True,SimpleNamespace(shape=shape)
    def imencode(self,extension,image,settings):
        self.encoded.append(image.shape)
        if len(self.encoded)>=self.stop_after:self.stop.set()
        return True,SimpleNamespace(tobytes=lambda:b'simulated JPEG')
    def release(self):self.released=True


def capture_fake(tmp_path,monkeypatch,shapes,**options):
    monkeypatch.setattr('soarm_station.camera._guard_parent',lambda _:None)
    stop=threading.Event();cv=FakeCV(stop,shapes,**options)
    slot=LatestMetadata(multiprocessing.get_context('spawn'))
    directory=tmp_path/'frames';directory.mkdir()
    config={'id':'test','device':'/synthetic-camera','width':640,'height':480,'fps':30}
    _capture(config,str(directory),str(tmp_path/'locks'),0,stop,slot,os.getpid(),cv)
    return cv,slot.read(),directory


def test_misreported_decoded_mode_is_rejected_before_any_jpeg(tmp_path,monkeypatch):
    cv,status,directory=capture_fake(tmp_path,monkeypatch,[(2448,3264,3)])
    assert status['state']=='ERROR' and not status['ready'] and status['path']==''
    assert status['requested_mode']['width']==status['negotiated_mode']['width']==640
    assert status['actual_mode']=={'width':3264,'height':2448}
    assert '3264x2448' in status['error'] and cv.encoded==[] and cv.reads==9
    assert not list(directory.glob('*.jpg')) and cv.released


def test_old_mode_startup_frames_are_discarded_without_resizing(tmp_path,monkeypatch):
    cv,status,_=capture_fake(tmp_path,monkeypatch,[(2448,3264,3),(480,640,3)])
    assert status['ready'] and status['discarded_startup_frames']==1 and status['sequence']==1
    assert cv.encoded==[(480,640,3)] and (status['width'],status['height'])==(640,480)
    assert Path(status['path']).is_file()


def test_midstream_mode_change_invalidates_previous_frame(tmp_path,monkeypatch):
    cv,status,_=capture_fake(tmp_path,monkeypatch,[(480,640,3),(720,1280,3)],stop_after=2)
    assert cv.encoded==[(480,640,3)] and status['state']=='ERROR' and status['path']==''
    assert status['sequence']==1 and not status['ready']


def test_failed_setter_is_visible_even_when_decoded_mode_is_correct(tmp_path,monkeypatch):
    cv,status,_=capture_fake(tmp_path,monkeypatch,[(480,640,3)],failed=(3,6))
    assert status['ready'] and status['setter_results']['width']=={'accepted':False}
    assert status['setter_results']['buffer_size']=={'accepted':False}
    assert 'Setter rejected: width' in status['warnings'] and cv.released


def test_wrong_negotiated_format_fails_without_starting_frame_reads(tmp_path,monkeypatch):
    cv,status,_=capture_fake(tmp_path,monkeypatch,[(480,640,3)],fourcc='YUYV')
    assert status['state']=='ERROR' and 'YUYV' in status['error'] and cv.reads==0
    assert status['negotiated_mode']['fourcc']=='YUYV' and cv.released
    lock=DeviceLock(str(tmp_path/'locks'),'/synthetic-camera');lock.close()


def test_encoded_files_are_bounded_and_latest_metadata_replaces_history(tmp_path,monkeypatch):
    cv,status,directory=capture_fake(tmp_path,monkeypatch,[(480,640,3)],stop_after=20)
    assert status['sequence']==20 and len(list(directory.glob('*.jpg')))==RETAINED_FRAMES
    assert len(cv.encoded)==20 and not list(directory.glob('*.tmp'))
    slot=LatestMetadata(multiprocessing.get_context('spawn'))
    for i in range(500):slot.write({'sequence':i})
    assert slot.read()=={'sequence':499} and len(slot.data)==METADATA_BYTES
    with pytest.raises(ValueError,match='fixed slot'):slot.write({'large':'x'*METADATA_BYTES})
    slot.lock.acquire()
    try:
        started=time.monotonic();assert slot.read() is None
        assert time.monotonic()-started<.1
    finally:slot.lock.release()


def synthetic_owner(config,directory,lock_dir,sequence,stop,slot,parent_pid):
    """Real child process and flock; simulate a blocked backend without devices."""
    _guard_parent(parent_pid)
    lock=DeviceLock(lock_dir,'synthetic-'+config['id']);directory=Path(directory)
    attempts=directory/'attempts.jsonl'
    first=not attempts.exists()
    with attempts.open('a') as stream:stream.write(json.dumps({'pid':os.getpid(),'first':first})+'\n')
    mode=config.get('fault') if first else None
    def frame():
        nonlocal sequence
        sequence+=1;path=directory/'synthetic.jpg';path.write_bytes(b'not a real image')
        slot.write({'id':config['id'],'sequence':sequence,'time_ns':time.time_ns(),'boottime':boottime(),
            'width':64,'height':48,'path':str(path),'frame_id':config['id']+'_optical_frame',
            'state':'READY','ready':True,'error':'','capture_pid':os.getpid(),'actual_mode':{'width':64,'height':48}})
    try:
        if mode in ('read','release'):frame()
        if mode=='release':slot.write({'state':'ERROR','ready':False,'error':'Simulated failure before blocked release'})
        if mode=='metadata_lock':slot.lock.acquire()
        if mode:
            while True:time.sleep(.05) # Deliberately ignore the cooperative Event.
        while not stop.is_set():frame();stop.wait(.01)
    finally:lock.close()


def wait_for(predicate,timeout=5):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        value=predicate()
        if value:return value
        time.sleep(.01)
    raise AssertionError('Camera condition did not become ready')


def camera(tmp_path,identifier='one',fault=None,target=synthetic_owner):
    return Camera({'id':identifier,'backend':'mock','width':64,'height':48,'fps':30,'fault':fault,
        'startup_timeout_s':2.,'frame_timeout_s':.3,'retry_initial_s':.05,'retry_max_s':.1},
        str(tmp_path/'frames'),str(tmp_path/'locks'),_capture_target=target)


@pytest.mark.parametrize('fault',['open','read','release','metadata_lock'])
def test_blocked_capture_is_reaped_before_replacement_can_own_device(tmp_path,fault):
    cam=camera(tmp_path,fault=fault);cam.start()
    try:
        wait_for(lambda:cam.restart_count>=1 and cam.snapshot()['ready'],timeout=7)
        status=cam.snapshot();attempts=[json.loads(x) for x in (cam.directory/'attempts.jsonl').read_text().splitlines()]
        assert len(attempts)==2 and attempts[0]['pid']!=attempts[1]['pid']
        with pytest.raises(ProcessLookupError):os.kill(attempts[0]['pid'],0)
        assert status['capture_pid']==attempts[1]['pid'] and status['sequence']>0 and status['age_ms']<500
        if fault=='read':assert status['sequence']>=2
    finally:cam.close()
    assert not cam.thread.is_alive() and cam.process is None and not cam.snapshot()['ready']
    lock=DeviceLock(str(tmp_path/'locks'),'synthetic-one');lock.close()


def test_camera_recovery_does_not_restart_arm_or_other_camera(tmp_path,monkeypatch):
    monkeypatch.setattr('soarm_station.control.now',time.monotonic)
    config=load(Path(__file__).resolve().parents[1]/'config/mock.yaml');config['lock_dir']=str(tmp_path/'locks')
    sick=camera(tmp_path,'sick','read');healthy=camera(tmp_path,'healthy')
    arm=ArmController(config['arms'][0],config,'test-boot',{'sick':sick,'healthy':healthy})
    session=arm.session;sick.start();healthy.start();arm.start()
    try:
        wait_for(lambda:healthy.snapshot()['ready']);healthy_pid=healthy.snapshot()['capture_pid']
        before=arm.sample_sequence
        wait_for(lambda:sick.restart_count>=1 and sick.snapshot()['ready'])
        assert healthy.snapshot()['capture_pid']==healthy_pid and healthy.snapshot()['ready']
        assert arm.session==session and arm.thread.is_alive() and arm.sample_sequence>before
        assert not arm.permission and arm.backend.writes==0
    finally:
        sick.close();healthy.close();arm.close()


def test_actual_mock_jpegs_are_readable_and_close_has_no_child_leak(tmp_path):
    import cv2
    cam=camera(tmp_path,target=_capture);cam.start()
    try:
        wait_for(lambda:cam.snapshot().get('sequence',0)>=8)
        status=cam.snapshot();image=cv2.imread(status['path'])
        assert image.shape==(48,64,3) and status['ready']
        assert len(list(cam.directory.glob('frame-*.jpg')))<=RETAINED_FRAMES
        process=cam.process;pid=process.pid
    finally:cam.close()
    assert cam.process is None and not cam.thread.is_alive()
    with pytest.raises(ProcessLookupError):os.kill(pid,0)


def test_stopped_or_failed_snapshot_never_exposes_recent_old_path(tmp_path):
    cam=camera(tmp_path)
    cam.latest={'path':'old.jpg','boottime':boottime(),'width':64,'height':48}
    cam._update(state='RECOVERING',ready=False,error='read failed')
    result=cam.snapshot()
    assert result['path']=='' and result['age_ms']==1e9 and not result['ready']
    cam.close()


def orphan_test_parent(directory,connection):
    cam=camera(Path(directory));cam.start()
    try:
        wait_for(lambda:cam.snapshot()['ready'])
        connection.send(cam.snapshot()['capture_pid'])
        while True:time.sleep(.1)
    finally:cam.close()


@pytest.mark.skipif(not sys.platform.startswith('linux'),reason='Linux kernel parent-death signaling')
def test_worker_death_terminates_its_capture_owner(tmp_path):
    context=multiprocessing.get_context('spawn');reader,writer=context.Pipe(duplex=False)
    parent=context.Process(target=orphan_test_parent,args=(str(tmp_path),writer));parent.start();writer.close()
    child_pid=None
    def child_exited():
        try:return Path(f'/proc/{child_pid}/stat').read_text().split(') ',1)[1].split()[0]=='Z'
        except FileNotFoundError:return True
    try:
        assert reader.poll(8);child_pid=reader.recv()
        parent.kill();parent.join(2);assert not parent.is_alive()
        wait_for(child_exited,timeout=3)
        lock=DeviceLock(str(tmp_path/'locks'),'synthetic-one');lock.close()
    finally:
        if parent.is_alive():parent.kill();parent.join(2)
        if child_pid and not child_exited():os.kill(child_pid,signal.SIGKILL)
        reader.close();parent.close()
