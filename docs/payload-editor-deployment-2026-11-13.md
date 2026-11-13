# Payload editor deployment — November 13, 2026

The reviewed Blocks/JSON editor `ab05da4` was cherry-picked as `1079105` after allowlist commit `9399af7`. The reviewed static permission-handling correction `802c3b7` was cherry-picked as `6921a24`; the earlier owner-label update was already deployed and was not applied again. Final served code revision: `6921a242204bfb1f7929c325c145bc42051bac4e`.

The editor uses one shared draft across Blocks and JSON, editable/reorderable typed steps, all six operation/delivery combinations and configured Cartesian endpoints. Validate reuses semantic submission checks without I/O. Visual/browser QA belongs to the frontend review; integration did not redesign the approved UI.

## Tests

All47 frontend tests passed on the Pi across transport, graph, camera layout and payload model. The new rejected-stale-enable regression is included: a definitively rejected canceled enable does not issue a compensating Hold against another controller; accepted late grants retain their existing cleanup. No actual permission/operation was requested in deployment verification.

The asset-route tests for the allowlist addition passed before activation. Existing backend/native checks remain documented in [backend validation](dashboard-backend-validation-2026-11-13.md).

## Dashboard-only activation

A fresh check confirmed permissionOFF, no active operation and goal-write count0. Only the dashboard child, identified by full executable path in the service cgroup, received SIGTERM. The launch respawned that child. The service itself, physical ROS owner and native worker were not restarted.

| Process | Before PID | After PID |
| --- | --- | --- |
| dashboard_node | 48110 | 49076 |
| lerobot_node | 48107 | 48107 |
| native_worker | 48198 | 48198 |

Service mainPID remained48080. Native boot stayed `52678000872a44639d42b4d562258aa7`; arm controller session stayed `7dc1e0883f9b4199892104c153c4d84e`. Calibration SHA-256 stayed `c816f1bbaa8122bb40475699f5a74724fbe8cce65634f02d14b5e8e0e4265814`. Existing motor goal registers were unchanged. Final feedback was approximately100Hz, all six torquesON, permissionOFF, no active operation and goalwrites0. Camera capture processes continued under the same native worker.

## Served assets and read-only smoke

All six public assets returned200 with the correct MIME and byte-for-byte agreement with the checked-in content. The final transport hash below includes the static client correction; it needed no additional process reload. No operator token was embedded/exported.

| Asset | Bytes | MIME | SHA-256 |
| --- | --- | --- | --- |
| app.js | 54266 | text/javascript | `fd81505d722ff8864d5eecebfada17485ecc63a96ff76b8f1c462618a548a9cb` |
| graph.js | 29883 | text/javascript | `02a389993bb526aac7534deb8d4730ef440e577dc4efadb41fa64b6520914ade` |
| transport.js | 46613 | text/javascript | `8c61153f16526224709acef4852aaa0177430288e19711abf47d879010a27760` |
| styles.css | 32641 | text/css | `4e74e27dd331a40611309e8d0075f98146f4a7f63a97ef26b28018be702868a5` |
| camera-layout.js | 9590 | text/javascript | `036e439cb193eaf18af9d5e0fdb13bb15ae31a23445d238641c2fd024891283d` |
| payload-model.js | 13740 | text/javascript | `44cef3f58ba83a23a3494b83127a613be21b684c253f968ca923878390bc5062` |

The page contains the Blocks/JSON controls. Authenticated status/resources/graph endpoints, all three JPEG endpoints and the public-subprotocol camera WebSocket passed. Anonymous APIs/media rejected401. Six per-motor rows remained available with torqueON and current_a null; current is not inferred. Fresh camera captures continued at640x480 without capture errors in the observed snapshot.

Graph coverage still reports unresolved RMW endpoint owners honestly; metadata does not prove liveness or delivery. This deployment does not newly qualify physical execution of all UI modes, learned task success, cameraFPS, large-history latency or continuous Cartesian/collision guarantees. No motion, calibration/PID change or GitHub push occurred.

Evidence: ignored `local/payload-editor-reload-before.json`, `local/payload-editor-reload-after.json` and `local/payload-editor-js-tests.log`. See [UI usage](dashboard-ui.md) and [backend API](dashboard-api.md). Refresh the browser to load the final static modules.
