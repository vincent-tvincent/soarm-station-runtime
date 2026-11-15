# Runtime reference

## Configuration and resource IDs

Tracked profiles live in `config/`; deployment profiles and credentials belong under ignored `local/`. Start from `config/mock.yaml` or `config/multi_mock.yaml`. Every arm and camera has a unique ID, and operations select an arm explicitly. No physical resource is inferred from its position in a list.

An arm declares its backend, named joints, radian position bounds, velocity limits, calibration, base/TCP/model settings and gripper convention. SO-101 is the current hardware adapter. The gripper uses percent separately from ROS joint radians. A camera declares its backend/capture identity, resolution, FPS and optional measured intrinsics. Duplicate physical device paths are rejected. Shared serial buses require a bus-level adapter rather than separate owners opening the same bus.

Set a camera's human-readable `label` in the same hardware-profile entry where its stable ID is bound to a capture device:

```yaml
cameras:
  - id: forward
    label: Workbench overview
    backend: v4l2
    device: /dev/v4l/by-id/REPLACE_WITH_VERIFIED_CAMERA_DEVICE
    width: 640
    height: 480
    fps: 30
```

The label is optional; omitting it displays the ID. An explicit label must be a nonblank string of 1–128 characters. Dashboard camera captions and policy camera choices use this label. Keep `id` stable: ROS topics, camera frame IDs, stream URLs and profile bindings continue to use `forward`, while checkpoint input keys such as `observation.images.camera1` remain unchanged. Display-label changes do not create a second camera or change its calibration/viewpoint. The hardware profile is loaded at startup; refreshing labels in a browser uses the latest resource metadata returned by the dashboard, not an automatic hardware reconfiguration.

Relative file paths resolve against the YAML file's directory. Physical configuration is loaded at startup. Model bundles can be bound to named startup entries in `profiles`, or prepared dynamically through [policy management](policy-management.md). Clients select registered runtime IDs rather than arbitrary checkpoint paths in motion requests. Saved profile IDs and the immutable prepared `runtime_profile_id` are distinct.

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

`INLINE` carries bounded inputs. `STREAM` accepts typed `COMMAND`, `HEARTBEAT` and `END` messages after `READY`. Healthy streams have no total lifetime deadline unless requested or capped by the profile. Browser/manual DIRECT WebSocket operations explicitly use `input_semantics: persistent_target`: accepted target intent remains until replaced or stopped, with no target/heartbeat inactivity expiry. Native leader and other legacy sources default to `live_sample`, whose targets expire independently of heartbeats. Stream frames still use host-issued 250-ms admission windows, increasing transport sequence numbers and current boot/session/operation identities. First pickup requires arm/gripper alignment. Socket closure cancels its own operation; reconnect does not rearm.

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

The dashboard opens automatically over HTTPS from the owner’s Tailscale connection or loopback, with no token form. Control and camera sockets are separate. Actual socket peers establish private-network access; browser origins are checked for commands and WebSockets. Existing bearer tokens remain compatible for other clients. HTTPS keys/certificates stay host-local.

