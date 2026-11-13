# Startup and foreground debugging

The systemd service is optional. After the repository has been bootstrapped, start the complete application with ordinary `ros2 launch`; the service and `scripts/run` are convenience entry points for the same graph. No service installation, lingering setting, or systemd unit is needed for development.

Commands below use **Bash on the Linux robot host**. On this Pi, SSH to `pipi@ubuntu-pi.tail521c60.ts.net`, enter `bash`, then `cd ~/so-101/ros_system`. The Pi's default login shell is Zsh, so explicitly use Bash for the `.bash` setup files. On another host, substitute its checkout path and ROS distribution.

## 1. First-time installation

Skip this section on an already bootstrapped checkout, including the deployed Pi.

The verified platform is Ubuntu 26.04.1 aarch64, ROS 2 Lyrical, system Python 3.14.4 and native Python 3.12.16. First install a matching ROS distribution, colcon/build tools, Python venv support and distribution ROS/Python dependencies. `scripts/bootstrap` installs the repository's pinned Python environments and builds both ROS packages; it does not install an operating system or ROS distribution. See [installation evidence and portability limits](reproducibility-2026-11-13.md).

Obtain the **whole runtime repository** (a local clone/archive is sufficient; publishing to GitHub is unnecessary), then:

```bash
cd /absolute/path/to/ros_system
export ROS_DISTRO=lyrical             # change for your installed distribution
scripts/bootstrap
.venv/bin/python scripts/init-local --name YOUR_ROBOT_DNS_NAME
scripts/init-security
```

`init-local` generates an operator token, collector token, and HTTPS certificate/key under ignored `local/`. Its certificate includes localhost and the supplied DNS name. Existing credentials are preserved. `init-security` provisions the DDS credentials used by this deployment on domain 71. Credentials/calibration are host-local and must be initialized/configured by the new user; they are deliberately absent from Git.

Two Python environments are intentional: `.venv` matches the ROS Python ABI; `.runtime-venv` contains the pinned LeRobot/PyTorch stack. Launch chooses the correct interpreter for each process. Do not replace the ROS interpreter with Python 3.12 or source the native environment before launching ROS.

## 2. Choose the profile and avoid a duplicate runtime

| Profile | Purpose |
| --- | --- |
| `config/mock.yaml` | Tracked one-arm/one-camera simulation; no hardware opened. Default direct-launch profile. |
| `config/multi_mock.yaml` | Tracked multiple-resource development example. |
| `local/pi.yaml` | This Pi's private native read-only profile. |
| `local/pi-writable.yaml` | This Pi's private native control profile; permission starts OFF. |
| `local/YOUR_HOST.yaml` | Your own deployment, with explicit arm/camera IDs, stable paths, calibration and limits. |

`local/pi*.yaml` are not shipped to another user. Copy a tracked example into `local/` and configure the hardware connected to that host. Relative configuration paths resolve against the YAML file's directory. See [architecture](architecture.md) for the resource model and [physical qualification](physical-motion-validation-2026-11-12.md) for the Pi profile's measured limits.

If a service is already installed/running, stop it **before** starting the foreground graph:

```bash
systemctl --user status soarm.service
systemctl --user stop soarm.service
```

Skip these commands when no unit is installed. Starting mock and production together also duplicates the fixed `/soarm` node/action names and normally conflicts on HTTPS port 8443. Use one complete graph for these instructions. Stopping a service manually leaves it stopped until explicitly started again; for a development-only machine with an existing unit, `systemctl --user disable --now soarm.service` also disables automatic startup.

## 3. Normal ROS foreground startup

From the runtime checkout, in Bash:

```bash
source /opt/ros/lyrical/setup.bash
source ros2_ws/install/setup.bash
ros2 launch soarm_system system.launch.py \
  config:="$PWD/config/mock.yaml" \
  log_level:=debug respawn:=false
```

For the deployed physical Pi, change only the config argument:

```bash
ros2 launch soarm_system system.launch.py \
  config:="$PWD/local/pi-writable.yaml" \
  log_level:=info respawn:=false
```

