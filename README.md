# SOARM Linux workstation

A configuration-driven ROS 2 application for **n cameras and m independent arms**. SO-101 is the first real arm adapter; mock adapters support development without devices. Other robot families require an adapter, not a new application architecture. Coordinated multi-arm motion is not enabled.

The Pi deployment is `/home/pipi/so-101/ros_system`. This repository is independent of `../training`, has no configured Git remote, and has not been pushed. Initial implementation was produced with Codex assistance.

## Run the deployed system

```bash
systemctl --user status soarm.service
journalctl --user -u soarm.service -f
# Explicit foreground launch after stopping the service:
systemctl --user stop soarm.service
scripts/run local/pi.yaml
```

The dashboard is **https://ubuntu-pi.tail521c60.ts.net:8443/**. Its development certificate is at `local/tls/cert.pem`; trust that certificate on the operator device. Enter the operator token stored on this host in `local/secrets.env`. Private credentials, calibration and device paths are ignored by Git. Do not copy the entire secret file to an operator computer.

The active Pi profile is **`local/pi-writable.yaml`**, using the native LeRobot motor bus. The user subsequently approved live native connection and controlled motion. Real joint/gripper, coordinated trajectory, streamed input, disconnect, expiry and restart tests passed under the declared coarse accuracy profile; see [physical qualification](docs/physical-motion-validation-2026-11-12.md). Motion permission remains disabled after startup/hold. The arm is currently holding its compact pose with all six torques ON. `local/pi.yaml` remains the strict read-only fallback.

## Environments and installation

The committed bootstrap was verified from clean local Git checkouts with new environments on the existing supported Pi OS/ROS stack. See [the reproducibility evidence and its limits](docs/reproducibility-2026-11-13.md). This does not establish a clean-OS or cross-distribution installation guarantee.

ROS Lyrical on Ubuntu 26.04 uses distribution Python 3.14. `.venv` uses that Python with system site packages. `.runtime-venv` uses Python 3.12 and LeRobot 0.7.0 / CPU PyTorch 2.11.0. A permission-restricted Unix socket keeps the Python/native ABIs separate. Motor and camera devices have one owner; per-arm threads prevent another arm or camera from blocking control.

On another compatible Linux host, install ROS 2, colcon, Python venv support and the distribution's NumPy/YAML dependencies, then:

```bash
scripts/bootstrap
.venv/bin/python scripts/init-local --name YOUR_HOSTNAME
scripts/init-security
scripts/run config/multi_mock.yaml
```

The lock files reproduce the tested Linux CPU environments. GPU training belongs in the training repository's separate GPU environment. Choose a ROS distribution whose Python ABI matches the host; set `ROS_DISTRO` when the ROS installation is not Lyrical. Runtime defaults require authenticated DDS on domain 71. `TZ=UTC` applies only to launched processes because Fast DDS 3.6.2 parses policy UTC validity dates using local `mktime`; the host timezone is unchanged.

## Configuration and resource model

Start with `config/mock.yaml` or `config/multi_mock.yaml`; create a host profile under `local/`. Every arm and camera has a unique ID. An arm declares its backend, named joints, radian bounds/velocity limits, calibration, base/TCP/model, and separate gripper-percent convention. A camera declares its capture identity, resolution/FPS and optional measured intrinsics. No camera count or arm default is inferred. Duplicate physical paths are rejected. Shared serial buses need a bus-level adapter; two owners may not open the same bus.

Each arm publishes `/soarm/arms/ID/joint_states`, `lerobot/status` and `lerobot/inference_status`. Camera streams are `/soarm/camera/ID/image_raw`, `image_raw/compressed` and `camera_info`. Joint names are canonical inside their resource-scoped topic; TF link names use `ID/` prefixes. CameraInfo with K[0] = 0 explicitly means uncalibrated. Configure only measured fixed transforms; the installation does not guess extrinsics.

