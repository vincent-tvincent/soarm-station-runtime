"""Native input-host bridge: discover without opening; sample only after Start.

The browser/gateway never owns leader USB. This process opens only the selected
leader's bus and never enables follower permission. Robot commands use the
existing gateway DIRECT+STREAM protocol. No robot/serial imports occur at import.
"""
import argparse
import asyncio
import copy
import contextlib
import dataclasses
import hashlib
import json
import math
import multiprocessing
import os
from pathlib import Path
import platform
import socket
import ssl
import time
import uuid


def load_config(path=None):
    if path is None:
        return {"host_id": socket.gethostname(), "label": socket.gethostname(), "leaders": []}
    path = Path(path).expanduser().resolve()
    if path.suffix.lower() == ".json":
        config = json.loads(path.read_text())
    else:
        import yaml
        config = yaml.safe_load(path.read_text())
    if not isinstance(config, dict):
        raise ValueError("Input-host configuration must be an object")
    config = copy.deepcopy(config)
    config.setdefault("host_id", socket.gethostname())
    config.setdefault("label", config["host_id"])
    config.setdefault("leaders", [])
    if not isinstance(config["host_id"], str) or not 1 <= len(config["host_id"]) <= 128:
        raise ValueError("A bounded input host_id is required")
    ids = []
    for profile in config["leaders"]:
        ids.append(profile["id"])
        calibration = Path(profile["calibration"]).expanduser()
        profile["calibration"] = str((path.parent/calibration).resolve() if not calibration.is_absolute() else calibration.resolve())
        if not profile.get("match"):
            raise ValueError("Every leader profile needs an explicit serial or port match")
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate leader profile IDs")
    return config


def serial_inventory():
    """OS metadata enumeration only; list_ports does not open a serial device."""
    from serial.tools import list_ports
    rows = []
    for port in list_ports.comports():
        # macOS has paired tty/cu paths: use callout handles and avoid duplicates.
        if platform.system() == "Darwin" and port.device.startswith("/dev/tty."):
            continue
        rows.append({"port": port.device, "serial": port.serial_number,
                     "vid": port.vid, "pid": port.pid, "description": port.description or port.device})
    return sorted(rows, key=lambda row: row["port"])


def profile_matches(profile, device):
    match = profile.get("match", {})
    supported = {"port", "serial", "vid", "pid"}
    if not match or set(match)-supported:
        return False
    return all((os.path.realpath(str(device.get(key))) == os.path.realpath(str(value)))
               if key == "port" else device.get(key) == value for key,value in match.items())


def calibration_for(profile):
    calibration = json.loads(Path(profile["calibration"]).read_text())
    mapping = profile.get("joint_map", {})
    gripper = profile.get("gripper_source", "gripper")
    if not mapping or len(set(mapping.values())) != len(mapping) or gripper in mapping:
        raise ValueError("Explicit one-to-one joint_map and separate gripper are required")
    names = list(mapping) + [gripper]
    if not isinstance(calibration, dict) or any(name not in calibration for name in names):
        raise ValueError("Calibration does not cover all configured leader motors")
    motor_ids = []
    for name in names:
        item = calibration[name]
        for key in ("id", "drive_mode", "homing_offset", "range_min", "range_max"):
            if type(item.get(key)) is not int:
                raise ValueError("Invalid calibration field: " + name + "." + key)
        if not 1 <= item["id"] <= 253 or item["range_min"] >= item["range_max"]:
            raise ValueError("Invalid calibration range/ID: " + name)
        motor_ids.append(item["id"])
    if len(set(motor_ids)) != len(motor_ids):
        raise ValueError("Duplicate motor IDs in selected calibration")
    return {name: calibration[name] for name in names}


def gripper_reference(value):
    """A valid percent reference, or None; zero is valid and never a default."""
    return float(value) if type(value) in (int,float) and math.isfinite(value) and 0<=value<=100 else None


def select_gripper_command(sample, follow, last_sent, initial_reference, follower_state):
    if follow:
        value=gripper_reference(sample)
        if value is None:raise ValueError('Invalid calibrated leader gripper sample')
        return value
    for candidate in (last_sent,initial_reference,(follower_state or {}).get('gripper_percent')):
        value=gripper_reference(candidate)
        if value is not None:return value
    raise ValueError('Follower gripper reference unavailable; no default jaw target is generated')


