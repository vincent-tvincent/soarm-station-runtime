# SOArm Station Runtime

SOArm Station is a ROS 2 application for operating configurable sets of cameras and independent robot arms on Linux. It provides joint control, timed trajectories, learned-policy execution, camera streaming, a private-network web dashboard, and persistent telemetry. SO-101 is the current hardware adapter; mock arms and cameras support development without devices.

The runtime owns devices and dispatches commands. Demonstration collection, dataset review and model training belong to the separate SOArm Station training repository. Other robot families require an adapter. Coordinated multi-arm planning is not implemented.

Repository: `soarm-station-runtime`. Companion repository: `soarm-station-training`.

## Requirements

The tested installation uses Ubuntu 26.04 aarch64, ROS 2 Lyrical and system Python 3.14. The bootstrap creates a separate Python 3.12 environment for LeRobot 0.7.0 and CPU PyTorch 2.11.0. Other Linux/ROS combinations require validation; see [installation coverage](docs/reproducibility-2026-11-13.md).

Install these prerequisites before running the bootstrap:

- ROS 2, including the ROS CLI, SROS2, standard message packages, TF and `robot_state_publisher`.
- Colcon, CMake, a C/C++ compiler, Python venv support, and distribution Python YAML/NumPy packages.
- Network access to download Python packages and the native Python runtime.

Use Bash for the commands below. Keep the complete repository: its Python core, ROS workspace, configuration and web assets are used together.

## Quick start: mock devices

Clone or extract the runtime repository, then run these commands from its root:

```bash
export ROS_DISTRO=lyrical
scripts/bootstrap
.venv/bin/python scripts/init-local --name localhost
scripts/init-security

source /opt/ros/lyrical/setup.bash
source ros2_ws/install/setup.bash
ros2 launch soarm_station_system system.launch.py \
  config:="$PWD/config/mock.yaml" log_level:=info respawn:=false
```

This starts one mock arm and one mock camera without opening physical devices. Open `https://localhost:8443/`, trust the generated certificate at `local/tls/cert.pem`, and enter the `SOARM_STATION_TOKEN` value from `local/secrets.env`. Press Ctrl-C in the launch terminal to stop the graph.

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

For remote dashboard access, initialize the certificate with the hostname used by clients, for example `.venv/bin/python scripts/init-local --name robot.example.net`, before first launch. Initialization preserves existing credentials. Tokens, private keys, calibration, datasets and generated environments are excluded from Git.

The runtime uses authenticated DDS on domain 71 with localhost discovery. Remote operators normally use HTTPS/WebSocket. See the [runtime reference](docs/runtime-reference.md) for resource configuration, control contracts, model deployment and telemetry.

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
- [Dashboard UI](docs/dashboard-ui.md): camera layouts, operations panels and generic graph interaction.
- [Dashboard backend API](docs/dashboard-api.md): motor/resource metadata, generic ROS discovery and bounded sampling.
- [Runtime reference](docs/runtime-reference.md): configuration, interfaces, operations, policy bundles and telemetry.
- [Naming validation](docs/naming-validation-2026-11-13.md): package migration, retained state and current service verification.
- [Installation evidence](docs/reproducibility-2026-11-13.md) and [foreground validation](docs/foreground-validation-2026-11-13.md): tested environments and verification limits.
- [Physical validation](docs/physical-motion-validation-2026-11-12.md): measured behavior of the qualified SO-101 deployment.

The application supports independent arm operation, position/time trajectories and registered local or native remote policies. It does not provide coordinated multi-arm planning, collision checking, force control or guaranteed continuous Cartesian paths. A successful startup or software integration test does not establish learned manipulation success. Deployment-specific addresses and profiles are documented separately in [the reference Pi deployment](docs/deployments/pi.md).
