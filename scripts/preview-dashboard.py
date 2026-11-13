#!/usr/bin/env python3
"""Local dashboard QA only: static files + authenticated synthetic API fixtures.

No ROS/runtime/device imports, subprocesses, IPC or outbound HTTP connections.
Camera media are existing historical project files. Controls mutate memory only.
Bind is deliberately fixed to 127.0.0.1:8767; never deploy this preview server.
"""
import argparse
import asyncio
import copy
import hmac
import io
import json
import math
import mimetypes
from pathlib import Path
import time
import uuid

from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "soarm-preview-only-not-a-secret"
HOST, PORT = "127.0.0.1", 8767
ASSETS = {"app.js", "graph.js", "transport.js", "styles.css", "camera-layout.js", "payload-model.js"}


class Preview:
    def __init__(self, fixture, web_root, reference_root, scenario):
        self.fixture = json.loads(fixture.read_text())
        self.web_root, self.reference_root = web_root.resolve(), reference_root.resolve()
        self.media = {}
        self.reset(scenario or self.fixture["default_scenario"])

    def reset(self, scenario):
        if scenario not in self.fixture["scenarios"]:
            raise ValueError("Unknown preview scenario")
        self.scenario = scenario
        self.data = copy.deepcopy(self.fixture["scenarios"][scenario])
        self.operations = {}
        self.samples = {}

    def response(self, payload, status=200):
        return web.json_response(payload, status=status, headers={
            "Cache-Control": "no-store", "X-SOArm-Preview": "hardware-free-mock",
        })

    def status_value(self):
        st = copy.deepcopy(self.data["status"])
        st["resources"] = copy.deepcopy(self.data["resources"])
        st["environment"] = {**self.fixture["preview"]["environment"], "scenario": self.scenario}
        st["mock"] = True
        # Timestamp only simulated metadata; historical camera acquisition is unknown.
        stamp = time.time_ns()
        for entry in st.get("motor_status", {}).values():
            if entry["state"] == "simulated":
                entry["health_time_ns"] = stamp - 320_000_000
                for motor in entry["motors"]:
                    if motor["position"] is not None:
                        motor["position_time_ns"] = stamp - 7_400_000
        return st

    async def index(self, request):
        return web.FileResponse(self.web_root / "index.html", headers={"X-SOArm-Preview": "hardware-free-mock", "Cache-Control": "no-store"})

    async def asset(self, request):
        name = request.match_info["name"]
        if name not in ASSETS:
            raise web.HTTPNotFound()
        path = (self.web_root / name).resolve()
        if path.parent != self.web_root or not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "no-store", "X-SOArm-Preview": "hardware-free-mock"})

    async def preview_info(self, request):
        return self.response({"environment": self.fixture["preview"]["environment"],
                              "scenario": self.scenario, "scenarios": list(self.fixture["scenarios"]),
                              "mock_token": TOKEN, "hardware_connected": False})

    async def set_scenario(self, request):
        self.reset((await request.json())["scenario"])
        return self.response({"mock": True, "scenario": self.scenario})

    async def status(self, request):
        return self.response(self.status_value())

    async def resources(self, request):
        return self.response(self.data["resources"])

    def graph_value(self):
        graph = copy.deepcopy(self.data["graph"])
        if graph["state"] == "ready":
            graph["captured_at_ns"] = time.time_ns()
            graph["age_ms"] = 0
        for topic in graph["topics"]:
            sample = self.samples.get(topic["name"])
            if not sample:
                continue
            elapsed = sample.get("stopped_elapsed", min(time.monotonic() - sample["started"], sample["duration"]))
            count = int(max(0, elapsed) * sample["rate"])
            complete = sample.get("stopped", False) or elapsed >= sample["duration"]
            topic["traffic"] = {
                "state": "complete" if complete else "sampling", "sample_id": sample["id"],
                "started_at_ns": sample["started_at_ns"], "ended_at_ns": sample["started_at_ns"]+int(elapsed*1e9) if complete else None,
                "messages": count, "bytes": count * 288,
                "rate_hz": count / elapsed if elapsed > 0 else 0,
                "bytes_per_second": count * 288 / elapsed if elapsed > 0 else 0,
                "last_message_age_ms": 10 if count else None,
                "history": [{"at_ns": sample["started_at_ns"] + int(i * 1e9), "messages": int(sample["rate"]), "bytes": int(sample["rate"])*288} for i in range(min(20, int(elapsed)))],
                "reason": "Synthetic preview counters; no ROS subscription or payload access",
            }
        return graph

    async def graph(self, request):
        return self.response(self.graph_value())

    async def sample(self, request):
        body = await request.json()
        topic = next((t for t in self.data["graph"]["topics"] if t["name"] == body.get("topic")), None)
        if topic is None:
            raise ValueError("Unknown topic")
        if body.get("stop"):
            if topic["name"] in self.samples:
                active = self.samples[topic["name"]]
                active.update(stopped=True, stopped_elapsed=min(time.monotonic()-active["started"], active["duration"]))
            return self.response({"schema_version": 1, "accepted": True, "topic": topic["name"], "state": "complete", "mock": True})
        if any(any(word in typ for word in ("Image", "PointCloud", "Audio")) for typ in topic["types"]):
            return self.response({"schema_version": 1, "accepted": False, "topic": topic["name"], "state": "blocked", "reason": "High-bandwidth types are not sampled", "mock": True})
        duration = float(body.get("duration_s", 5))
        if not math.isfinite(duration) or not 0 < duration <= 10:
            raise ValueError("Sampling duration must be in (0,10]")
        active = sum(not s.get("stopped", False) and time.monotonic()-s["started"] < s["duration"] for s in self.samples.values())
        if active >= 2 and topic["name"] not in self.samples:
            return self.response({"accepted": False, "state": "unavailable", "reason": "Two preview samples are already active"}, 409)
        sample_id = "mock-sample-" + uuid.uuid4().hex[:10]
        self.samples[topic["name"]] = {"id": sample_id, "started": time.monotonic(), "started_at_ns": time.time_ns(), "duration": duration, "rate": 50.0 if "joint" in topic["name"] else 10.0}
        return self.response({"schema_version": 1, "accepted": True, "topic": topic["name"], "sample_id": sample_id, "state": "sampling", "mock": True})

    def arm(self, identifier):
        if identifier not in self.data["status"]["arms"]:
            raise ValueError("Explicit known arm_id required")
        return self.data["status"]["arms"][identifier]

    async def permission(self, request):
        body = await request.json(); arm = self.arm(body.get("arm_id"))
        if body.get("controller_session") != arm["controller_session"]:
            raise ValueError("Controller session mismatch")
        if body.get("enable") and body["arm_id"] not in self.data["status"]["joints"]:
            raise ValueError("No simulated feedback sample for this resource")
        arm["permission"] = bool(body.get("enable")); arm["phase"] = "IDLE" if arm["permission"] else "HOLD"
        return self.response({"accepted": True, "permission": arm["permission"], "controller_session": arm["controller_session"], "reason": "Mock permission only; no motor connection", "mock": True})

    def hold_arm(self, identifier):
        arm = self.arm(identifier); arm.update(phase="HOLD", permission=False, operation_type="NONE", operation_id="")
        for operation in self.operations.values():
            if operation["arm_id"] == identifier and not operation["done"]:
                operation.update(done=True, result={"result_code":"HELD", "execution_completed":False, "task_outcome":"UNKNOWN", "reason":"Mock hold confirmed; no hardware"})

    async def hold(self, request):
        body = await request.json(); self.hold_arm(body.get("arm_id"))
        return self.response({"success": True, "message": "Mock hold confirmed; no hardware", "mock": True})

    def submit(self, body):
        arm = self.arm(body.get("arm_id"))
        if not arm["permission"]:
            raise ValueError("Enable mock control first")
        if body.get("runtime_boot_id") != arm["runtime_boot_id"] or body.get("controller_session") != arm["controller_session"]:
            raise ValueError("Runtime/controller session mismatch")
        if body.get("operation_type") not in ("DIRECT", "TRAJECTORY", "MODEL") or body.get("mode") not in ("INLINE", "STREAM"):
            raise ValueError("Unsupported operation/input mode")
        if any(not operation["done"] and operation["arm_id"] == body["arm_id"] for operation in self.operations.values()):
            raise ValueError("BUSY")
        identifier = "mock-request-" + uuid.uuid4().hex[:12]
        operation_id = "mock-operation-" + uuid.uuid4().hex[:12]
        arm.update(phase="READY", operation_type=body["operation_type"], operation_id=operation_id)
        arm["operation_epoch"] += 1
        self.operations[identifier] = {"arm_id": body["arm_id"], "body": copy.deepcopy(body), "done": False, "result": None,
                                      "started": time.monotonic(), "feedback": {**copy.deepcopy(arm), "stream_ready": True, "window_id":"mock-window-"+uuid.uuid4().hex[:8], "window_valid_for_ms":250, "last_accepted_input_id":"", "last_applied_input_id":"", "intent_generation":1, "last_sequence":-1,"last_accepted_semantic_revision":0}}
        return identifier

    async def operate(self, request):
        return self.response({"request_id": self.submit(await request.json()), "mock": True})

    async def operation_status(self, request):
        identifier = request.match_info["id"]
        if identifier not in self.operations:
            raise ValueError("Unknown mock request")
        operation = self.operations[identifier]
        if not operation["done"] and operation["body"]["mode"] == "INLINE" and time.monotonic()-operation["started"] > 1:
            operation.update(done=True, result={"result_code":"COMPLETED", "execution_completed":True, "task_outcome":"UNKNOWN", "reason":"Synthetic completion only; no physical task was performed"})
            self.arm(operation["arm_id"]).update(phase="IDLE", operation_type="NONE", operation_id="")
        if not operation["done"]:
            operation["feedback"].update(window_id="mock-window-"+str(int(time.monotonic()*10)),window_valid_for_ms=250)
        return self.response({"done":operation["done"], "result":operation["result"], "feedback":operation["feedback"], "mock":True})

    def stream_value(self, body):
        arm_id = body.get("arm_id"); arm = self.arm(arm_id)
        operation = next((v for v in self.operations.values() if v["arm_id"] == arm_id and not v["done"]), None)
        if not operation:
            raise ValueError("No active mock operation")
        if body.get("operation_id") != operation["feedback"]["operation_id"]:
            raise ValueError("Operation mismatch")
        if body.get("frame_kind") == "END":
            self.hold_arm(arm_id); return {"forwarded": True, "mock": True}
        if body.get("frame_kind") == "COMMAND":
            target = body.get("body", {}).get("positions", {})
            joints = self.data["status"]["joints"].get(arm_id)
            if joints and target:
                expected = {n.split("/")[-1] for n in joints["name"]}
                if set(target) != expected or not all(isinstance(v,(int,float)) and math.isfinite(v) for v in target.values()):
                    raise ValueError("Full finite simulated joint set required")
                joints["position"] = [float(target[n.split("/")[-1]]) for n in joints["name"]]
                for motor in self.data["status"].get("motor_status", {}).get(arm_id, {}).get("motors", []):
                    if motor["name"] in target:
                        motor.update(position=float(target[motor["name"]]), reference_position=float(target[motor["name"]]), goal_position=float(target[motor["name"]]))
            gripper = body.get("body", {}).get("gripper")
            if gripper is not None:
                if not math.isfinite(gripper) or not 0 <= gripper <= 100:
                    raise ValueError("Invalid simulated gripper percentage")
                arm["gripper_percent"] = gripper
                for motor in self.data["status"].get("motor_status", {}).get(arm_id, {}).get("motors", []):
                    if motor["role"] == "gripper":motor.update(position=gripper, reference_position=gripper, goal_position=gripper)
            arm["phase"] = "ACTIVE"
            operation["feedback"].update(last_accepted_input_id=body.get("input_id",""), last_applied_input_id=body.get("input_id",""), last_sequence=body.get("sequence",0),last_accepted_semantic_revision=body.get("semantic_revision",0), phase="ACTIVE")
        return {"forwarded":True,"mock":True,"simulation_only":True}

    async def stream(self, request):
        return self.response(self.stream_value(await request.json()))

    async def workflow(self, request):
        body = await request.json(); self.arm(body.get("arm_id"))
        return self.response({"accepted":True,"request_id":"mock-workflow-"+uuid.uuid4().hex[:8],"reason":"Synthetic acceptance only","mock":True})

    async def cancel(self, request):
        operation = self.operations.get(request.match_info["id"])
        if not operation:raise ValueError("Unknown mock request")
        self.hold_arm(operation["arm_id"])
        return self.response({"accepted":True,"stopped":True,"mock":True})

    async def history(self, request):
        arm_id=request.query.get("arm", "arm1");limit=min(500,max(0,int(request.query.get("limit",100))))
        joints=self.data["status"]["joints"].get(arm_id); records=[]
        if joints:
            for i in range(limit):
                records.append({"sequence":i+1,"sample":{"arm_id":arm_id,"valid":i%23!=11,"scheduled_time":{"sec":int(time.time())-limit+i,"nanosec":0},"measured_joints":{"name":joints["name"],"position":[v+.025*math.sin(i/9+j) for j,v in enumerate(joints["position"])]}},"synthetic":True})
        return self.response({"records":records,"events":self.data["status"]["events"],"next_sequence":0,"mock":True})

    def image_bytes(self, identifier):
        camera=next((c for c in self.data["resources"]["cameras"] if c["id"]==identifier),None)
        if not camera:raise web.HTTPNotFound(text="Unknown camera reference")
        source=(self.reference_root/camera["reference_file"]).resolve()
        if not source.is_relative_to(self.reference_root) or not source.is_file():
            raise web.HTTPNotFound(text="Historical reference file unavailable; no camera is opened")
        if source not in self.media:
            if source.suffix.lower() in (".jpg",".jpeg"):
                content=source.read_bytes()
            else:
                # Format-only in-memory JPEG conversion; original pixels/aspect are not cropped.
                from PIL import Image
                output=io.BytesIO()
                with Image.open(source) as im:im.convert("RGB").save(output,format="JPEG",quality=93)
                content=output.getvalue()
            self.media[source]=content
        return self.media[source],camera

    async def image(self, request):
        content,camera=self.image_bytes(request.match_info["id"])
        return web.Response(body=content,content_type="image/jpeg",headers={"Cache-Control":"no-store","X-SOArm-Preview":"historical-reference-not-live","X-Source-Time-Ns":"0","X-Reference-Date":camera["reference_date"],"X-Reference-Kind":camera.get("reference_kind","historical_reference")})

    async def camera_socket(self, request):
        content,camera=self.image_bytes(request.match_info["id"])
        ws=web.WebSocketResponse(protocols=["soarm_station"],heartbeat=10,max_msg_size=1024)
        await ws.prepare(request)
        try:
            await ws.send_json({"type":"preview_metadata","mock":True,"reference_only":True,"source_time_ns":None,"reference_date":camera["reference_date"]})
            while not ws.closed:
                await ws.send_bytes(content);await asyncio.sleep(.2)
        except (ConnectionError,RuntimeError,asyncio.CancelledError):pass
        finally:await ws.close()
        return ws

    async def control_socket(self, request):
        ws=web.WebSocketResponse(protocols=["soarm_station"],heartbeat=10,max_msg_size=262144)
        await ws.prepare(request);arm_id=None
        try:
            body=await ws.receive_json(timeout=5);arm_id=body["arm_id"]
            if body.get("operation_type")!="DIRECT" or body.get("mode")!="STREAM":raise ValueError("Preview control socket uses DIRECT+STREAM")
            identifier=self.submit(body);operation=self.operations[identifier];last=0
            while not ws.closed and not operation["done"]:
                if time.monotonic()-last>=.05:
                    await ws.send_json({"type":"grant","grant":operation["feedback"],"mock":True})
                    await ws.send_json({"type":"state","state":self.arm(arm_id),"joints":self.data["status"]["joints"].get(arm_id),"mock":True});last=time.monotonic()
                try:msg=await ws.receive(timeout=.02)
                except asyncio.TimeoutError:continue
                if msg.type==web.WSMsgType.TEXT:self.stream_value(json.loads(msg.data))
                elif msg.type in (web.WSMsgType.CLOSE,web.WSMsgType.CLOSED,web.WSMsgType.ERROR):break
        except Exception as exc:
            if not ws.closed:await ws.send_json({"type":"error","reason":str(exc),"mock":True})
        finally:
            if arm_id in self.data["status"]["arms"]:self.hold_arm(arm_id)
            await ws.close()
        return ws