The original fixed three-camera/single-arm plan was generalized to resource-scoped IDL. The generic Hold service replaces the reference plan's unscoped Trigger service with an explicit arm selector. Motor health uses typed telemetry fields and a standard DiagnosticArray, preserving physical units without claiming calibrated joint effort. See `docs/architecture.md`.

## Continuous operation update

The November13 continuity revision is documented in [the dated report](docs/continuous-operation-2026-11-13.md). Healthy STREAM operations now have no total lifetime deadline unless explicitly requested or capped by the deployment profile. Normal INLINE completion retains the same operator/controller permission and final command reference; each new motion still requires an explicit new operation. Stateless HTTPS callers explicitly hold/disable when their sequence is finished. Control-WebSocket disconnect, END/cancel, actual source expiry, fault and restart still revoke. The UI displays an enabled idle grant as waiting for a new operation.

Streamed trajectory replacements start from the admitted commanded reference, not lagging measured pose; a settled endpoint remains active for later updates. This is replacement of a bounded position/time path, not queued rolling-chunk scheduling. MODEL/TRAJECTORY stream commands require a positive monotonic `semantic_revision`; retries preserve revision+input_id+body and only the current revision can retry. Retired revisions never execute again. DIRECT JSON producers remain latest-only and need no new field. All ROS participants must rebuild for the new message fields. Reload/update clients that explicitly hardcoded120s; explicit durations remain honored.

Joint/body trajectories preserve an existing gripper grasp reference. Expected gripper contact/compliance does not freeze body advancement or force a half-second stall revocation. Velocity/lead limits remain; optional no-progress policies are actuator/mode-specific and default off. Native model weights remain resident across task resets; ACTION/WAIT/HOLD/FAULT appears in inference diagnostics and dashboard status, with real owner hold before native acknowledgement. Unconditioned ACT profiles allow empty instructions. Cached actions retain original source time and native cadence/eligibility; increasing a generic TTL is not the mechanism.

Per-arm `operation_limits` may configure `max_inline_duration_s` (3600), `max_path_duration_s` (3600), `default_model_step_duration_s` (30), `max_model_step_duration_s` (3600), `settle_timeout_s` (2), and optional `max_stream_duration_s` (null by default). These finite work limits are independent of continuous session lifetime. `tracking_timeout_s` is optional per joint/gripper, either a positive number or a mapping for DIRECT/TRAJECTORY/MODEL; null disables that optional inferred-stall policy. Existing source/torque/device-fault guards still apply.

## Operations and remote control

`/soarm/lerobot/operate` is a typed ExecuteOperation action. Each arm admits one operation: DIRECT named joint targets, TRAJECTORY timed joint points / supported Cartesian endpoints, or MODEL registered local/native-remote policy. INLINE carries bounded inputs; STREAM accepts typed COMMAND/HEARTBEAT/END messages after READY. End/cancel/expiry invalidates pending work; natural INLINE completion preserves the healthy owner grant. Held streams do not resume on reconnection. Gripper percent is separate from ROS radians.

Timed-path completion uses each joint's `goal_tolerance_rad` (default 0.03 rad). The qualified Pi elbow profile declares 0.07 rad because its unchanged stock gains leave about 3.2 degrees of physical following error; this is coarse control, not precision positioning. The limiter advances from prior commanded state with a default `max_target_lead_rad` of 0.12 rad.

The stream uses host-issued 250 ms windows, increasing sequence numbers and current boot/session/operation identities. DIRECT targets expire independently of heartbeats. First pickup requires arm/gripper alignment. Sparse path/task input IDs are idempotent; retries never reactivate a superseded task. A 100 Hz per-arm loop owns final velocity/lead-bounded writes. Policy action timing remains its trained cadence. Inline operations are finite and do not require heartbeats unless their profile enables that requirement; streamed operations require source liveness.

The browser provides joint sliders and hold controls. A non-ROS native operator can use:

```bash
# SOARM_TOKEN must be provisioned only to this authorized operator.
python -m soarm.operator --url https://ROBOT_HOST:8443 --ca robot-cert.pem \
  --arm arm1 --leader local/leader.yaml --hz 100
```