The [README](../README.md#web-api-tables) contains the complete current route tables, including model management, Manual calculators, ROS discovery and input hosts. The [external integration guide](web-api.md) gives request shapes, identity/permission rules and continuous-stream examples. Core control routes include:

| Method | Route |
| --- | --- |
| GET | `/api/status`, `/api/history`, `/api/cameras/ID.jpg` |
| POST | `/api/permission`, `/api/hold`, `/api/operate`, `/api/stream`, `/api/workflows` |
| GET | `/api/operations/REQUEST_ID` |
| WSS | `/ws/control`, `/ws/camera/ID` |

The owner’s browser needs no bearer header or token. Its sockets use the `soarm_station` subprotocol. Other clients may continue using bearer headers or the existing token subprotocol convention; query-string credentials are never used. The native operator CLI can sample a local LeRobot leader:

```bash
python -m soarm_station.operator --url https://robot.example.net:8443 --ca robot-cert.pem \
  --arm arm1 --leader local/leader.yaml --hz 100
```

Run that CLI in a native LeRobot environment, with its operator token provisioned. The leader YAML supplies explicit teleoperator configuration/degree units. Without `--leader`, the CLI accepts newline JSON targets after receiving a host window. `--hold-measured` is an explicit diagnostic input. Operation feedback is restricted to authenticated API/action clients; commands, windows and tokens are excluded from telemetry/bags.

## Policy deployment

The separate training repository exports bundles containing weights, saved processors, named state/action mappings, units, camera mappings, action FPS, calibration revision, hashes and evaluation metadata. Use [Policies to upload/select a checkpoint and prepare an execution profile](model-weight-upload.md), or bind a verified bundle to a startup `profiles` entry. Use the prepared per-arm `runtime_profile_id` or registered startup ID when starting MODEL.

Local inference uses LeRobot checkpoints/processors. Remote inference uses released `RemoteClient`/`RemoteInferenceEngine` with Zenoh. Start the compute-side server with `scripts/serve-policy CONFIG` using `config/policy-server.example.yaml`. The example binds loopback; configure protected connectivity for another host. Each native deployment owns one model/session.

Weights remain resident across task resets. Inference exposes ACTION/WAIT/HOLD/FAULT and acknowledges actual owner dispatch/hold. Cached actions retain original provenance and policy cadence; delayed outputs are not restamped as fresh. A successful adapter integration test does not establish physical task success.

## Telemetry, recording and releases

Telemetry commits to SQLite on the robot host before publication. Routine history defaults to one sample per arm every **10 seconds**, **30 days** of retention and a **512 MiB** storage budget. These are `monitoring.period_s`, `retention_days` and `storage_limit_mb`. Live control/feedback and immediate drift/availability incidents use their existing independent rates; camera frames are not stored in this history. Sampling cadence changes are recorded as events, and historical gaps are never filled with current observations.

With an empty `collector.url`, history stays local. Storage and upload counters are available through the status API and are omitted from the operator dashboard. Age-based maintenance removes only expired history; opening the journal does not prune existing measurements. Event retention and quota checks also apply (`monitoring.event_limit` defaults to 10,000), and quota accounting includes SQLite's database, WAL and shared-memory files. Reaching the budget reports a logging error rather than silently discarding recent measurements. SQLite can reuse space freed by retention; file size need not shrink immediately.

To add remote delivery, deploy `scripts/run-collector` on a collector host and configure `collector.url`, CA trust and its dedicated token in the robot profile. The outbox retains exact `(device_id, runtime_boot_id, sequence)` keys until durable acknowledgment. With remote delivery configured, unacknowledged records are retained even beyond the age window; quota exhaustion is explicit. Acknowledgments are not fabricated for local-only history, and browser access alone does not establish remote collector receipt.

### Telemetry storage location

The runtime profile's top-level `database` is the single setting for the SQLite
file. Absolute paths are used directly; relative paths resolve against that
YAML file's directory. Set `monitoring.storage_mount` for a separately mounted
data volume, for example `/mnt/ssd`. Telemetry requires the database to be inside
that mount and verifies that it is mounted before creating directories or
opening SQLite. An absent data drive cannot silently create replacement history
on the underlying system disk. This check belongs to telemetry; configuration
loading for arm and camera processes does not require the data volume.

For an existing journal, stop its writer before switching paths, use SQLite's
backup API to include committed WAL content, and verify integrity plus records,
events and schedule metadata at the destination. Update `database` and the
optional `storage_mount`, then restart the telemetry and dashboard processes.
Retain a verified rollback copy until the new file is receiving records; do not
copy only a live `.sqlite3` file and omit its WAL. Changing the configuration by
itself creates/opens the selected file; it does not migrate the old history.

`recording.enabled` starts optional MCAP evidence recording. The allowlist includes measured state, images/CameraInfo, TF and events, excluding commands/action internals. ROS bags are separate from LeRobot training datasets. Replay uses an isolated graph without physical devices.

`soarm-station-release keygen/sign/verify` provides Ed25519 archive manifests and rejects altered signatures/digests. Keep signing keys under ignored `local/` and verify an archive before deployment. DDS uses host-local CA credentials on domain 71. Launched processes use `TZ=UTC` for the tested Fast DDS policy-date behavior; the host timezone is unchanged.