def bounded_leader_target(measured,limits):
    """Keep all axes live when the leader travels beyond the follower's range."""
    values=measured.get('positions',{})
    if not limits or set(values)!=set(limits):raise ValueError('Complete follower joint limits are required')
    target=copy.deepcopy(measured)
    for name,value in values.items():
        low,high=limits[name]['min_rad'],limits[name]['max_rad']
        if any(type(v) not in (int,float) or not math.isfinite(v) for v in (value,low,high)) or low>=high:
            raise ValueError('Invalid leader sample or follower limits: '+name)
        target['positions'][name]=max(low,min(high,value))
    gripper=measured.get('gripper')
    if type(gripper) not in (int,float) or not math.isfinite(gripper):raise ValueError('Invalid leader gripper sample')
    target['gripper']=max(0.,min(100.,gripper))
    return target


async def sample_until_control_ends(sampler,receiver):
    """Stop a pending device read when its owning control stream ends."""
    pending=asyncio.create_task(sampler.sample())
    try:
        ready,_=await asyncio.wait((pending,receiver),return_when=asyncio.FIRST_COMPLETED)
        if receiver in ready:
            await receiver
            raise RuntimeError('Follower control socket closed')
        return pending.result()
    finally:
        if not pending.done():pending.cancel()
        with contextlib.suppress(asyncio.CancelledError,Exception):await pending


def discover(config, enumerate_ports=serial_inventory):
    """Return real OS devices, marking configured candidates without opening them."""
    result = []
    for device in enumerate_ports():
        matches = [p for p in config.get("leaders", []) if profile_matches(p, device)]
        digest = hashlib.sha256((device["port"] + "\0" + str(device.get("serial"))).encode()).hexdigest()[:16]
        item = {"id": "serial-"+digest, "label": device.get("description") or device["port"],
                "port": device["port"], "serial": device.get("serial"), "vid": device.get("vid"), "pid": device.get("pid"),
                "configured": False, "reason": "No matching local calibration profile", "joint_names": []}
        if len(matches) > 1:
            item["reason"] = "Ambiguous profiles; configure one match for this physical device"
        elif matches:
            profile = matches[0]
            item.update(id="profile-"+profile["id"], label=profile.get("label", profile["id"]), profile_id=profile["id"])
            try:
                calibration_for(profile)
                item.update(configured=True, joint_names=list(profile["joint_map"].values()),
                            reason="Configured candidate; live calibration is checked only after Start")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                item["reason"] = str(exc)
        result.append(item)
    return result


class ReadOnlyLeader:
    """Bus-only LeRobot 0.7/0.6 reader; no Teleoperator.connect/configure path."""
    def __init__(self, profile, port, bus_factory=None, lock_dir="/tmp/soarm-station-device-locks"):
        self.profile, self.port, self.bus_factory = profile, port, bus_factory
        self.lock_dir, self.bus, self.lock_file = lock_dir, None, None

    def connect(self):
        import fcntl
        expected = calibration_for(self.profile)
        lock_path = Path(self.lock_dir)
        lock_path.mkdir(parents=True, exist_ok=True)
        identity = hashlib.sha256(os.path.realpath(self.port).encode()).hexdigest()
        self.lock_file = open(lock_path/identity, "a+")
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.bus_factory:
                self.bus = self.bus_factory(self.port, expected, self.profile)
            else:
                from lerobot.motors import Motor, MotorCalibration, MotorNormMode
                try:
                    from lerobot.motors import SerialMotorsBus as Bus
                except ImportError:
                    from lerobot.motors.feetech import FeetechMotorsBus as Bus
                gripper = self.profile.get("gripper_source", "gripper")
                motors = {name: Motor(item["id"], self.profile.get("motor_model", "sts3215"),
                    MotorNormMode.RANGE_0_100 if name == gripper else MotorNormMode.DEGREES)
                    for name, item in expected.items()}
                self.bus = Bus(port=self.port, motors=motors,
                    calibration={name: MotorCalibration(**item) for name,item in expected.items()})
            def forbidden_write(*args, **kwargs):
                raise PermissionError("Leader input forbids configuration, calibration, torque and goal writes")
            for name in ("write", "sync_write", "write_calibration", "configure_motors", "enable_torque", "disable_torque"):
                if hasattr(self.bus, name):
                    setattr(self.bus, name, forbidden_write)
            self.bus.connect()
            actual = self.bus.read_calibration()
            for name, item in expected.items():
                observed = actual.get(name)
                observed = dataclasses.asdict(observed) if dataclasses.is_dataclass(observed) else observed
                if observed != item:
                    raise ValueError("Calibration mismatch for " + name + "; input rejected without recalibration")
        except BaseException:
            try:self.close()
            except Exception:pass
            raise

    def sample(self):
        started = time.monotonic_ns()
        values = self.bus.sync_read("Present_Position")
        positions = {target: math.radians(float(values[source])) for source,target in self.profile["joint_map"].items()}
        gripper = float(values[self.profile.get("gripper_source", "gripper")])
        if not all(math.isfinite(v) for v in positions.values()) or not math.isfinite(gripper):
            raise ValueError("Invalid measured leader sample")
        return {"body": {"positions": positions, "gripper": gripper, "gripper_unit": "percent"},
                "started_ns": started, "completed_ns": time.monotonic_ns()}

    def close(self):
        try:
            if self.bus is not None:
                self.bus.disconnect(disable_torque=False)
        finally:
            self.bus = None
            if self.lock_file is not None:self.lock_file.close(); self.lock_file = None