The leader YAML is a native LeRobot teleoperator configuration with explicit degree units. Without `--leader`, newline JSON targets are read from stdin **after a window arrives**. The optional `--hold-measured` mode is an explicit diagnostic input, not a leader emulator. Keep control/state and camera sockets separate. The API authenticates bearer headers; browsers use the `soarm` WebSocket subprotocol plus token, never query-string credentials.

Authenticated routes: GET `/api/status`, `/api/history`, `/api/cameras/ID.jpg`; POST `/api/permission`, `/api/hold`, `/api/operate`, `/api/stream`, `/api/workflows`; GET `/api/operations/REQUEST_ID`; WSS `/ws/control` and `/ws/camera/ID`. Operation feedback is private to authenticated API/action clients. No command topic, window or operator token enters bags or telemetry.

## Training exports and inference

Collect/review/train in `../training` with production stopped and its automatic restart disabled for the handoff. Both repositories use the same physical-device lock convention. Production consumes an exported bundle containing weights, saved processors, named state/action mappings, units, camera mapping, action FPS, calibration revision, hashes and evaluation metadata. Bind that bundle to a named `profiles` entry; clients choose a registered ID rather than a path or URL.

Local inference loads LeRobot checkpoints/processors. Native remote inference uses released RemoteClient/RemoteInferenceEngine and Zenoh, with caller-owned capture of targets before guarded dispatch. `scripts/serve-policy CONFIG` starts the native server on its chosen compute host. Use `config/policy-server.example.yaml`; default examples bind loopback. Configure protected Zenoh connectivity explicitly for another host. Each native deployment owns one model/session. The ACT synthetic export and native server/client were tested; no useful SmolVLA policy or physical manipulation success is claimed.

Cartesian endpoints use LeRobot/Placo, bounded solve time, explicit base frame, joint limits, independent FK residuals and timed joint paths. Position-only and strict-pose tasks are supported. Tool-axis tasks, continuous Cartesian-path guarantees, requested positive collision clearance, derivative trajectories and coordinated multi-arm planning reject explicitly. The supplied SO101 model is for kinematics/TF and omits meshes/collision geometry.

## Monitoring and course evidence

Telemetry commits to SQLite before publication. The monitoring run and expected schedule persist across restart; missed periods become invalid gap records rather than fresh copies of current data. The outbox retains exact `(device_id, runtime_boot_id, sequence)` keys until an HTTPS collector acknowledges durable commit. Storage is bounded; unacknowledged records are not silently deleted. Parked encoder drift and feedback availability produce visible decisions and durably acknowledged incidents with debounce.

Deploy `scripts/run-collector` on a **separate server**, then set `collector.url`, CA trust and its dedicated token in the robot's local configuration. The installation tested a separate loopback collector process and exact retry/deduplication; an external wireless collector endpoint is not configured. Pi data continues to queue. Browser access alone does not establish wireless device-to-server receipt.

Optional ROS evidence recording is enabled in `recording.enabled`. Its allowlist includes measured state, images/CameraInfo, TF and events; command streams/action internals are excluded. Bags are not LeRobot training datasets. Replay must use an isolated graph without physical devices.

`soarm-release keygen/sign/verify` provides Ed25519 signed archive manifests and rejects altered signatures/digests. Signing keys stay under ignored `local/`; do not deploy an update before verification. DDS participants require the local CA credentials; private-key compromise is a trusted-host compromise, not solved by a ROS namespace.

## Verification and status

```bash
scripts/test
.venv/bin/python scripts/qualify-monitor.py --config local/pi.yaml --seconds 20 --output local/rates.json
# Mock only; stop production, run multi_mock profile, then:
set -a; source local/secrets.env; set +a
.venv/bin/python scripts/integration-check.py --ca local/tls/cert.pem
```

See `docs/validation-2026-11-12.md` for measured results and outstanding physical/course evidence. Native controlled physical operation has now been qualified as documented above. Formal 72-hour qualification, actual current measurement, full VLA training and task-level manipulation validation remain outstanding.
