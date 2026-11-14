# Startup and debugging

This guide covers installation, foreground ROS launch, individual-node debugging and optional systemd deployment. Commands run in Bash on the Linux host that owns the robot and camera devices. Paths are relative to the runtime repository root unless stated otherwise.

## Install the development environment

The tested platform is Ubuntu 26.04 aarch64 with ROS 2 Lyrical and system Python 3.14. Install ROS 2, SROS2, colcon, CMake, a C/C++ compiler, Python venv support and distribution Python YAML/NumPy packages before continuing. ROS packages must include the standard message types, TF and `robot_state_publisher` used by the packages in `ros2_ws/src/`. Other OS/ROS combinations require separate validation; see [installation coverage](reproducibility-2026-11-13.md).

Obtain the complete runtime repository. In a new Bash terminal, change to its root and run:

```bash
cd /path/to/soarm-station-runtime
export ROS_DISTRO=lyrical
scripts/bootstrap
.venv/bin/python scripts/init-local --name localhost
scripts/init-security
```

The bootstrap installs pinned Python dependencies and builds the ROS workspace. It creates `.venv` with the ROS distribution's Python ABI and `.runtime-venv` with Python 3.12 for LeRobot/PyTorch. It does not install ROS or OS build packages.

`init-local` generates an operator token, collector token and HTTPS certificate/key under ignored `local/`. For remote dashboard access, supply the hostname clients will use, for example `--name robot.example.net`. The `--name` argument can be specified more than once during first initialization. Existing keys/certificates and tokens are preserved; rerunning it does not add names to an existing certificate.

`init-security` creates the authenticated DDS credentials for domain 71. The repository does not include credentials, calibration or physical-device profiles. Each deployment provisions its own files.

## Start the mock system

After installation, source the standard ROS environments and launch:

```bash
source /opt/ros/lyrical/setup.bash
source ros2_ws/install/setup.bash
ros2 launch soarm_station_system system.launch.py \
  config:="$PWD/config/mock.yaml" log_level:=debug respawn:=false
```

This launches one mock arm (`arm1`) and one mock camera (`forward`). Use `config/multi_mock.yaml` for multiple mock resources. Both examples use HTTPS port 8443 and the same `/soarm_station` namespace; run one complete graph at a time with these settings.