def _reader_process(connection, profile, port, lock_dir):
    reader = ReadOnlyLeader(profile, port, lock_dir=lock_dir)
    try:
        reader.connect(); connection.send({"type": "ready"})
        while True:
            message = connection.recv()
            if message["type"] == "close":break
            if message["type"] != "sample":raise ValueError("Unknown reader request")
            connection.send({"type": "sample", "request_id": message["request_id"], **reader.sample()})
    except (EOFError, BrokenPipeError):pass
    except BaseException as exc:
        try:connection.send({"type": "error", "reason": str(exc)[:512]})
        except (EOFError, BrokenPipeError):pass
    finally:
        reader.close(); connection.close()


class ProcessSampler:
    """One pending read; Stop/source closure cancels and releases the reader."""
    def __init__(self, profile, port, lock_dir):
        self.profile, self.port, self.lock_dir = profile, port, lock_dir
        self.process = self.connection = None

    async def _receive(self, timeout):
        deadline = None if timeout is None else time.monotonic()+timeout
        while not self.connection.poll():
            if not self.process.is_alive():raise RuntimeError("Leader reader exited")
            if deadline is not None and time.monotonic() >= deadline:raise TimeoutError("Leader read/connect deadline expired")
            await asyncio.sleep(.001)
        message = self.connection.recv()
        if message["type"] == "error":raise RuntimeError(message["reason"])
        return message

    async def start(self):
        context = multiprocessing.get_context("spawn")
        self.connection, child = context.Pipe()
        self.process = context.Process(target=_reader_process, args=(child,self.profile,self.port,self.lock_dir), daemon=True)
        self.process.start(); child.close()
        await self._receive(8)

    async def sample(self):
        request_id = uuid.uuid4().hex
        self.connection.send({"type":"sample", "request_id":request_id})
        message = await self._receive(self.profile.get("sample_timeout_s"))
        if message.get("request_id") != request_id:raise RuntimeError("Leader sample identity mismatch")
        return message

    async def close(self):
        if self.process is None:return
        try:
            if self.process.is_alive():
                try:self.connection.send({"type":"close"})
                except (BrokenPipeError,EOFError,OSError):pass
                deadline=time.monotonic()+.3
                while self.process.is_alive() and time.monotonic()<deadline:await asyncio.sleep(.01)
                if self.process.is_alive():self.process.terminate()
                await asyncio.to_thread(self.process.join, .5)
                if self.process.is_alive():self.process.kill();await asyncio.to_thread(self.process.join, .5)
        finally:
            self.connection.close();self.process=None;self.connection=None


