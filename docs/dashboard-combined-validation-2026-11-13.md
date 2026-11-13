# Combined dashboard deployment — November 13, 2026

Integrated backend `10b861a`, root-approved frontend `7c49f5c` cherry-picked as `a93dc2e`, and root-approved unresolved-owner labeling `8432b60` cherry-picked as `0191bca`. The latter changes only static graph presentation and required no second restart. Served code revision: `0191bcaeae326d52b1f545a55d13bd3a4ad4409b`.

The coordinating chat approved the visual design/browser QA and owns the layout. The backend integration did not redesign or hand-edit its visual files.

## Verification

- Backend ROS102 tests passed with8 native-only skips; focused native46 tests passed; Python application package build passed.
- All33 frontend JavaScript tests passed on the Pi before restart. The6 graph tests passed again after the static labeling follow-up.
- One manual restart was performed only after a fresh check confirmed permissionOFF and no active operation. Support torque was preserved. New native boot: `52678000872a44639d42b4d562258aa7`; service mainPID48080, running, zero automatic systemd restarts.
- Native feedback measured approximately100Hz. Motion permission is OFF, no operation is active, all six measured torque states are ON, and native goal-write count is0. Existing motor goal registers matched their pre-restart values. No move, enable, hold, operation, calibration or PID request was issued by the smoke test.
- Calibration SHA-256 stayed `c816f1bbaa8122bb40475699f5a74724fbe8cce65634f02d14b5e8e0e4265814`.

## Native motor snapshot

| ID | Motor | Temperature C | Voltage V | Torque | Current A |
| --- | --- | --- | --- | --- | --- |
| 1 | shoulder_pan | 31.0 | 12.1 | ON | null |
| 2 | shoulder_lift | 31.0 | 12.0 | ON | null |
| 3 | elbow_flex | 32.0 | 12.0 | ON | null |
| 4 | wrist_flex | 31.0 | 12.1 | ON | null |
| 5 | wrist_roll | 34.0 | 12.0 | ON | null |
| 6 | gripper | 32.0 | 12.0 | ON | null |

All rows were reported as measured with valid position/goal data. Body and gripper timestamps retain separate provenance. Current is unavailable on the existing adapter and remains null; it is not inferred from load. Admitted references are null at idle startup rather than fabricated from measured pose. Per-motor health uses the same existing1Hz batched reads.

## HTTP, cameras and graph

Public index and all five approved static assets returned200 with correct content types and byte-for-byte equality to the checked-in files: app.js, graph.js, transport.js, styles.css, camera-layout.js. API/status/resources/graph and camera JPEGs rejected anonymous requests. Unlisted asset names rejected; resource descriptors omitted private paths and secrets. The operator token was not exported or embedded in assets.

Authenticated JPEG access passed for forward/overview/wrist, with fresh running capture sequences. The camera WebSocket selected the public `soarm_station` subprotocol and delivered a valid JPEG. Nominal FPS settings do not establish measured capture-rate performance.

The graph contained35 topics,52 services,2 actions and155 relationship edges in the captured snapshot, including the operation server and both declared clients.12 unresolved endpoint-owner placeholders were retained honestly; topic coverage is partial, not proof of liveness/delivery. The static UI counts identified nodes and unresolved owners separately.

One explicit one-second joint-state observation counted89 messages /15308 bytes. This is local best-effort subscriber observation, not an end-to-end delivery or physical encoder-rate benchmark. Sampling ended and its subscription cleaned up; no payload was exported. No wholesale/high-bandwidth subscriptions were created.

## Scope and artifacts

This deploys the approved UI and read-only backend observations. It does not newly qualify physical execution through every UI mode, model task success, camera FPS, large retained-history query latency or collision/Cartesian-path guarantees. Earlier native motion qualification and policy limits remain separate evidence.

Detailed machine evidence is ignored `local/dashboard-combined-before.json` and `local/dashboard-combined-after.json`; backend contract is [dashboard-api.md](dashboard-api.md), UI design/usage [dashboard-ui.md](dashboard-ui.md). No Git remote was added and no GitHub push occurred. Browser reload obtains the new static assets.
