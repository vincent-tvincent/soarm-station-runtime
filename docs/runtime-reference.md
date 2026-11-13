# Runtime reference

## Configuration and resource IDs

Tracked profiles live in `config/`; deployment profiles and credentials belong under ignored `local/`. Start from `config/mock.yaml` or `config/multi_mock.yaml`. Every arm and camera has a unique ID, and operations select an arm explicitly. No physical resource is inferred from its position in a list.

An arm declares its backend, named joints, radian position bounds, velocity limits, calibration, base/TCP/model settings and gripper convention. SO-101 is the current hardware adapter. The gripper uses percent separately from ROS joint radians. A camera declares its backend/capture identity, resolution, FPS and optional measured intrinsics. Duplicate physical device paths are rejected. Shared serial buses require a bus-level adapter rather than separate owners opening the same bus.

Relative file paths resolve against the YAML file's directory. Configuration is loaded at startup. Model bundles are bound to named entries in `profiles`; clients select registered IDs rather than arbitrary paths or URLs.

The runtime's `.venv` uses distribution Python for ROS. `.runtime-venv` contains the pinned native LeRobot/PyTorch environment. A restricted Unix socket separates their Python ABIs. One worker owns motor and camera I/O, with an independent control thread per arm and one capture/encoding path per camera. See [architecture](architecture.md) for the process model.

## ROS interfaces

| Interface | Purpose |
| --- | --- |
| `/soarm_station/arms/ID/joint_states` | Measured joint positions, with canonical resource-local joint names. |
| `/soarm_station/arms/ID/lerobot/status` | Permission, operation, feedback validity/rate and measured torque state. |
| `/soarm_station/arms/ID/lerobot/inference_status` | Policy state and reason. |
| `/soarm_station/camera/ID/image_raw` | Raw camera frames, decoded when subscribed. |
| `/soarm_station/camera/ID/image_raw/compressed` | Shared JPEG frames. |
| `/soarm_station/camera/ID/camera_info` | Camera model; `K[0] = 0` means uncalibrated. |
| `/soarm_station/health`, `/soarm_station/diagnostics` | Typed motor health and standard diagnostics. |
| `/soarm_station/lerobot/operate` | `ExecuteOperation` action. |
| `/soarm_station/lerobot/set_permission` | Explicit arm control-permission service. |
| `/soarm_station/lerobot/hold` | Explicit arm hold service. |

Custom IDL lives in `ros2_ws/src/soarm_station_interfaces`. TF link names use `ID/` prefixes. Configure measured fixed transforms; camera extrinsics are not inferred. Physical telemetry preserves units and does not claim calibrated joint effort.

## Operations and permission

Each arm admits one operation at a time:

- `DIRECT`: named joint targets with a separate gripper command.
- `TRAJECTORY`: timed joint points or supported Cartesian endpoints.
- `MODEL`: a registered local or native remote policy profile.

`INLINE` carries bounded inputs. `STREAM` accepts typed `COMMAND`, `HEARTBEAT` and `END` messages after `READY`. Healthy streams have no total lifetime deadline unless requested or capped by the profile. Direct targets expire independently of heartbeats. Stream sources use host-issued 250-ms windows, increasing transport sequence numbers and current boot/session/operation identities. First pickup requires arm/gripper alignment.

Successful INLINE completion retains the healthy owner's permission and final command reference. Each subsequent motion still requires a new operation. Explicit disable, hold/cancel, END, control-WebSocket disconnect, actual source expiry, fault and restart revoke permission. Reconnection does not resume an old operation. Stateless HTTPS clients explicitly hold/disable when their sequence is finished.

Sparse MODEL/TRAJECTORY stream commands require a positive monotonic `semantic_revision`. A higher valid revision replaces intent; a retry of the current revision preserves its input ID/body and does not restart it. Retired revisions cannot execute again. DIRECT producers use latest-only targets without this field. A rebuilt interface definition is required when changing ROS participants across protocol versions.

Trajectory replacements begin from the admitted commanded reference, rather than lagging measurements. A STREAM trajectory remains active at its settled endpoint and can accept a later replacement. This supports bounded path replacement; it is not a general rolling-chunk scheduler. Body-only trajectories preserve the existing gripper grasp reference. Expected gripper contact does not automatically freeze body advancement.

A 100-Hz per-arm loop owns final velocity/lead-bounded dispatch. Default target lead is 0.12 rad; default joint completion tolerance is 0.03 rad, overridable by a qualified profile. Model timing follows the policy's trained cadence. Camera decoding and model work remain outside the device thread. Shutdown/hold preserves support torque.

