import hashlib
import json
import os
import subprocess
from pathlib import Path
from .common import BaseNode,run,topic
class Recorder(BaseNode):
    def __init__(self):
        super().__init__('recorder');c=self.config;r=c.get('recording',{})
        path=Path(r.get('path',str(Path(c['database']).parent/'bags'))).expanduser()
        path.mkdir(parents=True,exist_ok=True)
        from datetime import datetime,timezone
        output=path/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        topics=['/tf','/tf_static','/soarm/events','/soarm/workflow/status','/soarm/workflow/decision']
        for a in c['arms']:topics.extend([topic(a['id'],'joint_states'),topic(a['id'],'lerobot/status')])
        for camera in c['cameras']:
            topics.extend(['/soarm/camera/'+camera['id']+'/image_raw/compressed','/soarm/camera/'+camera['id']+'/camera_info'])
        manifest={'schema_version':1,'config_revision':c['_revision'],'topics':topics,'device_id':c['device_id'],
                  'models':[{k:a.get(k) for k in ('id','base_frame','urdf','tcp_frame')} for a in c['arms']],
                  'camera_calibration':[{k:v for k,v in x.items() if k!='device'} for x in c['cameras']]}
        for a in manifest['models']:
            if a.get('urdf'):a['urdf_sha256']=hashlib.sha256(Path(a['urdf']).read_bytes()).hexdigest()
        (path/(output.name+'.manifest.json')).write_text(json.dumps(manifest,indent=2))
        self.process=subprocess.Popen(['ros2','bag','record','--storage','mcap','-o',str(output),*topics])
    def destroy_node(self):
        import signal
        self.process.send_signal(signal.SIGINT)
        try:self.process.wait(timeout=8)
        except subprocess.TimeoutExpired:self.process.terminate()
        super().destroy_node()
def main():run(Recorder)
