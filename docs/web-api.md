# External application integration

The [README API tables](../README.md#web-api-tables) list every mounted application route, including aliases and the optional separate collector. This guide explains the control protocol. It describes the implemented gateway, not a generic ROS bridge: external programs need HTTPS/WSS access, but do not need to become ROS nodes or open robot devices.

Use an external LLM/MCP harness, CV pipeline or rule-based application to decide the task. Submit named joint targets, bounded Cartesian targets, joint paths or registered policy instructions. LeRobot is required on the execution/inference side for learned policies; an external agent sending ordinary joint/IK commands does not need a LeRobot policy.

## Connection, access and discovery

The configured gateway normally listens on HTTPS port 8443. Trust the installation's CA/certificate. The default access middleware accepts actual loopback and Tailscale socket peers; otherwise supply `Authorization: Bearer <SOARM_STATION_TOKEN>`. `web.trusted_network_access: false` requires the bearer for those peers too. The token is separate from the client-generated `operator_session_id` used for motion ownership.

Browser mutations and WebSockets require a matching `Origin` when one is supplied. There is no permissive CORS proxy. An external server-side agent can connect directly; an embedded browser client should share the gateway origin. Use `soarm_station` as the camera/control WebSocket subprotocol. Headless WebSocket clients can use the bearer header; a browser requiring bearer access can supply `['soarm_station', token]`. Never put credentials in the URL. `/ws/input-host` is the separate native leader-registration protocol, not an ordinary joint-control socket.

This standard-library Python example is **read-only**. Set the URL and trust file for your installation; set the token only when required. It prints discovered resource IDs and current arm state without granting permission or sending a goal.

```python
import json
import os
import ssl
import urllib.request
import uuid

BASE = os.environ.get("STATION_URL", "https://localhost:8443").rstrip("/")
TLS = ssl.create_default_context(cafile=os.environ["STATION_CA"])
TOKEN = os.environ.get("SOARM_STATION_TOKEN", "")
OPERATOR = "external-" + uuid.uuid4().hex

def api(path, body=None, method=None):
    headers = {"Accept": "application/json"}
    if TOKEN:
        headers["Authorization"] = "Bearer " + TOKEN
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, allow_nan=False).encode()
    request = urllib.request.Request(BASE + path, data=data, headers=headers,
                                     method=method or ("POST" if data is not None else "GET"))
    with urllib.request.urlopen(request, context=TLS, timeout=10) as response:
        return json.load(response)

resources = api("/api/resources")
status = api("/api/status")
print("Arms:", [a["id"] for a in resources["arms"]])
print("Cameras:", [c["id"] for c in resources["cameras"]])
print("Arm state:", status["arms"])
```

Read joint names/limits, arm IDs and camera IDs from `/api/resources`. Status body joints are in `status['joints'][arm_id]` (`name` and `position` arrays), and the separate gripper value is `status['arms'][arm_id]['gripper_percent']`. ROS joint names may include the arm prefix; control payloads use the resource-local names listed by discovery. Use actual measured values and their freshness; missing observations are not zero-valued joints.

Units are radians for body joints, metres for Cartesian positions, XYZW for quaternions, seconds for HTTP durations, and calibrated percent for the separate gripper (0 closed / 100 open). MODEL outputs are adapted using the trained profile contract, not assumed to use those units directly.

## Starting an operation

For a motion request, read current state, explicitly obtain permission, then send an operation with the same operator identity and current boot/controller IDs. The following functions extend the read-only helper above. **Calling them can operate the selected arm.** Use the mock configuration when first integrating a client.

```python
def enable_control(arm_id):
    state = api("/api/status")["arms"][arm_id]
    response = api("/api/permission", {
        "arm_id": arm_id,
        "controller_session": state["controller_session"],
        "operator_session_id": OPERATOR,
        "enable": True,
    })
    if not response["accepted"] or not response["permission"]:
        raise RuntimeError(response.get("reason", "Permission rejected"))
    return state

def start_operation(arm_id, operation):
    state = api("/api/status")["arms"][arm_id]
    body = dict(operation)
    body.update(arm_id=arm_id,
                runtime_boot_id=state["runtime_boot_id"],
                controller_session=state["controller_session"],
                operator_session_id=OPERATOR)
    return api("/api/operate", body)["request_id"]

def operation_status(request_id):
    return api("/api/operations/" + request_id)

def hold_arm(arm_id):
    result = api("/api/hold", {"arm_id": arm_id})
    if not result["success"]:
        raise RuntimeError(result.get("message", "Hold not confirmed"))
    return result
```

Call `enable_control(arm_id)` deliberately before starting. `start_operation` does not silently re-enable permission. Permission and operation identity are independent of API authentication. The owner admits one operation per arm; a competing operation can be rejected. A restart changes boot/controller identity; a reconnect does not continue the previous stream.

`request_id` is a gateway polling handle. `operation_status(request_id)` returns:

```json
{
  "done": false,
  "feedback": {
    "arm_id": "arm1",
    "phase": "READY",
    "operation_id": "NATIVE_OPERATION_ID",
    "runtime_boot_id": "BOOT_ID",
    "controller_session": "CONTROLLER_ID",
    "operation_epoch": 1,
    "stream_ready": true,
    "window_id": "HOST_WINDOW_ID",
    "window_valid_for_ms": 250,
    "last_accepted_input_id": "",
    "last_applied_input_id": "",
    "last_accepted_semantic_revision": 0,
    "last_sequence": 0
  },
  "result": null
}
```

For INLINE, feedback need not be stream-ready. Poll until `done: true`; inspect `result.execution_completed`, `result.result_code`, `result.reason` and `result.task_outcome`. HTTP acceptance is not physical completion, and execution completion does not establish task success. The gateway retains a bounded in-memory operation map, so this is not durable run history. A dashboard restart can lose polling handles.

Normal healthy INLINE completion retains the owner's permission. Explicit hold/disable ends that authority. `/api/hold` is arm-wide rather than scoped to a request ID; use it deliberately for the intended arm, not as unconditional cleanup for a stale client after another operator has taken over. Do not blindly repeat an operation POST after a network timeout: reconcile current state first, because the earlier request may already have been admitted.

## Operation payloads

The `operation` argument to `start_operation` contains one of the shapes below; the helper adds arm/session/operator fields. `duration_s` at the top level is an optional operation limit, subject to configured bounds. STREAM has no default total lifetime limit unless the deployment sets `max_stream_duration_s`.

### DIRECT: named joints and gripper

```python
operation = {
    "operation_type": "DIRECT",
    "mode": "INLINE",
    "points": [
        {"time_s": 0.25, "positions": measured_positions, "gripper": measured_gripper},
        {"time_s": 1.5, "positions": planned_positions, "gripper": planned_gripper},
    ],
}
```

Here `measured_positions`/`planned_positions` are complete dictionaries of discovered body-joint names to radians, and gripper values are explicit percentages. DIRECT INLINE builds a checked timed path from the admitted reference and allows at most 16 steps. `time_s` is cumulative from operation start, positive and strictly increasing; the gateway derives each step duration. Specify the gripper even when unchanged—omitting it is not a documented preserve command. The aligned pickup requirement described below applies to DIRECT STREAM.

For continuous input use `DIRECT` + `STREAM` with `/ws/control`, described below. This is also the entry point for an LLM agent that already generates joint targets.

### TRAJECTORY: timed joint positions

```python
operation = {
    "operation_type": "TRAJECTORY",
    "mode": "INLINE",
    "points": [
        {"time_s": 0.5, "positions": waypoint_one},
        {"time_s": 2.0, "positions": waypoint_two},
    ],
}
```

Provide 1–256 complete named joint points with increasing positive cumulative `time_s`. This HTTP path carries body position/time targets, not velocities, accelerations, effort or gripper changes. It preserves the current gripper reference; use DIRECT for explicit gripper commands. For HTTP TRAJECTORY STREAM, open a stream with no inline points and send `JOINT_TRAJECTORY` command bodies. A new semantic revision replaces the prior path from the admitted reference. This is path replacement, not a queued rolling-chunk planner.

### TRAJECTORY: Cartesian IK endpoints

```python
operation = {
    "operation_type": "TRAJECTORY",
    "mode": "INLINE",
    "payload_kind": "CARTESIAN_TARGET",
    "cartesian": [{
        "frame_id": discovered_base_frame,
        "position": target_xyz_metres,
        "orientation_mode": "POSITION_ONLY",
        "duration_s": 1.5,
    }],
}
```

Use 1–16 endpoints with `frame_id` exactly matching the configured arm base frame; this endpoint performs no automatic TF conversion. Each Cartesian `duration_s` is the duration of that segment, unlike cumulative joint `time_s`. `POSITION_ONLY` leaves orientation unconstrained; `STRICT_POSE` requires a normalized quaternion `orientation: [x,y,z,w]` and may be infeasible for this five-axis arm. The configured model, frame, IK residual and joint/path checks remain authoritative. The HTTP gateway rejects Cartesian STREAM. Its contract does not expose collision-clearance constraints or a continuous Cartesian-path guarantee; adding extra JSON fields does not enable them.

YOLO/CV detections must first be converted from pixels/depth into the declared robot frame using actual calibration. Alternatively, incremental teleoperation can call `/api/manual/{arm_id}/step`, then send its returned joint target through the existing DIRECT stream. Manual FK/step/preset/roll endpoints only calculate; receiving a successful calculation does not command the arm.

### MODEL: ACT or language-conditioned policy

For a managed profile, prepare its saved revision through `/api/v1/policy-profiles/{id}/prepare` and wait for its job. Select the intended arm's **matching ready profile revision** from `applied_by_arm` in `/api/v1/policy-runtime`; use its `runtime_profile_id` as the operation's `profile_id`. The saved human-readable profile ID and immutable runtime ID are different. Startup-configured policy IDs advertised for that arm by `/api/resources` can also be used directly; they do not require this management preparation flow.

```python
operation = {
    "operation_type": "MODEL",
    "mode": "INLINE",
    "profile_id": prepared_runtime_profile_id,
    "model_steps": [
        {"instruction": "Place the cup in the tray", "duration_s": 10.0},
    ],
}
```

INLINE supports 1–16 bounded model steps. For a policy with no language conditioning, such as a typical ACT checkpoint, supply an empty instruction and a bounded duration. A VLA instruction must match its trained capabilities; the GUI/descriptor does not make every policy language-conditioned. For continuously updated instructions use HTTP MODEL STREAM, `payload_kind: "MODEL_TASK"` and `body: {"instruction": "..."}`. Native descriptor text-generation support is not the same as action language conditioning.

## Continuous input and acknowledgments

### DIRECT control WebSocket

After permission, open `wss://HOST:8443/ws/control`. Its first JSON message is an operation envelope with `operation_type: "DIRECT"`, `mode: "STREAM"`, explicit arm/operator/boot/controller IDs and optional `duration_s`; do not put a point sequence into this opening message.

Server text messages are:

- `{"type":"grant","grant":{...OperationFeedback...}}`: readiness, identity, current window and acceptance acknowledgments.
- `{"type":"state","state":{...},"joints":{...}}`: cached status and measured joints.
- `{"type":"result","result":{...}}`: terminal result.
- `{"type":"error","reason":"..."}`: rejected or interrupted connection.

After `stream_ready`, generate fresh targets using the current grant. A command frame is:

```json
{
  "arm_id": "arm1",
  "operator_session_id": "YOUR_STABLE_OPERATOR_ID",
  "runtime_boot_id": "FROM_GRANT",
  "controller_session": "FROM_GRANT",
  "operation_id": "FROM_GRANT",
  "operation_epoch": 1,
  "window_id": "FROM_CURRENT_GRANT",
  "sequence": 1,
  "frame_kind": "COMMAND",
  "payload_kind": "DIRECT_JOINT_TARGET",
  "input_id": "UNIQUE_INPUT_ID",
  "body": {"positions": {"joint_name": 0.1}, "gripper": 35.0, "gripper_unit": "percent"}
}
```

Replace the illustrative one-joint dictionary with the entire discovered body-joint set. Copy `operation_epoch` and all identities from actual feedback; example values are not defaults. Begin with an aligned pickup target. Increase `sequence` for every transmitted frame, not just target changes. Track `last_accepted_input_id` and `last_applied_input_id`; source validity and actual dispatch are distinct.

The host supplies approximately 250-ms windows. Track their usable lifetime against your own local elapsed time, accounting for response delay; never compare monotonic clocks between hosts or re-stamp a cached sensor sample as fresh. The current native operator requests 50–120 Hz input, while the device dispatch loop remains approximately 100 Hz. Keep latest targets rather than queuing stale poses across latency/reconnect.

`HEARTBEAT` and `END` frames carry the same current identities/window/sequence with `payload_kind: "NONE"` and no command body. Heartbeats maintain source liveness; they do not extend the lifetime of a stale DIRECT target. END or control-socket loss ends that operation and revokes its permission. Reconnection requires an explicit new operation and fresh pickup; it never resumes old commands.

### MODEL and TRAJECTORY over HTTP STREAM

1. Start `/api/operate` with the corresponding type and `mode: "STREAM"`, with no inline points/model steps. MODEL requires a registered `profile_id`; omit that field for TRAJECTORY.
2. Poll `/api/operations/{request_id}` for `feedback.stream_ready` and a usable window. The dashboard client polls this at about 50 ms; this does not set the native motor rate.
3. POST frames to `/api/stream` with the same envelope as above and `operator_session_id`. Use `MODEL_TASK` + `{instruction}` or `JOINT_TRAJECTORY` + `{points}`.
4. For every new semantic command, supply a positive increasing `semantic_revision`. Retrying the current intent preserves its revision, `input_id` and body while using a new transport sequence and current window. Retired revisions cannot be replayed.
5. Check feedback `last_accepted_input_id` / `last_accepted_semantic_revision` before treating an update as accepted. Continue current-window HEARTBEAT frames while a sparse instruction/path remains active, even when its content has not changed. The bundled client uses approximately 100-ms heartbeats.
6. Send END through the current stream or deliberately hold the arm to finish. A completed STREAM path waits at its settled endpoint for a later replacement; it does not implicitly close the stream.

`/api/stream` returning `{"forwarded":true}` is not an acceptance receipt. `/ws/control` rejects MODEL and TRAJECTORY opening goals; do not send those modes there. An external instruction generator can run at its own semantic cadence while its transport maintains the active source.

## Reuse the browser transport

For a same-origin browser integration, the existing ES module handles identity, windows, acceptance checks, latest targets and camera URLs:

```javascript
import { StationClient } from "/assets/transport.js";

const client = new StationClient({ onState: console.log, onOperation: console.log });
await client.connect(); // Optional token argument for bearer-only access.
// The following are explicit user/application actions, not startup behavior:
// await client.enable("arm1");
// await client.startManual("arm1");
// client.setManualTarget({ positions: completeJointMap, gripper: 35, gripper_unit: "percent" });
// await client.submit("arm1", "MODEL", "STREAM", { instruction: "..." }, runtimeProfileId);
// await client.updateStream({ instruction: "..." });
// await client.endOperation();
// await client.hold("arm1");
```

`startManual` seeds pickup from measured state. Its returned promise means the transport opened, not that every target is already admitted; use operation events. This module is the dashboard client, not a separately versioned npm SDK. Native Python leader/JSON clients can inspect `python/soarm_station/operator.py`; that legacy CLI still requires `--ca` and `SOARM_STATION_TOKEN` in its environment even on trusted networks. The newer native leader-host integration supports tokenless trusted access and is documented in [leader-input.md](leader-input.md).

## Cameras, graph and errors

`GET /api/cameras/{id}.jpg` returns the cached JPEG with `X-Source-Time-Ns` and no-store caching. `/ws/camera/{id}` emits binary JPEG messages only, with no per-frame JSON timestamp envelope and no application-level command input. Its send loop is capped at 30 Hz, but actual frames follow measured capture rate. Reuse these streams for CV/dashboard viewing instead of opening the USB camera a second time.

ROS graph discovery is metadata, not a command proxy. Graph sampling creates bounded observer subscriptions (up to two topics / ten seconds), counts traffic and does not expose arbitrary payloads or publish into topics. `/api/workflows` runs a registered startup recipe; the current built-in executor is a configured color-sort workflow, not a general uploaded Python/LLM pipeline. External workflows can orchestrate the operation API directly.

Check HTTP status and response content type. Policy-management errors are structured JSON (`schema_version`, `code`, `message`, optional `details`); older dashboard routes can return plain-text 400/401/403 errors. A successful HTTP response can still contain `accepted:false`, a failed job, or an operation result with `execution_completed:false`. Preserve edits on 409 revision conflicts and inspect per-job errors before applying/running a model. See [model upload](model-weight-upload.md) and [policy-management schemas](policy-management-api.md).
