# SO-Arm Station Runtime

SO-Arm Station is a ROS 2 application for operating configurable sets of cameras and independent robot arms on Linux. It provides joint control, timed trajectories, learned-policy execution, camera streaming, a private-network web dashboard, and persistent telemetry. SO-101 is the current hardware adapter; mock arms and cameras support development without devices.

The runtime owns devices and dispatches commands. Demonstration collection, dataset review and model training belong to the separate SO-Arm Station training repository. Other robot families require an adapter. Coordinated multi-arm planning is not implemented.

Repository: [vincent-tvincent/soarm-station-runtime](https://github.com/vincent-tvincent/soarm-station-runtime). Companion repository: `soarm-station-training` (separate).

## Requirements

The tested installation uses Ubuntu 26.04 aarch64, ROS 2 Lyrical and system Python 3.14. The bootstrap creates a separate Python 3.12 environment for LeRobot 0.7.0 and CPU PyTorch 2.11.0. Other Linux/ROS combinations require validation; see [installation coverage](docs/reproducibility-2026-11-13.md).

Install these prerequisites before running the bootstrap:

- ROS 2, including the ROS CLI, SROS2, standard message packages, TF and `robot_state_publisher`.
- Colcon, CMake, a C/C++ compiler, Python venv support, and distribution Python YAML/NumPy packages.
- Network access to download Python packages and the native Python runtime.

Use Bash for the commands below. Keep the complete repository: its Python core, ROS workspace, configuration and web assets are used together.

## Quick start: mock devices

Clone the runtime repository, then run:

```bash
git clone https://github.com/vincent-tvincent/soarm-station-runtime.git
cd soarm-station-runtime
export ROS_DISTRO=lyrical
scripts/bootstrap
.venv/bin/python scripts/init-local --name localhost
scripts/init-security

source /opt/ros/lyrical/setup.bash
source ros2_ws/install/setup.bash
ros2 launch soarm_station_system system.launch.py \
  config:="$PWD/config/mock.yaml" log_level:=info respawn:=false
```

This starts one mock arm and one mock camera without opening physical devices. Open `https://localhost:8443/` using the generated certificate at `local/tls/cert.pem` as a trust anchor. The dashboard connects automatically from loopback or a Tailscale peer with the default access configuration. Other clients, or deployments with `web.trusted_network_access: false`, require the configured bearer token. Press Ctrl-C in the launch terminal to stop the graph.

No systemd service or manual venv activation is required. The launch file selects the ROS and native interpreters and prepares child-process environments. Motion permission starts disabled.

For multiple mock resources, select `config/multi_mock.yaml`. For detailed installation, debugging and shutdown instructions, see [Startup and debugging](docs/startup-and-debug.md).

## Configure a deployment

Create a host profile under the ignored `local/` directory:

```bash
cp config/mock.yaml local/robot.yaml
# Edit local/robot.yaml for the target devices and application settings.
ros2 launch soarm_station_system system.launch.py config:="$PWD/local/robot.yaml"
```

The copied profile initially uses mock devices. Hardware deployment requires adapter settings, stable device paths, calibration, joint limits and any model or measured transform assets. Each arm and camera has an explicit unique ID. Resource counts come from configuration; no default physical arm is selected. Relative paths resolve against the YAML file's directory.

Set the telemetry file location with the profile's top-level `database` field.
For example, to use an SSD mounted at `/mnt/ssd`:

```yaml
database: /mnt/ssd/soarm-station/telemetry.sqlite3
monitoring:
  storage_mount: /mnt/ssd
  period_s: 10
  retention_days: 30
  storage_limit_mb: 512
```

`storage_mount` is optional; when set, telemetry verifies the mount before
creating or opening the database. Changing the path does not move existing
history automatically. See [storage configuration and migration](docs/runtime-reference.md#telemetry-storage-location).

For remote dashboard access, initialize the certificate with the hostname used by clients, for example `.venv/bin/python scripts/init-local --name robot.example.net`, before first launch. Initialization preserves existing credentials. Tokens, private keys, calibration, datasets and generated environments are excluded from Git.

The runtime uses authenticated DDS on domain 71 with localhost discovery. Remote operators normally use HTTPS/WebSocket. See the [runtime reference](docs/runtime-reference.md) for resource configuration, control contracts, model deployment and telemetry.

## Plug in an external program or AI agent

An external application can use HTTPS/WSS without installing ROS or LeRobot. Keep its detection, task logic, prompts and MCP/LLM harness in that application; send the resulting robot targets or policy tasks to Station. The runtime remains the owner of the arm and cameras.

| Your application produces | Station entry point |
| --- | --- |
| Named joint targets, including an LLM agent's output | `DIRECT` operation; `INLINE` for a bounded sequence or `/ws/control` for a continuous `STREAM`. |
| An XYZ target from CV/YOLO or other planning code | `TRAJECTORY` + `INLINE` + `CARTESIAN_TARGET` through `/api/operate`. Supply an explicit robot frame and metres; pixel/depth-to-robot-frame conversion belongs to your application. |
| An already planned joint path | `TRAJECTORY` + `INLINE`, or HTTP `STREAM` for successive path replacements. |
| A trained ACT policy | Prepare a local/remote execution profile, then start `MODEL` using its returned `runtime_profile_id`. ACT normally has no language instruction. |
| A VLA instruction or sequence of instructions | `MODEL` + `INLINE` for bounded steps, or HTTP `STREAM` with acknowledged instruction revisions. |
| Camera images, measured joints or system health | `/ws/camera/{camera_id}`, `/api/cameras/{camera_id}.jpg`, `/api/status` and `/api/resources`. |

The normal control sequence is **discover resources → read current state → explicitly grant permission → start an operation → consume feedback → stop/hold when finished**. Use the same client-generated `operator_session_id` for permission, operation and stream requests. Read `runtime_boot_id` and `controller_session` from current `/api/status`; do not hard-code them. Resource IDs and joint names come from `/api/resources`. Body joints use radians; the gripper uses a separate calibrated percentage, 0 closed / 100 open.

`POST /api/operate` returns a gateway `request_id`. Poll `/api/operations/{request_id}` for acceptance progress and completion; its native `operation_id` is a different identifier. STREAM commands use the current feedback window and increasing transport sequence. `/api/stream` returning `forwarded: true` only means the gateway forwarded the frame—check `last_accepted_input_id` and, for MODEL/TRAJECTORY, `last_accepted_semantic_revision` in operation feedback.

Read [the external API integration guide](docs/web-api.md) for exact JSON payloads, a runnable read-only Python example, permission/operation examples, stream framing and browser-client reuse. It also explains the difference between bounded Cartesian IK, manual IK calculation and continuous joint control. The approximately 100 Hz device loop is independent of UI polling and the trained policy's inference cadence.

### Connection and authentication

The default gateway is `https://ROBOT_HOST:8443`; WebSockets use `wss://ROBOT_HOST:8443`. Port and bind address are deployment configuration. Use the installation's CA/certificate rather than disabling TLS verification.

- Default trusted access recognizes the actual loopback/Tailscale socket peer. It does not trust forwarded headers. Ordinary LAN clients require `Authorization: Bearer TOKEN` unless their connection reaches the gateway through a recognized trusted peer.
- Headless clients can send the bearer header on HTTPS and WebSocket handshakes. Browser WebSockets use `soarm_station` as the subprotocol; bearer-only browser deployments may use `['soarm_station', token]`. Credentials never belong in URLs.
- Mutations and WebSockets reject a supplied `Origin` that differs from the gateway origin. This is not a general cross-origin browser API. Serve an embedded UI from the same origin, or use your application's backend as the integration client.
- Access to the API does not grant motion permission. Model upload, profile saving, inspection and preparation do not start an operation.

Read-only connection check:

```bash
STATION_URL=https://robot.example.net:8443
STATION_CA=/path/to/robot-cert.pem
curl --fail --cacert "$STATION_CA" "$STATION_URL/api/resources"
# For a client requiring bearer authentication, add:
#   -H "Authorization: Bearer $SOARM_STATION_TOKEN"
```

## Web API tables

Paths below are relative to the gateway. `{arm_id}`, `{camera_id}`, `{host_id}` and model/profile/job IDs are explicit values returned by discovery or creation responses. JSON bodies use `Content-Type: application/json`, except binary upload chunks. WSS denotes a WebSocket upgrade, not a JSON polling request. Automatic HEAD handling for GET routes is omitted.

### Status, cameras and ROS graph

| Method | Path | Purpose / key input |
| --- | --- | --- |
| GET | `/api/resources` | Arms, named joints/limits, cameras, capabilities and registered runtime policy IDs. |
| GET | `/api/status` | Cached measured arm/joint/per-motor state, camera IDs, inference, workflows, events and upload status. |
| GET | `/api/history` | Telemetry query: `?arm={arm_id}&after={sequence}&limit={count}`; at most 500 rows per request. |
| GET | `/api/cameras/{camera_id}.jpg` | Latest JPEG; source timestamp in `X-Source-Time-Ns`. |
| WSS | `/ws/camera/{camera_id}` | New camera frames as binary JPEG messages; no camera-device ownership transfer. |
| GET | `/api/v1/ros/graph` | Discovered ROS nodes, topics, services, actions, QoS and sampling metadata. Alias: `/api/graph`. |
| POST | `/api/v1/ros/watch` | Acquire/renew/release a scoped continuous graph-watch lease; body contains `operation` and `client_id` or `lease_id`. |
| POST | `/api/v1/ros/sample` | Observe topic traffic: `{topic, duration_s}` or `{topic, stop:true}`. Alias: `/api/graph/sample`; no message-payload export or robot command. |

### Operations and kinematics

| Method | Path | Purpose / key input |
| --- | --- | --- |
| POST | `/api/permission` | `{arm_id, controller_session, operator_session_id, enable}`; inspect returned `accepted`, `permission`, `reason`. |
| POST | `/api/operate` | Start `DIRECT`, `TRAJECTORY` or `MODEL` in `INLINE`/`STREAM` mode; returns `request_id`. |
| GET | `/api/operations/{request_id}` | `done`, `feedback` and eventual `result`; not a durable job-history API. |
| POST | `/api/stream` | Forward a current-window `COMMAND`, `HEARTBEAT` or `END` frame for an admitted HTTP stream. |
| WSS | `/ws/control` | **DIRECT + STREAM only**: opening goal, grant/state feedback and named joint targets. `input_semantics` selects persistent manual intent or the default live samples; see [stream semantics](docs/web-api.md#direct-control-websocket). |
| POST | `/api/hold` | `{arm_id}`; arm-wide hold, ends the current operation and revokes permission while retaining support torque. |
| POST | `/api/workflows` | `{arm_id, operator_session_id, definition_id}`; run an already configured workflow definition. Does not upload arbitrary task code. |
| GET | `/api/manual/{arm_id}/describe` | Kinematic capability, frames, key map, speed levels and available named poses. |
| POST | `/api/manual/{arm_id}/fk` | Calculate geometry from `{positions, gripper}`; optional target overlay. No actuation. |
| POST | `/api/manual/{arm_id}/step` | Calculate one incremental IK target from measured/reference joints, keys and `dt_s`. No actuation. |
| POST | `/api/manual/{arm_id}/preset` | Calculate configured Home/Fold target using `{positions, gripper, name, preserve_wrist?}`. No actuation. |
| POST | `/api/manual/{arm_id}/roll` | Calculate a configured wrist-roll target using `{positions, gripper, name}`. No actuation. |

### Remote leader input hosts

These routes coordinate an installed native input-host process. They are not required for a generic LLM/CV client. See [leader setup and payloads](docs/leader-input.md).

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/input-hosts` | Live connected hosts and OS serial-device inventory. |
| WSS | `/ws/input-host` | Native host registration, inventory/status and start/stop coordination. |
| POST | `/api/input-hosts/{host_id}/start` | Start the selected configured leader for an explicitly permitted arm. |
| POST | `/api/input-hosts/{host_id}/heartbeat` | Renew the matching active leader run's browser lease. |
| POST | `/api/input-hosts/{host_id}/settings` | Update matching run settings such as `follow_gripper`. |
| POST | `/api/input-hosts/{host_id}/stop` | Stop the identified leader run. |

### Model sources, catalog and profiles

These are metadata/configuration operations; they do not move the robot. [Exact schemas and response envelopes](docs/policy-management-api.md).

| Method | Path | Purpose / key input |
| --- | --- | --- |
| GET | `/api/v1/policy-sources` | Loaded source list, paths, availability, revision and fixed profile-store location. |
| POST | `/api/v1/policy-sources/reload` | Read the host source-config file again; optional `expected_revision`. Starts scans of changed sources. |
| GET | `/api/v1/policies` | Catalog search across roots: `?q={text}&source_id={optional_id}`. |
| GET | `/api/v1/policies/{model_id}` | Artifact metadata, input contract, revision and full file inventory. |
| POST | `/api/v1/policy-scans` | Rescan loaded roots: `{}` or `{source_ids:[...]}`; returns `job`. |
| GET | `/api/v1/policy-scans/{job_id}` | Scan progress and errors. |
| GET | `/api/v1/policy-profiles` | Saved execution-profile summaries. |
| POST | `/api/v1/policy-profiles` | Create from `{text}` or `{document}`; never overwrites. |
| GET | `/api/v1/policy-profiles/{profile_id}` | Raw text, parsed document, revision and validation. |
| PUT | `/api/v1/policy-profiles/{profile_id}` | Save raw text/document with `expected_revision`; external edits produce 409. |
| POST | `/api/v1/policy-profiles/validate` | Validate `{text}` or `{document}` without saving/applying. |
| POST | `/api/v1/policy-profiles/refresh` | Rediscover profile files changed outside the UI; body `{}`. |
| POST | `/api/v1/policy-profiles/import-preview` | Review `{text}` and any ID conflict without saving. |
| POST | `/api/v1/policy-profiles/import` | Save reviewed `{text, id, mode, expected_revision?}`; mode `new`/`replace`. |
| GET | `/api/v1/policy-profiles/{profile_id}/export` | Download profile with `?format=yaml` or `json`. |

### Policy preparation and model uploads

| Method | Path | Purpose / key input |
| --- | --- | --- |
| POST | `/api/v1/policy-profiles/inspect-draft` | Read a remote descriptor for `{text}`/`{document}`; may return an asynchronous job. |
| POST | `/api/v1/policy-profiles/{profile_id}/inspect` | Inspect a saved `expected_revision`; no remote session admission. |
| POST | `/api/v1/policy-profiles/{profile_id}/prepare` | `{expected_revision, mode:"idle"}` or `mode:"after_current"`; prepare saved profile without starting motion. |
| GET | `/api/v1/policy-preparations/{job_id}` | Inspect/preparation job and terminal result. |
| GET | `/api/v1/policy-runtime` | Per-arm applied/pending profiles, capabilities and immutable `runtime_profile_id` values used by MODEL operations. |
| POST | `/api/v1/model-uploads` | Create upload: `{source_id, destination_name, kind, files}`; `kind` is `folder`/`archive`. |
| GET | `/api/v1/model-uploads` | Upload activity list. |
| GET | `/api/v1/model-uploads/{upload_id}` | Full upload detail and chunk receipts for resumption. |
| PUT | `/api/v1/model-uploads/{upload_id}/files/{file_index}/chunks/{offset}` | Raw bytes, at most 131072 bytes, plus `X-Chunk-SHA256`; index is zero-based, offset is bytes. |
| POST | `/api/v1/model-uploads/{upload_id}/finalize` | Verify, publish and index staged files; body `{}`. |
| POST | `/api/v1/model-uploads/{upload_id}/cancel` | Cancel only this upload's staging; body `{}`. |

The web UI is served by `GET /` and allowlisted `GET /assets/{file}`. There is no generic filesystem browser, weight download endpoint, checkpoint-code installer or arbitrary ROS publish/service-call proxy. Host files are managed using ordinary development tools.

### Optional telemetry collector — separate server

The collector is a separate HTTPS service, normally on port 9443, with its own required `SOARM_STATION_COLLECTOR_TOKEN` bearer credential. These routes are **not** mounted on the robot dashboard. See [collector setup](docs/runtime-reference.md#telemetry-recording-and-releases).

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/v1/samples` | Accept 1–128 telemetry samples and return receipt keys only after durable commit. |
| GET | `/v1/status` | Number of durable receipts. |

## Upload and use your own model weights

Export a complete inference checkpoint from the separate training workflow: model configuration, learned weights, saved pre/postprocessors and their referenced state files. A lone `.pt`/`.safetensors` file is not a complete executable policy. The architecture must have a compatible implementation installed in the native LeRobot environment, and the trained joint units/order, camera inputs and cadence must be known. Uploading does not install Python implementations or convert an arbitrary network into ACT/VLA.

1. **Configure storage.** The default source in `config/model-sources.yaml` points to `local/models`. Create it with `mkdir -p local/models`. Add other host/SSD folders to that YAML using unique source IDs and `allow_upload: true` when uploads are desired. Source paths resolve relative to the source YAML, not the browser's filesystem.
2. **Reload or refresh.** After editing source configuration, choose **Policies → Sources → Reload source config**. After copying weights into an already loaded source using SSH/VS Code/SCP, choose **Refresh models** instead.
3. **Upload through the UI.** Open **Policies → Uploads**, select **Checkpoint folder** or **ZIP archive**, choose a configured writable destination source and a new model-directory name, then upload. Include companion files. Transfers are chunked, can be paused/resumed with the original bundle, and become selectable only after verification/publication/cataloging. Existing model directories are not overwritten.
4. **Create a profile.** In **Model library**, search across source groups, select the checkpoint and choose **Create profile**. Bind an explicit arm and each required camera to its trained input key. The three-role SO-ARM template appears only for a compatible Global/Wrist/Additional schema; other models use checkpoint-defined inputs. Form and Raw YAML/JSON edit the same profile.
5. **Save and apply.** Save the profile, resolve validation issues, then select **Apply model**. This pins the saved profile and artifact revisions and loads/prepares the selected local policy, or verifies the remote descriptor. Inspect the result; catalog availability alone is not native readiness. Apply does not start the arm.
6. **Run explicitly.** Use **Run policy**, or take the per-arm prepared `runtime_profile_id` from `/api/v1/policy-runtime` and send a MODEL operation through the API after explicit permission. Choose a bounded run or continuous instruction stream as appropriate for the policy.

For remote inference, the weights belong on the **inference-server computer**. Configure that server's model first, then make a remote profile with its endpoint, deployment and expected artifact identity. Uploading to the Pi does not forward weights to that server. The Pi keeps bindings and the execution profile; model installation/loading on the remote host remains under that host's tools.

See [the complete model upload guide](docs/model-weight-upload.md) for folder layout, source YAML, trained-contract metadata, raw profile import, programmatic chunk upload, native remote-server setup and troubleshooting.

## Development

For foreground debugging, pass `log_level:=debug respawn:=false`. Prepare a second terminal for ROS CLI inspection:

```bash
source scripts/setup.bash config/mock.yaml
ros2 node list --no-daemon --spin-time 5
ros2 topic echo /soarm_station/arms/arm1/joint_states --once \
  --qos-reliability best_effort
scripts/test
```

The [debugging guide](docs/startup-and-debug.md) also covers individual `ros2 run` commands, interpreter selection, rebuilds and IDE debugging.

| Path | Purpose |
| --- | --- |
| `python/soarm_station/` | Device ownership, control, policy execution, IPC and persistence. |
| `ros2_ws/src/soarm_station_system/` | ROS node adapters and launch entry point. |
| `ros2_ws/src/soarm_station_interfaces/` | Messages, services and actions. |
| `config/` | Tracked mock and policy-server examples. |
| `scripts/` | Bootstrap, local initialization, startup and development commands. |
| `web/` | Dashboard assets. |
| `tests/` | Core and ROS integration checks. |
| `local/` | Ignored host configuration, credentials and runtime data. |

## Optional service deployment

After configuring a deployment profile, install a user systemd service with an explicit absolute config path:

```bash
scripts/install-service "$PWD/local/robot.yaml"
systemctl --user status soarm-station.service
journalctl --user -u soarm-station.service -f
```

The installer enables and starts the service. Stop it before launching the foreground graph with the same resources. Service startup invokes `scripts/run`, which enables application-node respawn. Direct `ros2 launch` defaults to no respawn for debugging. See [service management](docs/startup-and-debug.md#optional-systemd-service) for update, logout and removal behavior.

## Documentation and current scope

See the [name migration](docs/name-migration.md) when updating clients or installations using the earlier SOARM names.

- [Startup and debugging](docs/startup-and-debug.md): first installation, foreground launch, ROS CLI, single-node runs and service management.
- [Architecture](docs/architecture.md): process ownership, node responsibilities and timing.
- [Policy/model management](docs/policy-management.md): file-owned sources, versioned profiles, uploads and no-motion preparation.
- [Policy editor](docs/policy-management-frontend.md) and [deployment verification](docs/policy-management-deployment-2026-11-13.md): editor behavior, upload transport and the deployed third workspace.
- [GitHub deployment](docs/github-deployment.md): source updates, Pi read-only access and activation boundaries.
- [Dashboard UI](docs/dashboard-ui.md): camera layouts, operations panels and generic graph interaction.
- [Dashboard backend API](docs/dashboard-api.md): motor/resource metadata, generic ROS discovery and bounded sampling.
- [Runtime reference](docs/runtime-reference.md): configuration, interfaces, operations, policy bundles and telemetry.
- [Naming validation](docs/naming-validation-2026-11-13.md): package migration, retained state and current service verification.
- [Installation evidence](docs/reproducibility-2026-11-13.md) and [foreground validation](docs/foreground-validation-2026-11-13.md): tested environments and verification limits.
- [Physical validation](docs/physical-motion-validation-2026-11-12.md): measured behavior of the qualified SO-101 deployment.

The application supports independent arm operation, position/time trajectories and registered local or native remote policies. It does not provide coordinated multi-arm planning, collision checking, force control or guaranteed continuous Cartesian paths. A successful startup or software integration test does not establish learned manipulation success. Deployment-specific addresses and profiles are documented separately in [the reference Pi deployment](docs/deployments/pi.md).
