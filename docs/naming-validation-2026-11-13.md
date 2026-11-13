# SOArm Station naming validation — November 13, 2026

The coordinated rename updates the runtime/training distributions, Python modules, ROS packages/types/namespace, shell environment variables, CLIs, WebSocket subprotocol, dashboard branding and user service. See [name migration](name-migration.md) for the public mapping and [startup](startup-and-debug.md) for current developer commands. SO-101 hardware model identities and deployment directory roles are unchanged.

## Software verification

- Clean colcon build: `soarm_station_interfaces` and `soarm_station_system`, completed in 1 minute 16 seconds.
- Sourced ROS Python 3.14 suite: **62 passed, 8 native-only skipped**, plus four passing subtests.
- Native Python 3.12 suite: **65 passed, 5 ROS-only skipped**, plus four passing subtests. Shared tests overlap. Pytest tooling was loaded from the training environment; native imports, including CPU Torch 2.11.0, resolved inside the runtime native environment.
- Training CLI `soarm-station-training --help` and both training tests passed. New runtime CLI entry points and package metadata resolved; legacy imports/console-script shims were absent from the active environments.
- Documentation links/anchors and Bash examples checked.

## Foreground mock integration

A direct `ros2 launch soarm_station_system system.launch.py` from a shell with old/new wrapper variables unset passed with two mock arms and three mock cameras. The authenticated control, anonymous rejection, camera JPEG, browser token subprotocol `soarm_station`, disconnect hold, independent-arm state, INLINE completion and durable history checks passed. The run sent 268 mock commands. ROS node discovery, status echo and action listing used `/soarm_station`; the served dashboard title/header were SOArm Station. Ctrl-C stopped the mock owner before native startup.

The first run against the existing populated mock database timed out on the five-second history-service request. The repeat used an isolated fresh mock database and passed; the timeout's cause was not established or changed by this rename. Large retained-history page latency is not qualified by the fresh fixture. Existing mock data was preserved.

## Native service and retained state

`soarm-station.service` replaced the disabled legacy unit and is enabled/running with zero systemd restarts. It uses the existing writable Pi profile. Final measured state: approximately 100.000 Hz feedback, all three authenticated camera JPEGs valid, permission OFF, torque ON, no active operation, zero goal writes, 12.0 V and maximum temperature 34 C. No physical motion command was issued for the rename.

Credential values were compared before/after and preserved; variable names changed. Arm/camera profile arrays were exactly equal to their backups. Calibration SHA-256 remained `c816f1bbaa8122bb40475699f5a74724fbe8cce65634f02d14b5e8e0e4265814`. Device/control source files were byte-identical to the baseline after accounting for their directory rename.

All 24663 pre-migration telemetry rows, including their sequence, payload and acknowledgement state, matched the saved prefix digest. SQLite quick check passed; new monitoring records append to the same database. No dataset, checkpoint or calibration content was renamed or overwritten.

Ignored evidence: `local/station-rename-integration.json`, `local/station-rename-native.json`, and the backup directory recorded in `local/station-rename-backup-path`. The backup contains the previous unit/profiles/credential file, a consistent telemetry snapshot and old generated ROS build outputs; it is not an importable overlay or tracked source. No remote was added and nothing was pushed.
