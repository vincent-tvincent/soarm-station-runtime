#!/usr/bin/env python3
"""Read only IPC snapshots; no controls or hardware opens."""
import argparse,json,time,statistics
from pathlib import Path
from soarm.config import load
from soarm.ipc import Client
p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--seconds',type=float,default=20);p.add_argument('--output',required=True);a=p.parse_args()
c=load(a.config);client=Client(c['socket']);initial=client.call('snapshot');seen={};gaps={};start=time.monotonic()
while time.monotonic()-start<a.seconds:
 s=client.call('snapshot')
 for n,v in s['cameras'].items():
  if v.get('sequence') and seen.get(n,(0,0))[0]!=v['sequence']:
   if n in seen:gaps.setdefault(n,[]).append((v['time_ns']-seen[n][1])/1e6)
   seen[n]=(v['sequence'],v['time_ns'])
 time.sleep(.005)
elapsed=time.monotonic()-start;final=client.call('snapshot')
result={'duration_s':elapsed,'arms':{},'cameras':{},'read_only':True}
for n,v in final['arms'].items():
 first=initial['arms'][n];result['arms'][n]={'fresh_hz':(v['sequence']-first['sequence'])/elapsed,'health':v['health'],'phase':v['phase'],'permission':v['permission'],'error':v['error']}
 assert not v['permission'] and v['health'].get('writes',0)==0
for n,v in final['cameras'].items():
 first=initial['cameras'][n];values=sorted(gaps.get(n,[]))
 result['cameras'][n]={'fresh_fps':(v.get('sequence',0)-first.get('sequence',0))/elapsed,'longest_observed_gap_ms':max(values) if values else None,'age_ms':v['age_ms'],'error':v['error']}
Path(a.output).write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2));client.close()