There is no call to `scripts/run` or systemd here. You do not need to activate either venv for the complete launch. The launch file finds its checkout, selects the ROS venv for node scripts, passes the native interpreter to the hardware owner, loads the configured tokens for child processes, and applies the authenticated DDS environment. It checks credentials/environments before starting the graph. It never creates keys or enables control permission at startup.

The graph includes `lerobot_node` (which owns the native worker), `workflow_manager`, `telemetry_node`, `dashboard_node`, `static_transform_publishers`, one `robot_state_publisher` per configured URDF, and `recorder` when `recording.enabled` is true. Training, an external collector and a remote policy server are separate programs; they are not started by this launch.

Expected output includes process-start messages. The dashboard should report the configured resources, fresh joint feedback and camera images. A physical arm can already have supporting torque ON while control permission is OFF. No goals are issued by startup.

Press **Ctrl-C once** in the launching terminal and allow cleanup to finish. This stops the graph/native worker and releases device ownership. Shutdown retains the arm's existing support torque; it is not an emergency power-off or an instruction to disable torque. Run the same command again to restart. Configuration is read at process startup: stop/relaunch after YAML edits; changing the ROS `config` parameter does not hot-reload devices.

### Launch arguments

```bash
ros2 launch soarm_system system.launch.py --show-args
```

| Argument | Default / behavior |
| --- | --- |
| `root` | Inferred from this launch file's checkout. Pass an absolute checkout path for a relocated install. |
| `config` | Exported `SOARM_CONFIG`, otherwise `ROOT/config/mock.yaml`. Explicit `config:=...` takes precedence. |
| `log_level` | `info`; accepts `debug`, `info`, `warn`, `error`, `fatal` for launched ROS nodes. |
| `respawn` | `false`; a crashed application node stays down for inspection. Other nodes can remain running until Ctrl-C. `true` restarts application nodes after 3 seconds. |
| `secrets_file` | `ROOT/local/secrets.env`; optional custom path. Only configured operator/collector token names are read. Exported nonempty token values take precedence. |
| `security_keystore` | Exported `ROS_SECURITY_KEYSTORE`, otherwise `ROOT/local/security`. Use an absolute path for an override. |

Example for an explicitly relocated checkout:

```bash
ros2 launch soarm_system system.launch.py \
  root:=/opt/soarm config:=/opt/soarm/local/robot.yaml \
  log_level:=debug respawn:=false
```

Token files accept `NAME=value` and optional shell-style quotes/`export`; direct launch treats them as data and does not evaluate shell substitutions. ROS node logging appears in the foreground and under `~/.ros/log/`. Native-worker messages go to the launching terminal too. `log_level:=debug` controls ROS logging, not every third-party library's logging configuration.

## 4. Inspect it from a second terminal

Environment changes inside a launch apply to its children, not another terminal. Use this source-only helper to match the graph's ROS overlay, ROS venv, DDS credentials, UTC timezone and domain 71:

```bash
bash
cd ~/so-101/ros_system
source scripts/setup.bash local/pi-writable.yaml  # config/mock.yaml for mock
ros2 node list --no-daemon --spin-time 5
ros2 topic list
ros2 action list
ros2 service list
ros2 param get /soarm/lerobot_node config
ros2 topic echo /soarm/arms/arm1/joint_states --once \
  --qos-reliability best_effort
ros2 topic echo /soarm/arms/arm1/lerobot/status --once
ros2 topic hz /soarm/arms/arm1/joint_states \
  --qos-reliability best_effort
ros2 topic info /soarm/camera/forward/image_raw/compressed --verbose
```

Replace `arm1` and `forward` with configured IDs. Ctrl-C exits continuous inspection commands. `source scripts/setup.bash ...` starts no nodes and grants no motion permission. Its optional profile argument defaults to `config/mock.yaml`. It is convenient for inspection and individual-node work; complete launch does not depend on this helper.