def build_app(preview):
    @web.middleware
    async def auth(request, handler):
        public=request.path in ("/","/api/preview") or request.path.startswith("/assets/")
        if not public:
            supplied=request.headers.get("Authorization", "")
            if request.path.startswith("/ws/") and not supplied:
                protocols=[p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",")]
                if len(protocols)==2 and protocols[0]=="soarm_station":supplied="Bearer "+protocols[1]
            if not hmac.compare_digest(supplied,"Bearer "+TOKEN):
                return preview.response({"error":"Preview authentication required","mock":True},401)
            origin=request.headers.get("Origin")
            if origin and origin not in (f"http://127.0.0.1:{PORT}",f"http://localhost:{PORT}"):
                return preview.response({"error":"Preview origin mismatch","mock":True},403)
        try:return await handler(request)
        except (ValueError,KeyError,TypeError) as exc:return preview.response({"error":str(exc),"mock":True},400)
    app=web.Application(middlewares=[auth],client_max_size=262144)
    routes=[web.get("/",preview.index),web.get("/assets/{name}",preview.asset),web.get("/api/preview",preview.preview_info),
            web.post("/api/preview/scenario",preview.set_scenario),web.get("/api/status",preview.status),web.get("/api/resources",preview.resources),
            web.get("/api/graph",preview.graph),web.get("/api/v1/ros/graph",preview.graph),web.post("/api/graph/sample",preview.sample),web.post("/api/v1/ros/sample",preview.sample),
            web.get("/api/history",preview.history),web.post("/api/permission",preview.permission),web.post("/api/hold",preview.hold),web.post("/api/operate",preview.operate),
            web.get("/api/operations/{id}",preview.operation_status),web.post("/api/operations/{id}/cancel",preview.cancel),web.post("/api/stream",preview.stream),web.post("/api/workflows",preview.workflow),
            web.get("/api/cameras/{id}.jpg",preview.image),web.get("/ws/camera/{id}",preview.camera_socket),web.get("/ws/control",preview.control_socket)]
    app.add_routes(routes)
    return app


def main():
    global PORT
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port",type=int,default=PORT)
    parser.add_argument("--fixture",type=Path,default=ROOT/"tests/dashboard-fixture.json")
    parser.add_argument("--web-root",type=Path,default=ROOT/"web")
    parser.add_argument("--reference-root",type=Path,default=Path.cwd(),help="Outer project containing existing outputs/ historical camera files")
    parser.add_argument("--scenario",choices=["station","mobile","unknown"],default="station")
    args=parser.parse_args();PORT=args.port
    if not 1024<=PORT<=65535:parser.error("Port must be between 1024 and 65535")
    preview=Preview(args.fixture,args.web_root,args.reference_root,args.scenario)
    print(f"MOCK ONLY / no hardware or backend network: http://{HOST}:{PORT}")
    print(f"Fake preview token: {TOKEN}")
    print("Historical camera references only. API controls mutate in-memory fixtures.")
    web.run_app(build_app(preview),host=HOST,port=PORT,access_log=None,print=None)


if __name__=="__main__":main()
