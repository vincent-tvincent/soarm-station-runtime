# Dashboard backend validation — November 13, 2026

Backend changes are based on `0f81375`. They add public resource descriptors, cached per-motor telemetry, generic ROS discovery/traffic counters, explicit static-asset routes and the existing Cartesian INLINE JSON conversion. The coordinating frontend work owns all visual files. No `web/*` content is changed by this backend commit.

## Tests and build

- ROS Python 3.14: **102 passed, 8 native-only skipped**, plus four passing subtests.
- Focused native Python 3.12 health, graph, actuator-dispatch and policy-semantics checks: **46 passed**. Pytest tooling was loaded from the training environment; target native imports remained in the runtime environment.
- `colcon build --symlink-install --packages-select soarm_station_system` passed. No ROS IDL or dependency changes are required.
- Native health tests assert exactly the existing four batched reads. Read-only protocol health tests assert the existing three reads per motor, with no goal/current/register additions.
- Resource privacy, unavailable/zero distinction, independent gripper timestamps, malformed-record cache retention, subscription cleanup failures, graph-loop recovery, unknown RMW owners, route authentication/asset traversal and camera protocol negotiation are covered.
- Cartesian POSITION_ONLY/STRICT_POSE INLINE conversion round trips pass; mixed/STREAM/malformed cases reject. No kinematics algorithm, permission, dispatch limit, controller timing or actuation gate is changed.

## Isolated mock endpoints

A separate authenticated DDS keystore/domain72, mock-only two-arm/three-camera profile, separate IPC/data and HTTPS port18453 were used. Production domain71 and its service were left running. All12 simulated motor rows arrived with configured IDs/units and nullable current. Resource metadata omitted private paths/secrets. Authenticated graph/resources/status requests and browser camera protocol passed; anonymous APIs rejected. Explicit joint-state sampling counted 71 messages without exporting payloads; raw image sampling was blocked. All mock motion permissions stayed OFF; no operation was requested.

The initial mock process loaded the discovery code before the unresolved-owner fix and failed graph refresh. The corrected isolated process was restarted and the endpoint checks passed. An earlier live probe also exposed the real RMW unknown-owner condition; both fixes are regression-tested, not counted as initially successful runs.

## Live read-only discovery evidence

A separate graph observer inspected the running domain71 graph. Its metadata contained 35 topics, 53 services, 2 actions and 158 relationship edges, including the existing operation server and both declared clients. 14 endpoint owner placeholders remained explicit because the RMW did not supply ROS node identities; topic coverage is partial rather than fabricated. Placeholder records do not establish a physical node/process count.

A one-second joint-state-only observation counted 70 messages / 12040 bytes. These are local best-effort observation counts, not an end-to-end delivery or physical sampling-rate qualification. No message payload was decoded/exported.

The native worker boot ID remained `a7326e44d57443cda46be5ffeee29c68`, permission OFF and goal-write count0 before/after. No motor connection or actuation call was opened/issued by the probe. Production remained active with main PID44099 and zero systemd restarts. Actual newly added per-motor native telemetry is pending the coordinated integration/restart; fake-bus/native-ABI tests and mock ROS transport do not claim that live feature was already deployed.

## Integration handoff

The backend commit can be followed by the separately reviewed frontend Git patch/commit. Use the API contract in [dashboard-api.md](dashboard-api.md). The explicit public assets are app.js, graph.js, transport.js, styles.css and camera-layout.js. DIRECT `/ws/control` retains its existing operation scope; other supported operations use the existing typed HTTP/action/stream interfaces.

No production restart is performed by this backend task. After frontend integration, the coordinator can run combined checks and its authorized read-only startup smoke. The isolated mock profile is under the directory recorded in ignored `local/dashboard-backend-test-path`, with its own security/ directory, domain72 and HTTPS18453. The isolated test process is stopped after verification. Evidence: ignored `local/dashboard-backend-mock-report.json`, `local/dashboard-backend-live-graph.json`, `local/dashboard-backend-live-graph-snapshot.json`. No GitHub push or calibration change occurred.