If a pre-existing ROS CLI daemon was started with another domain/credential environment, stop it in its original terminal with `ros2 daemon stop` and restart it in the matching environment, or use the command's `--no-daemon` option where supported. Keep domain 71 with the generated DDS policy; changing only `ROS_DOMAIN_ID` does not regenerate permissions. Discovery defaults to localhost; remote operators normally use the authenticated HTTPS/WebSocket interface.

## 5. Run/debug individual nodes

Stop the complete graph first. In a prepared Bash terminal:

```bash
cd ~/so-101/ros_system
source scripts/setup.bash config/mock.yaml
ros2 run --prefix "$SOARM_ROOT/.venv/bin/python" \
  soarm_system lerobot_node --ros-args \
  -p config:="$SOARM_CONFIG" --log-level debug
```

Use the same environment in other terminals for `workflow_manager`, `telemetry_node`, `static_transform_publishers` or `recorder`. For a standalone dashboard, also export its token variables before starting it:

```bash
source scripts/setup.bash config/mock.yaml
set -a
source "$SOARM_ROOT/local/secrets.env"
set +a
ros2 run --prefix "$SOARM_ROOT/.venv/bin/python" \
  soarm_system dashboard_node --ros-args \
  -p config:="$SOARM_CONFIG" --log-level debug
```

The `--prefix` is necessary for individual `ros2 run` here: ament-installed console scripts can have an absolute system-Python shebang, so activating a venv alone does not select the pinned web/image dependencies. The complete launch applies this prefix automatically. Only one `lerobot_node`/native worker should own a device set.

For an IDE, select `.venv/bin/python` when debugging ROS node modules in `ros2_ws/src/soarm_system/soarm_system/`; select `.runtime-venv/bin/python` for `python/soarm/worker.py` and native adapters. They are separate processes, so a debugger attached to a ROS node does not automatically step into native-worker code. Pausing the hardware owner can make liveness expire; use mock for breakpoint-driven stepping. No systemd installation is needed for either process.

## 6. Dashboard and normal operation

Open `https://YOUR_ROBOT_DNS_NAME:8443/`; on the deployed Pi use `https://ubuntu-pi.tail521c60.ts.net:8443/`. Trust that host's certificate from `local/tls/cert.pem` and enter the operator token from its private `local/secrets.env`. The login page is public; status/media/control endpoints authenticate.

Choose the explicit arm, check measured state, then use the existing Enable / manual-operation controls when you intend to move it. Startup, refresh, reconnect and relaunch do not enable motion. Hold ends the operation and revokes its grant. This guide's startup verification does not need any enable or motion command.

Model operations also require a configured named policy profile and compatible model bundle. The deployed Pi currently has no production policy profile; a successful system launch is not evidence of a trained manipulation policy.

## 7. Rebuild and run checks after changes

Core Python and launch sources are used through editable/symlink installs. Rebuild after modifying ROS packaging or interfaces; all participants need the rebuilt interface definitions:

```bash
source /opt/ros/lyrical/setup.bash
cd ros2_ws
colcon build --symlink-install --parallel-workers 2
cd ..
source ros2_ws/install/setup.bash
scripts/test
```

For launch/package-only edits, `colcon build --symlink-install --packages-select soarm_system` is sufficient. Restart the running graph to load source changes. Native-only tests, when needed, run in `.runtime-venv`; ROS-only tests cannot run in that interpreter because of the ABI boundary.

## 8. Return to the optional service

After Ctrl-C has stopped the foreground graph:

```bash
systemctl --user start soarm.service       # existing installed unit
systemctl --user status soarm.service
journalctl --user -u soarm.service -f
```

To install on a new host only when desired:

```bash
scripts/install-service "$PWD/local/YOUR_HOST.yaml"
```

This writes a user unit with this checkout's paths and enables/starts it. `scripts/run` now explicitly requests `respawn:=true`; its existing operational behavior is retained. A direct `ros2 launch` defaults to `respawn:=false` for debugging. User lingering, when configured by that host's administrator, lets a user service run without an interactive login; it is irrelevant to foreground startup.

