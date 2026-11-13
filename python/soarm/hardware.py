"""Measured radians and separate gripper percent; no setup writes on connect."""
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import time

class DeviceLock:
    def __init__(self, directory, identity):
        Path(directory).mkdir(parents=True,exist_ok=True)
        name=hashlib.sha256(os.path.realpath(str(identity)).encode()).hexdigest()
        self.file=open(Path(directory)/name,'a+')
        try: fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close(); raise RuntimeError(f'Device already owned: {identity}')
    def close(self): self.file.close()

class MockArm:
    def __init__(self, config):
        self.config=config
        self.q={j['name']:j.get('initial_rad',0.) for j in config['joints']}
        self.gripper=50.; self.writes=0; self.torque=False
    def connect(self): pass
    def read(self): return dict(self.q), self.gripper
    def health(self):
        return {'torque':'ON' if self.torque else 'OFF','voltage_v':12.,'temperature_c':25.,'writes':self.writes}
    def send(self,q,gripper):
        self.q=dict(q); self.gripper=gripper; self.writes+=1; self.torque=True
    def hold(self):
        if self.torque:self.send(self.q,self.gripper)
        return {'torque':'ON' if self.torque else 'OFF','writes':self.writes}
    def close(self): pass

class LeRobotArm:
    def __init__(self,config):
        self.config=config;self.bus=None;self.writes=0;self.dispatch_ready=False
    def connect(self):
        from lerobot.motors import Motor, MotorCalibration, MotorNormMode, SerialMotorsBus
        c=self.config
        calibration=json.loads(Path(c['calibration']).read_text())
        body=[j['name'] for j in c['joints']]
        self.gripper_name=c.get('gripper',{}).get('name','gripper')
        motors={n:Motor(calibration[n]['id'],'sts3215',MotorNormMode.DEGREES) for n in body}
        motors[self.gripper_name]=Motor(calibration[self.gripper_name]['id'],'sts3215',MotorNormMode.RANGE_0_100)
        self.bus=SerialMotorsBus(port=c['port'],motors=motors,
            calibration={n:MotorCalibration(**calibration[n]) for n in motors})
        # Bus-only connection avoids robot.configure(), calibration and torque contexts.
        self.bus.connect()
        actual=self.bus.read_calibration()
        for n in motors:
            if actual[n] != self.bus.calibration[n]:
                self.bus.disconnect(disable_torque=False)
                raise RuntimeError(f'Calibration mismatch: {n}')
    def read(self):
        values=self.bus.sync_read('Present_Position')
        return {j['name']:math.radians(values[j['name']]) for j in self.config['joints']},float(values[self.gripper_name])
    def health(self):
        torque=self.bus.sync_read('Torque_Enable',normalize=False)
        voltage=self.bus.sync_read('Present_Voltage',normalize=False)
        temp=self.bus.sync_read('Present_Temperature',normalize=False)
        goals=self.bus.sync_read('Goal_Position')
        return {'torque':'ON' if all(torque.values()) else 'OFF' if not any(torque.values()) else 'MIXED',
                'voltage_v':min(voltage.values())/10,'temperature_c':max(temp.values()),'writes':self.writes,
                'goal_positions_rad':{j['name']:math.radians(goals[j['name']]) for j in self.config['joints']},
                'goal_gripper_percent':goals[self.gripper_name]}
    def send(self,q,gripper):
        action={n:math.degrees(v) for n,v in q.items()}
        action[self.gripper_name]=gripper
        torque=self.bus.sync_read('Torque_Enable',normalize=False)
        if not self.dispatch_ready:
            if not all(torque.values()):
                # First authorized dispatch: stage a freshly measured pose. These
                # servos may arm from a goal write, so the staged goal must be current.
                present=self.bus.sync_read('Present_Position',normalize=False)
                self.bus.sync_write('Goal_Position',present,normalize=False)
                self.writes+=1
                self.bus.enable_torque()
                if not all(self.bus.sync_read('Torque_Enable',normalize=False).values()):
                    raise RuntimeError('Torque enable was not confirmed')
            self.dispatch_ready=True
        elif not all(torque.values()):
            raise RuntimeError('Torque was lost; reconnect and obtain fresh permission before rearming')
        self.bus.sync_write('Goal_Position',action)
        self.writes+=1
    def hold(self):
        # Hold only motors measured ON. Never arm an OFF motor from a stop request.
        torque=self.bus.sync_read('Torque_Enable',normalize=False)
        present=self.bus.sync_read('Present_Position',normalize=False)
        engaged={n:v for n,v in present.items() if torque[n]}
        if engaged:
            self.bus.sync_write('Goal_Position',engaged,normalize=False)
            self.writes+=1
        return {'torque':'ON' if all(torque.values()) else 'OFF' if not any(torque.values()) else 'MIXED',
                'writes':self.writes}
    def close(self):
        if self.bus is not None: self.bus.disconnect(disable_torque=False)