class InputHostBridge:
    def __init__(self, config, url, tls, headers=None, sampler_factory=ProcessSampler, enumerate_ports=serial_inventory):
        self.config, self.url, self.tls, self.headers = config, url.rstrip("/"), tls, headers or {}
        self.sampler_factory, self.enumerate_ports = sampler_factory, enumerate_ports
        self.task=None;self.active=None;self.registry=None;self.session=None

    def advertisement(self):
        return {"id":self.config["host_id"],"label":self.config["label"],"platform":platform.system(),
                "devices":discover(self.config,self.enumerate_ports)}

    async def emit(self, **message):
        if self.registry is not None and not self.registry.closed:
            await self.registry.send_json(message)

    async def stop(self, run_id=None):
        if self.active is None or (run_id is not None and self.active["run_id"]!=run_id):return
        task=self.task
        if task and not task.done():
            if not task.cancelling():task.cancel()
            # Do not inject a second cancellation into native-reader cleanup.
            try:await asyncio.wait_for(asyncio.shield(task),3)
            except (asyncio.CancelledError,TimeoutError):pass

    def apply_settings(self, message):
        active=self.active
        if (active is None or message.get('run_id')!=active['run_id']
                or message.get('operator_session_id')!=active['operator_session_id']
                or type(message.get('follow_gripper')) is not bool):
            return False
        active['follow_gripper']=message['follow_gripper']
        return True

    async def operate(self, request):
        sampler=None;run_id=request["run_id"];arm_id=request["arm_id"]
        try:
            device=next((d for d in discover(self.config,self.enumerate_ports) if d["id"]==request["device_id"]),None)
            if not device or not device["configured"]:raise ValueError("Selected leader is absent or unconfigured")
            if set(device["joint_names"])!=set(request["joint_names"]):raise ValueError("Leader mapping does not match selected follower joints")
            profile=next(p for p in self.config["leaders"] if p["id"]==device["profile_id"])
            sampler=self.sampler_factory(profile,device["port"],self.config.get("lock_dir","/tmp/soarm-station-device-locks"))
            await sampler.start()
            await self.emit(type="state",run_id=run_id,state="READY",reason="Leader calibration checked; awaiting follower grant")
            # No permission-enable request: the browser's explicit grant is mandatory.
            async with self.session.ws_connect(self.url+"/ws/control",ssl=self.tls,max_msg_size=262144,heartbeat=10) as control:
                await control.send_json({"arm_id":arm_id,"runtime_boot_id":request["runtime_boot_id"],
                    "controller_session":request["controller_session"],"operator_session_id":request["operator_session_id"],
                    "operation_type":"DIRECT","mode":"STREAM"})
                latest={"grant":None,"received":0.,"feedback_received":0.,"state":None}
                async def receive():
                    async for message in control:
                        if message.type.value!=1:continue
                        body=json.loads(message.data)
                        if body["type"]=="grant":
                            first_grant=latest["grant"] is None
                            latest.update(grant=body["grant"],received=time.monotonic())
                            if first_grant:
                                await self.emit(type="state",run_id=run_id,state="READY",reason="Follower ready; following from its current reference",operation_id=body["grant"].get("operation_id"))
                        elif body["type"]=="state":latest.update(state=body.get("state"),feedback_received=time.monotonic())
                        elif body["type"]=="error":raise RuntimeError(body["reason"])
                        elif body["type"]=="result":
                            result=body.get('result') or {}
                            raise RuntimeError(result.get('reason') or result.get('result_code') or 'Follower operation ended')
                receiver=asyncio.create_task(receive());sequence=0;started=time.monotonic();last_report=0.
                last_sent_gripper=None
                try:
                    while True:
                        tick=time.monotonic()
                        if receiver.done():await receiver;raise RuntimeError("Follower control socket closed")
                        grant=copy.deepcopy(latest["grant"])
                        grant_received=latest['received']
                        if not grant or not grant.get("stream_ready"):
                            if tick-started>5:raise TimeoutError("Follower did not grant READY")
                            await asyncio.sleep(.01);continue
                        if not latest["feedback_received"]:
                            if tick-started>5:raise TimeoutError("Follower supplied no measured-state feedback")
                            await asyncio.sleep(.002);continue
                        # This read starts after the original Pi window was received.
                        if tick-grant_received >= grant.get("window_valid_for_ms",0)/1000:
                            await asyncio.sleep(.002);continue
                        sample=await sample_until_control_ends(sampler,receiver)
                        if receiver.done():await receiver;raise RuntimeError("Follower control socket closed")
                        # A delayed read can miss this server window. Drop it and
                        # read again under a fresh window instead of ending a run.
                        if time.monotonic()-grant_received >= grant.get("window_valid_for_ms",0)/1000:
                            continue
                        # Read the current setting after the asynchronous native read.
                        # Only the outgoing jaw target changes; retain the real leader
                        # sample for the pose overlay and continue all body joints.
                        body=bounded_leader_target(sample['body'],request.get('joint_limits'))
                        follow=request.get('follow_gripper',True)
                        body['gripper']=select_gripper_command(body.get('gripper'),follow,last_sent_gripper,
                            request.get('gripper_reference'),latest['state'])
                        sequence+=1
                        await control.send_json({**grant,"arm_id":arm_id,"frame_kind":"COMMAND","sequence":sequence,
                            "input_id":uuid.uuid4().hex,"body":body})
                        last_sent_gripper=body['gripper']
                        if tick-last_report>=.1:
                            reason=latest["grant"].get("reason","")
                            running=(latest["state"] or {}).get("phase")=="ACTIVE" and (latest["state"] or {}).get("operation_id")==grant.get("operation_id")
                            await self.emit(type="state",run_id=run_id,state="RUNNING" if running else "READY",
                                reason=reason or ("" if running else "Following from current follower reference"),operation_id=grant.get("operation_id"),pose=sample["body"],
                                follow_gripper=follow,gripper_reference=last_sent_gripper,
                                sample_age_ms=(time.monotonic_ns()-sample["completed_ns"])/1e6,
                                measured_hz=sequence/max(.001,time.monotonic()-started),sequence=sequence)
                            last_report=tick
                        await asyncio.sleep(max(0,1/request["hz"]-(time.monotonic()-tick)))
                finally:
                    receiver.cancel()
                    try:await receiver
                    except (asyncio.CancelledError,RuntimeError):pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.emit(type="state",run_id=run_id,state="ERROR",reason=str(exc)[:512])
        finally:
            if sampler is not None:await sampler.close()
            await self.emit(type="stopped",run_id=run_id,state="IDLE")
            if self.active is not None and self.active["run_id"]==run_id:self.active=None

    async def run(self):
        from aiohttp import ClientSession
        async with ClientSession(headers=self.headers) as self.session:
            # A bridge disconnection ends sampling. Reconnect advertises only; no resume.
            while True:
                try:
                    async with self.session.ws_connect(self.url+"/ws/input-host",ssl=self.tls,max_msg_size=262144,heartbeat=5) as self.registry:
                        await self.registry.send_json({"type":"hello","host":self.advertisement()})
                        async def advertise():
                            while True:
                                await asyncio.sleep(3);await self.emit(type="inventory",host=self.advertisement())
                        updater=asyncio.create_task(advertise())
                        try:
                            async for message in self.registry:
                                if message.type.value!=1:continue
                                body=json.loads(message.data)
                                if body.get("type")=="start":
                                    if self.active is not None:await self.emit(type="state",run_id=body["run_id"],state="ERROR",reason="Input host is busy");continue
                                    self.active=body;self.task=asyncio.create_task(self.operate(body))
                                elif body.get("type")=="stop":await self.stop(body.get("run_id"))
                                elif body.get("type")=="settings":self.apply_settings(body)
                                elif body.get("type")=="refresh":await self.emit(type="inventory",host=self.advertisement())
                        finally:
                            updater.cancel();await self.stop()
                except asyncio.CancelledError:await self.stop();raise
                except Exception as exc:
                    print("Input-host connection unavailable:",str(exc)[:200],flush=True)
                    await self.stop();await asyncio.sleep(2)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url",help="Existing HTTPS dashboard URL on the trusted tailnet")
    parser.add_argument("--ca",help="CA certificate for the existing gateway TLS identity")
    parser.add_argument("--config",type=Path,help="Local JSON/YAML leader profiles; omit to advertise unconfigured OS serial devices")
    parser.add_argument("--list",action="store_true",help="Print OS inventory/configuration only; no device opens or network")
    args=parser.parse_args();config=load_config(args.config)
    if args.list:print(json.dumps({"host_id":config["host_id"],"devices":discover(config)},indent=2));return
    if not args.url or not args.url.startswith("https://"):parser.error("Use the gateway HTTPS URL; TLS verification is required")
    tls=ssl.create_default_context(cafile=args.ca)
    token=os.environ.get("SOARM_STATION_TOKEN","")
    headers={"Authorization":"Bearer "+token} if token else {}
    print("Input host:",config["host_id"],"— advertising only; choose a configured leader and press Start in the dashboard",flush=True)
    try:asyncio.run(InputHostBridge(config,args.url,tls,headers).run())
    except KeyboardInterrupt:pass


if __name__=="__main__":main()
