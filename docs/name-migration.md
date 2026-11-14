# SO-Arm Station naming migration

The project is named **SO-Arm Station**. The runtime and training repositories are `soarm-station-runtime` and `soarm-station-training`. SO-101 and other hardware model names remain adapter identities.

The display spelling was standardized to **SO-Arm** on November 13, 2026.
Repository, package, command, environment-variable and ROS identifiers retain
the spellings in the table below; this display-name correction requires no
interface migration.

## Public names

| Component | Previous name | Current name |
| --- | --- | --- |
| Runtime Python distribution | `soarm-core` | `soarm-station-runtime` |
| Runtime Python module | `soarm` | `soarm_station` |
| ROS application package | `soarm_system` | `soarm_station_system` |
| ROS interface package | `soarm_interfaces` | `soarm_station_interfaces` |
| ROS namespace | `/soarm` | `/soarm_station` |
| Training distribution/CLI | `soarm-training` | `soarm-station-training` |
| Training module | `soarm_training` | `soarm_station_training` |
| Operator CLI | `soarm-operator` | `soarm-station-operator` |
| Collector CLI | `soarm-collector` | `soarm-station-collector` |
| Release CLI | `soarm-release` | `soarm-station-release` |
| Worker CLI | `soarm-worker` | `soarm-station-worker` |
| User service | `soarm.service` | `soarm-station.service` |
| WebSocket subprotocol | `soarm` | `soarm_station` |
| Environment variables | `SOARM_*` | `SOARM_STATION_*` |

The normal foreground command is:

```bash
source /opt/ros/lyrical/setup.bash
source ros2_ws/install/setup.bash
ros2 launch soarm_station_system system.launch.py \
  config:="$PWD/config/mock.yaml" log_level:=debug respawn:=false
```

See [startup and debugging](startup-and-debug.md) for installation and individual-node commands.

## Upgrade an existing installation

These are interface/package renames; update ROS clients and custom scripts together. Rebuild all ROS participants against `soarm_station_interfaces`, update topic/action/service names to `/soarm_station`, and reload browser/operator clients. HTTP API routes and token values are unchanged. Old Python imports, CLI names, ROS names and the old WebSocket subprotocol are not aliases.

1. Stop and disable an installed `soarm.service` before updating source. Stop training or collection processes using the affected packages/devices too.
2. Save the existing deployment profiles, credential file and service unit. Rename token variable keys to `SOARM_STATION_TOKEN` and `SOARM_STATION_COLLECTOR_TOKEN`, preserving their values; update configured `token_env` names to match. Update other custom `SOARM_*` exports.
3. Remove the old editable distributions from their environments, then reinstall the current source. The runtime distribution is installed in both ROS/native environments, and training has its own environment. `scripts/bootstrap` is the fresh-install entry point.
4. Move obsolete `ros2_ws/build`, `ros2_ws/install` and `ros2_ws/log` outputs outside the workspace before a clean build. Archive stale generated `python/soarm_core.egg-info` or `python/soarm_system.egg-info` metadata if present. Source ROS and rebuild with `colcon build --symlink-install`.
5. Update local socket/frame/lock paths consistently. The examples use `/tmp/soarm-station-*`; runtime and training share `/tmp/soarm-station-device-locks`. An explicit existing database path can remain in use. Do not rename or edit dataset/checkpoint contents merely to change the application name.
6. Run the checks and a foreground mock launch. Install the replacement service with `scripts/install-service "$PWD/local/robot.yaml"` when ready. Archive/remove the disabled old unit and reload the user systemd manager so it cannot be started accidentally alongside the new owner.

Credentials, calibration, device identities, joint limits and stored telemetry are deployment data, separate from branding. Existing policy bundles keep their explicit metadata; new examples use `soarm_station_multi` as the generic robot-type label. Validate an existing bundle's mappings rather than rewriting trained assets.

Dated validation reports retain the names and commands used by their tested source revisions. Use the current README/startup guide for current commands.

## Reference deployment

The reference Pi retains its deployment directories `/home/pipi/so-101/ros_system` and `/home/pipi/so-101/training`. Directory names describe the installed host layout; repository/package names follow the table above. The active user service is `soarm-station.service`. See [deployment details](deployments/pi.md).