## 9. Troubleshooting

| Symptom | Check / correction |
| --- | --- |
| `Package 'soarm_system' not found` | Source the matching `/opt/ros/.../setup.bash`, build, then source this checkout's `ros2_ws/install/setup.bash`. |
| Root cannot be found | Keep the whole checkout, or pass `root:=/absolute/checkout`. Copying only the colcon install tree is not a standalone deployment. |
| Missing Python environment | Run `scripts/bootstrap`; keep the native venv interpreter path, rather than replacing it with its resolved base-Python symlink target. |
| Missing token/TLS/DDS credentials | Run the one-time initialization, or supply the configured paths/environment variables. Do not expect ignored `local/` files in a clone. |
| ROS types/modules fail to import | Match the ROS distribution's system Python ABI; rebuild/source interfaces. Do not run ROS modules with the native Python 3.12 environment. |
| Empty CLI graph / DDS auth failure | Use the second-terminal helper, matching credentials/domain/UTC, and check for an old CLI daemon. The host's clock must be correct. |
| Address/device already in use | Stop the service/previous foreground graph. Check this checkout's configured HTTPS port, socket and physical-device owner. |
| Console-script `StopIteration` after upgrading old source | Inspect obsolete `python/soarm_system.egg-info` from the former core package name. Archive that generated directory outside `python/`, reinstall with bootstrap and rebuild. The current core distribution is `soarm-core`; the ROS package is `soarm_system`. |
| Camera below requested FPS | Inspect actual capture timing, USB bandwidth and lighting/exposure. The Pi's cameras currently deliver about 14–18 FPS; requesting 30 does not establish 30 measured FPS. |
| Status is healthy but no motion | Startup permission is OFF. Select the correct arm/profile and explicitly start an operation. The read-only profile cannot dispatch goals. |

## Verification record — November 13, 2026

Verified on the deployed Pi, separately from earlier physical-motion qualification:

- Rebuilt `soarm_system`; sourced ROS suite: **62 passed, 8 skipped**, plus four passing subtests. Seven launch-environment checks cover root discovery, token parsing/precedence, missing prerequisites and preservation of native-venv interpreter symlinks. A ROS serialization regression covers initial integer-valued native samples.
- Direct launch from a shell with wrapper/private environment variables unset: `--show-args`, mock profile with `log_level:=debug`, and native writable profile with `respawn:=false` all passed after corrections. Full launch used only standard ROS/colcon setup and `ros2 launch`.
- Mock authenticated status and valid camera JPEG passed; permission stayed OFF. Short ROS topic-rate inspection measured approximately 95–96 Hz under debug logging; this is a diagnostic sample, not a performance qualification.
- Native writable foreground run: fresh feedback **99.999 Hz**, all three authenticated camera JPEGs valid, permission OFF, measured torque ON, native health **writes=0**, voltage 12.0 V, maximum temperature 34 C.
- Second-terminal node discovery (five-second discovery window), joint/status echo, action listing, parameter lookup and verbose camera-topic inspection passed. Default short secure discovery can initially return only a subset of nodes.
- Individual `ros2 run --prefix ...` hardware-node and dashboard commands passed together with valid mock feedback; the seven launch-environment tests also passed in native Python 3.12.
- Ctrl-C stopped both full graphs cleanly with no remaining worker/node process or listener on port 8443. No motion/enable command was issued.

Testing found and corrected the ROS launch environment-action import, core/ROS Python path precedence, native-venv symlink handling, and integer-to-float conversion at initial ROS status publication. Obsolete generated metadata from the former `soarm-system` core package was archived under ignored `local/debug-startup-stage/`; current core source/package naming remains `soarm-core`.

This is not a new clean-OS or cross-platform installation qualification. Hardware profiles, calibration, physical limits and motion-permission rules were not changed.

The existing user service was restored after testing. Its native writable profile again reported valid approximately100 Hz feedback, all three camera feeds, permission OFF, torque ON and zero startup goal writes. No remote or GitHub push was performed.