### Configurable work limits

Per-arm `operation_limits` supports:

| Key | Default |
| --- | --- |
| `max_inline_duration_s` | 3600 |
| `max_path_duration_s` | 3600 |
| `default_model_step_duration_s` | 30 |
| `max_model_step_duration_s` | 3600 |
| `settle_timeout_s` | 2 |
| `max_stream_duration_s` | `null` (no total STREAM limit) |

Optional joint/gripper `tracking_timeout_s` is either a positive value or a mapping for DIRECT/TRAJECTORY/MODEL; `null` disables that inferred-stall policy. Source freshness, measured feedback, device faults and torque loss remain distinct checks.

Cartesian endpoints use LeRobot/Placo with explicit base frames, bounded solve time, joint bounds, independent FK residual checks and timed joint paths. Position-only and strict-pose endpoints are supported. Tool-axis tasks, requested positive collision clearance, derivative trajectories, coordinated multi-arm planning and guaranteed continuous Cartesian paths reject explicitly. The supplied SO-101 model is for kinematics/TF and has no mesh/collision geometry.

## Dashboard and operator API

The dashboard uses HTTPS and authenticated WebSocket connections. Tokens are read from the environment variable named by configuration; HTTPS keys/certificates are host-local. The static login page is public; state, media and control endpoints authenticate. Control and camera sockets are separate.

Authenticated routes include:

| Method | Route |
| --- | --- |
| GET | `/api/status`, `/api/history`, `/api/cameras/ID.jpg` |
| POST | `/api/permission`, `/api/hold`, `/api/operate`, `/api/stream`, `/api/workflows` |
| GET | `/api/operations/REQUEST_ID` |
| WSS | `/ws/control`, `/ws/camera/ID` |

HTTP clients use bearer headers. Browser sockets use the `soarm_station` subprotocol and token, never query-string credentials. The native operator CLI can sample a local LeRobot leader:

```bash
python -m soarm_station.operator --url https://robot.example.net:8443 --ca robot-cert.pem \
  --arm arm1 --leader local/leader.yaml --hz 100
```

Run that CLI in a native LeRobot environment, with its operator token provisioned. The leader YAML supplies explicit teleoperator configuration/degree units. Without `--leader`, the CLI accepts newline JSON targets after receiving a host window. `--hold-measured` is an explicit diagnostic input. Operation feedback is restricted to authenticated API/action clients; commands, windows and tokens are excluded from telemetry/bags.

## Policy deployment

The separate training repository exports bundles containing weights, saved processors, named state/action mappings, units, camera mappings, action FPS, calibration revision, hashes and evaluation metadata. Bind a verified bundle to a named `profiles` entry.

Local inference uses LeRobot checkpoints/processors. Remote inference uses released `RemoteClient`/`RemoteInferenceEngine` with Zenoh. Start the compute-side server with `scripts/serve-policy CONFIG` using `config/policy-server.example.yaml`. The example binds loopback; configure protected connectivity for another host. Each native deployment owns one model/session.

Weights remain resident across task resets. Inference exposes ACTION/WAIT/HOLD/FAULT and acknowledges actual owner dispatch/hold. Cached actions retain original provenance and policy cadence; delayed outputs are not restamped as fresh. A successful adapter integration test does not establish physical task success.

## Telemetry, recording and releases

Telemetry commits to SQLite before publication. Monitoring schedules persist across restart; missed periods become invalid gap records. The upload outbox retains `(device_id, runtime_boot_id, sequence)` keys until an HTTPS collector acknowledges durable commit. Unacknowledged records are not silently deleted when storage reaches its bound. Drift and availability decisions produce debounced, durably acknowledged incidents.

Deploy `scripts/run-collector` on a collector host and configure `collector.url`, CA trust and its dedicated token in the robot profile. An empty URL leaves records queued locally. Browser access alone does not establish collector receipt.

`recording.enabled` starts optional MCAP evidence recording. The allowlist includes measured state, images/CameraInfo, TF and events, excluding commands/action internals. ROS bags are separate from LeRobot training datasets. Replay uses an isolated graph without physical devices.

`soarm-station-release keygen/sign/verify` provides Ed25519 archive manifests and rejects altered signatures/digests. Keep signing keys under ignored `local/` and verify an archive before deployment. DDS uses host-local CA credentials on domain 71. Launched processes use `TZ=UTC` for the tested Fast DDS policy-date behavior; the host timezone is unchanged.
