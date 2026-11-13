#!/usr/bin/env python3
"""Synthetic local/native-loopback policy qualification with a mock arm only."""
import argparse
import copy
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
import numpy as np
import cv2
from soarm_station.config import load
from soarm_station.control import ArmController
from soarm_station.policy import verify_bundle

def exercise(root,bundle,backend):
    # Import the pinned native API before starting the fake high-rate plant.
    from lerobot.inference import SyncInferenceEngine
    profile=verify_bundle(bundle)
    profile.update(backend=backend,bundle=bundle,device='cpu',startup_timeout_s=30.,timeout_s=5.)
    if backend=='remote':profile['remote']={'endpoint':'tcp/127.0.0.1:7447','deployment':'synthetic-act','semantics':'synthetic-fixture-v1'}
    with tempfile.TemporaryDirectory() as temporary:
        directory=Path(temporary);image=directory/'frame.jpg'
        class Camera:
            sequence=0
            def snapshot(self):
                cv2.imwrite(str(image),np.zeros((64,64,3),dtype=np.uint8));self.sequence+=1
                return {'path':str(image),'sequence':self.sequence,'age_ms':0.,'boottime':time.clock_gettime(time.CLOCK_BOOTTIME)}
        c=load(root/'config/mock.yaml');c['lock_dir']=str(directory/'locks');c['profiles']={'synthetic':profile}
        cfg=c['arms'][0];cfg['calibration_revision']=profile['calibration_revision']
        arm=ArmController(cfg,c,'synthetic-test',{'forward':Camera()});arm.backend.gripper=0.;arm.start()
        end=time.monotonic()+3
        while not arm.q and time.monotonic()<end:time.sleep(.01)
        arm.set_permission(True,'test',arm.session)
        arm.begin({'principal':'test','runtime_boot_id':arm.boot,'controller_session':arm.session,
                   'operation_type':'MODEL','mode':'STREAM','profile_id':'synthetic','instruction':''})
        start=time.monotonic();states=set();updated=False;first_policy=None
        try:
            while time.monotonic()-start<15:
                with arm.lock:
                    if arm.phase=='FAULT' or not arm.op:raise RuntimeError(arm.error)
                    g=arm.feedback()
                    frame={k:g[k] for k in ('runtime_boot_id','controller_session','operation_id','operation_epoch','window_id')}
                    frame.update(principal='test',sequence=arm.last_sequence+1,frame_kind='HEARTBEAT',body={})
                    arm.input(frame);states.add(arm.model_state)
                    if arm.model and arm.model.actions>=4 and not updated:
                        first_policy=id(getattr(arm.model.session,'policy',arm.model.session))
                        frame.update(sequence=arm.last_sequence+1,frame_kind='COMMAND',semantic_revision=1,input_id='task-reset',body={'instruction':''})
                        arm.input(frame);updated=True
                    if updated and arm.model.resets>=2 and arm.model.actions>=8:break
                time.sleep(.02)
            assert updated and arm.model.loads==1 and arm.model.resets>=2 and arm.model.actions>=8
            assert id(getattr(arm.model.session,'policy',arm.model.session))==first_policy
            report={'backend':backend,'duration_s':time.monotonic()-start,'loads':arm.model.loads,'task_resets':arm.model.resets,
                    'native_actions':arm.model.actions,'wait_ticks':arm.model.wait_ticks,'native_holds':arm.model.holds,
                    'states':sorted(states),'mock_arm_goal_writes':arm.backend.writes,'physical_actuation':False,'passed':True}
        finally:
            arm.hold('TEST_FINISHED');session=arm.model.session if arm.model else None;arm.close()
            if session is not None:session.close()
        return report

def main():
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--bundle',required=True)
    p.add_argument('--server-config',required=True);p.add_argument('--native-python',required=True);p.add_argument('--output',required=True);a=p.parse_args()
    reports=[exercise(Path(a.root),a.bundle,'local')]
    log=open('/tmp/soarm-station-continuity-native-server.log','w')
    server=subprocess.Popen([a.native_python,'-m','lerobot.scripts.lerobot_policy_server','--config_path='+a.server_config],stdout=log,stderr=subprocess.STDOUT)
    try:
        time.sleep(4.)
        if server.poll() is not None:raise RuntimeError('Synthetic native server failed; inspect /tmp/soarm-station-continuity-native-server.log')
        reports.append(exercise(Path(a.root),a.bundle,'remote'))
    finally:
        server.terminate()
        try:server.wait(timeout=5)
        except subprocess.TimeoutExpired:server.kill();server.wait()
        log.close()
    Path(a.output).write_text(json.dumps(reports,indent=2));print(json.dumps(reports,indent=2))
if __name__=='__main__':main()
