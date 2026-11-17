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
- Synthetic UI checks at1600×900 and780×1000 confirmed readable component status,
  no horizontal overflow, and preserved control buttons and the fixed FK panel.

## Mac companion

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
