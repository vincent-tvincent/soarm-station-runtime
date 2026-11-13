#!/usr/bin/env python3
"""Native bus-only connection and feedback check; does not dispatch motion."""
import argparse
import hashlib
import json
import statistics
import time
from pathlib import Path

from soarm.config import load
from soarm.hardware import DeviceLock, LeRobotArm

p = argparse.ArgumentParser()
p.add_argument('--config', required=True)
p.add_argument('--arm', required=True)
p.add_argument('--output', required=True)
p.add_argument('--seconds', type=float, default=3.)
a = p.parse_args()
c = load(a.config)
cfg = next(arm for arm in c['arms'] if arm['id']==a.arm)
calibration_hash = hashlib.sha256(Path(cfg['calibration']).read_bytes()).hexdigest()
lock = DeviceLock(c['lock_dir'], cfg['port'])
arm = LeRobotArm(cfg)
report = {'port': cfg['port'], 'arm_id': cfg['id'], 'calibration_sha256': calibration_hash}
try:
    arm.connect()
    report['calibration_matches'] = True
    report['before'] = {'position': arm.read(), 'health': arm.health()}
    ticks = []
    reads = []
    start = time.monotonic()
    deadline = start
    while time.monotonic() - start < a.seconds:
        tick = time.monotonic()
        q, gripper = arm.read()
        ticks.append(time.monotonic())
        reads.append({'positions_rad': q, 'gripper_percent': gripper})
        deadline += .01
        time.sleep(max(0., deadline - time.monotonic()))
    report['after'] = {'position': arm.read(), 'health': arm.health()}
    gaps = [b-a for a,b in zip(ticks,ticks[1:])]
    report.update(samples=len(ticks), measured_hz=(len(ticks)-1)/(ticks[-1]-ticks[0]),
                  max_gap_ms=max(gaps)*1000, goal_writes=arm.writes)
    report['calibration_file_unchanged'] = calibration_hash == hashlib.sha256(Path(cfg['calibration']).read_bytes()).hexdigest()
    assert report['goal_writes'] == 0
    assert report['before']['health']['torque'] == report['after']['health']['torque']
    assert report['calibration_file_unchanged']
    report['passed'] = True
finally:
    arm.close()
    lock.close()
    Path(a.output).write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2))