Open `https://localhost:8443/` with the certificate at `local/tls/cert.pem` trusted by the client. Default loopback/Tailscale access connects automatically; bearer-only clients require the configured `SOARM_STATION_TOKEN`. See [API access](web-api.md#connection-access-and-discovery). Successful startup produces process-start messages, fresh arm feedback and camera images. No motion permission or operation is created by startup.

The full launch selects both Python interpreters, loads the configured tokens for child processes and applies the DDS environment. It requires neither manual venv activation nor a systemd service.

Press **Ctrl-C** in the launch terminal and allow cleanup to finish. This stops the nodes and native worker and releases device ownership. Hardware shutdown retains existing support torque; it does not command torque off. Run the launch command again to restart.

## Configure and start physical hardware

Create an ignored host profile:

```bash
cp config/mock.yaml local/robot.yaml
```

Edit it for the target machine. Copying the example leaves its adapters in mock mode. A hardware profile must specify the arm backend, stable serial identity, calibration, named joints and limits; cameras need stable capture identities, formats and rates. Set measured transforms/model assets when needed. Each resource has an explicit unique ID. See the [runtime configuration reference](runtime-reference.md#configuration-and-resource-ids).

After configuring the profile, launch it with:

```bash
ros2 launch soarm_station_system system.launch.py \
  config:="$PWD/local/robot.yaml" log_level:=info respawn:=false
```

If an installed service or another process already owns these devices, stop it first. For the supplied service:

```bash
systemctl --user stop soarm-station.service
```

This command is unnecessary when no service is installed. Do not run the service and a foreground graph against the same device set.

Startup leaves motion permission disabled. Enable the intended arm and start an explicit operation through the dashboard or control API when ready to command it. Hold ends the operation and revokes its grant. Configuration is read at process startup: stop and relaunch after changing YAML. Setting the ROS `config` parameter does not hot-reload devices.

## Launch options and logs

```bash
ros2 launch soarm_station_system system.launch.py --show-args
```

| Argument | Behavior |
| --- | --- |
| `root` | Full repository root, normally inferred from the selected launch file. Set an absolute path for a relocated installation. |
| `config` | Explicit deployment YAML. Defaults to exported `SOARM_STATION_CONFIG`, otherwise `ROOT/config/mock.yaml`. |
| `log_level` | ROS node logging: `debug`, `info` (default), `warn`, `error` or `fatal`. |
| `respawn` | `false` by default. `true` restarts application nodes after three seconds. |
| `secrets_file` | Defaults to `ROOT/local/secrets.env`. Only configured operator/collector token names are read; exported nonempty values take precedence. |
| `security_keystore` | Defaults to exported `ROS_SECURITY_KEYSTORE`, otherwise `ROOT/local/security`. Use an absolute override path. |

With `respawn:=false`, a failed node remains stopped for inspection; other nodes can continue until Ctrl-C. ROS logs appear in the terminal and under `~/.ros/log/`; native-worker output also reaches the launching terminal. The logging argument controls ROS node logging, not every dependency's logger.

Token files accept `NAME=value`, optional quotes and optional `export`. Direct launch reads them as data without evaluating shell commands or substitutions. It checks environments and local credentials before launching the graph.

## Inspect the graph from another terminal

From the same repository in a second Bash terminal:

```bash
source scripts/setup.bash config/mock.yaml
ros2 node list --no-daemon --spin-time 5
ros2 topic list
ros2 action list
ros2 service list
ros2 param get /soarm_station/lerobot_node config
ros2 topic echo /soarm_station/arms/arm1/joint_states --once \
  --qos-reliability best_effort
ros2 topic echo /soarm_station/arms/arm1/lerobot/status --once
ros2 topic hz /soarm_station/arms/arm1/joint_states \
  --qos-reliability best_effort
ros2 topic info /soarm_station/camera/forward/image_raw/compressed --verbose
```

For hardware, select `local/robot.yaml` instead. Replace resource IDs in topic paths to match the profile. Ctrl-C ends continuous inspection commands.

The source-only helper starts no processes. It prepares the overlay, ROS venv, DDS credentials, domain and process timezone for CLI use. Environment changes inside a launch do not affect another terminal.

Secure discovery can take several seconds. An existing ROS CLI daemon may have a different environment; stop it with `ros2 daemon stop` in its original environment, then use the matching setup or a supported `--no-daemon` option. Generated DDS permissions use domain 71; changing `ROS_DOMAIN_ID` alone does not regenerate the policy. Discovery defaults to localhost.

## Run individual nodes

Stop the complete graph, then start the hardware node with mock adapters:

```bash
source scripts/setup.bash config/mock.yaml
ros2 run --prefix "$SOARM_STATION_ROOT/.venv/bin/python" \
  soarm_station_system lerobot_node --ros-args \
  -p config:="$SOARM_STATION_CONFIG" --log-level debug
```

Use the same setup in separate terminals for `workflow_manager`, `telemetry_node`, `static_transform_publishers` and, when configured, `recorder`. A standalone dashboard opens directly to loopback/Tailscale peers; no operator token is required for that access:

```bash
source scripts/setup.bash config/mock.yaml
ros2 run --prefix "$SOARM_STATION_ROOT/.venv/bin/python" \
  soarm_station_system dashboard_node --ros-args \
  -p config:="$SOARM_STATION_CONFIG" --log-level debug
```

The prefix selects the ROS venv even when ament console scripts have absolute system-Python shebangs. The complete launch applies it automatically. Run one hardware owner for each device set.

For IDE debugging, use `.venv/bin/python` for modules under `ros2_ws/src/soarm_station_system/soarm_station_system/` and `.runtime-venv/bin/python` for `python/soarm_station/worker.py` and native adapters. They are separate processes. A debugger attached to the ROS node does not automatically attach to its worker. Use mock devices for breakpoint stepping: pausing a real hardware owner can expire liveness checks.

## Rebuild and test

From the repository root:

```bash
source /opt/ros/lyrical/setup.bash
cd ros2_ws
colcon build --symlink-install --parallel-workers 2
cd ..
source ros2_ws/install/setup.bash
scripts/test
```

For application-package edits alone, `colcon build --symlink-install --packages-select soarm_station_system` is sufficient. Interface changes require rebuilding affected participants. Restart the graph to load source/configuration changes. Core Python uses editable installation and ROS Python uses symlink installation.

`scripts/test` runs checks in the ROS environment. Native-only checks require the separate LeRobot environment and appropriate test tooling; ROS modules cannot be imported with that environment's Python ABI.

## Optional systemd service

Stop the foreground graph before installing the user service:

```bash
scripts/install-service "$PWD/local/robot.yaml"
systemctl --user status soarm-station.service
journalctl --user -u soarm-station.service -f
```

The installer writes `~/.config/systemd/user/soarm-station.service` using the current checkout and supplied config paths, reloads systemd, and enables/starts the unit. Always pass an explicit absolute profile path. The unit starts the same ROS graph through `scripts/run`, with application-node respawn enabled and service restart on failure.

Use `systemctl --user stop soarm-station.service` before foreground debugging and `systemctl --user start soarm-station.service` to return to service operation. After source/config changes, use `systemctl --user restart soarm-station.service`. If the checkout or profile path changes, rerun the installer and restart the unit so the new paths take effect.

User services run in the account's user manager. Running after logout or before an interactive login requires user lingering configured by the host administrator. Foreground ROS launch does not require lingering.

To disable automatic startup, run `systemctl --user disable --now soarm-station.service`. To remove the generated unit afterward, delete `~/.config/systemd/user/soarm-station.service` and run `systemctl --user daemon-reload`. This does not remove the repository, configuration or runtime data.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Package not found | Build the workspace and source its `install/setup.bash` after the matching ROS setup. |
| Repository root not found | Keep the full checkout or pass `root:=/absolute/checkout`. A copied colcon install tree alone is incomplete. |
| Missing Python environment | Run bootstrap. Keep the native venv's interpreter path; resolving it to base Python bypasses the venv. |
| Missing token, TLS or DDS credentials | Run local initialization or supply the configured files/environment. Ignored credentials are not included in a clone. |
| ROS module/type import failure | Match the ROS distribution's Python ABI and rebuild/source interfaces. |
| Empty graph or DDS authentication failure | Match credentials/domain, allow discovery time, check the host clock and an existing CLI daemon. |
| Port or device already owned | Stop the previous graph/service and check configured ports, sockets and device owners. |
| Console-script `StopIteration` after an old installation | Check for obsolete `python/soarm_system.egg-info` generated by the former core package name. Archive it outside `python/`, then reinstall/rebuild. The current core distribution is `soarm-station-runtime`. |
| Camera below requested FPS | Measure capture timing and inspect USB bandwidth, exposure and lighting; requested FPS is not measured FPS. |
| Healthy feedback but no motion | Check the selected backend, writable profile, arm permission and explicit operation state. |

See [foreground validation](foreground-validation-2026-11-13.md) for measured results and [the reference Pi deployment](deployments/pi.md) for an installation-specific example.
