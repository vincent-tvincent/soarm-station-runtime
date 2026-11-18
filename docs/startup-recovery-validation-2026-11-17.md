# Startup recovery validation — November 17, 2026

This change addresses three observed startup gaps: an unsupervised input-host
process and temporary configuration location, telemetry crashing while its SSD
was absent, and camera capture that could retain the wrong decoded resolution
or block without independent recovery. It also makes component readiness
visible instead of equating HTTP connectivity with a fully ready station.

## Software verification

- Browser suite: 363 tests passed.
- Pi ROS environment with rebuilt interfaces: 737 tests and 160 subtests passed;
  8 tests were skipped. This is separate from physical motion validation.
- Isolated full ROS graph, domain 198 and loopback port 18443, used only mock
  resources and its own TLS/DDS credentials. Normal startup reached ready.
  Killing its camera child recovered capture without restarting its mock arm
  runtime or issuing motor writes.
- A second isolated launch pointed telemetry at a missing mount. Controller and
  camera remained ready; telemetry reported waiting. History returned HTTP503
  with `available: false`, and no fallback database/directory was created.
- Tests cover blocked camera open/read/release, parent death, bounded latest-frame
  metadata, wrong-size frame rejection, storage disappearance/recovery, UUID
  mismatch, obsolete upload acknowledgements, gap accounting and stale-message
  readiness. Interface participants must rebuild for the new QueryHistory fields.
- Synthetic UI checks at 1600×900 and 780×1000 confirmed readable component status,
  no horizontal overflow, and preserved control buttons and the fixed FK panel.

## Mac companion (historical)

The subsequent browser USB release retired this client installation. Current
operators pair from the dashboard. [Current leader setup](leader-input.md)

The configured Mac companion was installed as a per-user launch agent with a
persistent source snapshot and profile. Automatic process restart and subsequent
Station reconnection were observed, with state IDLE and no active operation.
Calibration bytes remained unchanged. At that check no matching leader USB
adapter was present in the Mac's OS inventory; this does not imply a lost profile.
Automatic discovery requires a logged-in Mac account. It never auto-starts following.

## Remaining physical/administrator checks

The mountpoint-protection helper was reviewed and tested with substituted mount
operations, but requires an administrator to execute against the deployment.
It protects the underlying system-disk directory while leaving the SSD mounted.
See `scripts/protect-telemetry-mount` and the storage section of runtime-reference.
The ordinary unavailable-mount check is active without it; protection against a
forced/lazy unmount racing SQLite path resolution needs that OS permission guard.

A PCIe device that was not enumerated cannot be restored by the database code.
Complete cold-start acceptance still requires a proper power-off/on cycle with
SSD/HAT attached beforehand. Service restart testing does not substitute for that
physical check. No joint limits, calibration, torque commands or motion permission
were changed by the recovery tests. No speculative DDS transport workaround was added.

## Live Pi activation

Deployment of `e23a642` rebuilt both interface/system packages and restarted an
idle Station. The only deployment-profile addition was the existing SSD's
filesystem UUID check. Calibration, all other configuration and measured joint
positions were unchanged. Feedback returned at 99.99 Hz with zero startup motor
writes. Torque was OFF before the restart and remained OFF; permission stayed
disabled and no operation resumed.

All three native cameras reported 640×480 with zero recovery attempts. A 30-second
read-only browser check received 295/290/294 frames, with maximum gaps below 243 ms,
one connection per camera, no reconnects and no JavaScript errors. Telemetry
committed count increased from 212167 to 212171; history was available. Each camera
capture process used approximately 58 MiB RSS at the subsequent check.

Final logs revealed one further startup defect: the RPC tracer's old ownership
manifest survived while its LTTng session daemon no longer existed. The final
correction reads LTTng's structured XML session list before cleanup, and verifies
absence again after a failed destruction attempt. Unknown errors retain evidence;
only the recorded dead collector's exact session and directory are eligible.
53 focused tracer tests passed. A real LTTng probe with a test-owned stale record
and an absent daemon then launched the full isolated mock graph, reached
`trace_ready: true`, and removed only its own session during shutdown.