def make_arm(config):
    return (ReadOnlySO101(config) if config.get('read_only',False) and config['backend']=='so101' else {'mock':MockArm,'so101':LeRobotArm}[config['backend']](config))


class ReadOnlySO101:
    """Strict protocol-0 read allowlist for unattended encoder monitoring.

    This adapter cannot emit a goal, torque, calibration or configuration write.
    LeRobot's documented degree/percent calibration convention is preserved.
    """
    ALLOWED=frozenset(((3,2),(9,2),(11,2),(31,2),(40,1),(56,2),(62,1),(63,1)))
    def __init__(self,config):self.config=config;self.serial=None;self.writes=0
    def _read(self,motor,address,size):
        if (address,size) not in self.ALLOWED or not 1<=motor<=253:raise ValueError('Read outside allowlist')
        packet=bytearray([255,255,motor,4,2,address,size]);packet.append((~sum(packet[2:]))&255)
        if packet[4]!=2:raise ValueError('Only READ instruction is permitted')
        self.serial.reset_input_buffer();self.serial.write(packet);self.serial.flush()
        deadline=time.monotonic()+.06;received=bytearray()
        while time.monotonic()<deadline:
            chunk=self.serial.read(max(1,size+6-len(received)))
            received.extend(chunk)
            if len(received)>=size+6:break
        if len(received)!=size+6 or received[:2]!=b'\xff\xff' or received[2]!=motor or received[3]!=size+2 or (sum(received[2:])&255)!=255:
            raise ConnectionError(f'Invalid read reply id={motor} address={address} bytes={len(received)}')
        if received[4]:raise RuntimeError(f'Servo protection id={motor} status={received[4]}')
        return int.from_bytes(received[5:-1],'little')
    def connect(self):
        import serial
        self.calibration=json.loads(Path(self.config['calibration']).read_text())
        self.serial=serial.Serial(self.config['port'],1000000,timeout=.01,exclusive=True)
        for name,c in self.calibration.items():
            if self._read(c['id'],3,2)!=777:raise ValueError('SO101 motor model mismatch')
            homing=self._read(c['id'],31,2);homing=-(homing&2047) if homing&2048 else homing
            if (homing,self._read(c['id'],9,2),self._read(c['id'],11,2))!=(c['homing_offset'],c['range_min'],c['range_max']):
                raise ValueError('Calibration mismatch: '+name)
    def read(self):
        q={}
        for j in self.config['joints']:
            n=j['name'];c=self.calibration[n];raw=self._read(c['id'],56,2)
            if raw&32768:raw=-(raw&32767)
            if abs(raw)>4095:raise ValueError('Position outside one encoder turn')
            q[n]=(raw-(c['range_min']+c['range_max'])/2)*2*math.pi/4095
        name=self.config.get('gripper',{}).get('name','gripper');c=self.calibration[name];raw=self._read(c['id'],56,2)
        if raw&32768:raw=-(raw&32767)
        gripper=(raw-c['range_min'])/(c['range_max']-c['range_min'])*100
        return q,gripper
    def health(self):
        cs=list(self.calibration.values());torque=[self._read(c['id'],40,1) for c in cs]
        return {'torque':'ON' if all(torque) else 'OFF' if not any(torque) else 'MIXED',
            'voltage_v':min(self._read(c['id'],62,1) for c in cs)/10,
            'temperature_c':max(self._read(c['id'],63,1) for c in cs),'writes':0,'read_only':True}
    def send(self,q,gripper):raise PermissionError('Read-only monitoring profile; use a supervised writable backend for motion')
    def close(self):
        if self.serial is not None:self.serial.close()
