# Merged policy-management verification — November 13, 2026

Reviewed candidate: `0a755306140a3494dad8dc2b9876a1b84eb76f52`, containing
the policy frontend/backend on the exact live Manual baseline
`9284cd760166dd53d6525ec6287c105422117650`.

The independent test checkout is
`/var/tmp/soarm-policy-test-2656393cbc/merged-src`; it has its own Git tree and
uses the existing isolated Python 3.14 environment. Installed ROS interfaces
are read-only. The production checkout/environment and running processes were
not changed or restarted.

## Complete suites

The original merged candidate passed **322 Python tests, 118 subtests and all
134 JavaScript tests**, with eight Python 3.12 native tests skipped in the ROS
environment. After the lifecycle fix below, the complete suites passed
**323 Python tests, 118 subtests and all 134 JavaScript tests**, with the same
eight native-only skips. Python took 9.77 s; JavaScript took approximately
3.49 s. There were no failed or canceled tests.

All **eight native tests also passed** in 10.80 s in a separate task-owned
Python 3.12 test venv. Only that venv received pytest; its dependency search
reads the pinned native environment without changing it. These tests use
tensors/fake engines and open no robot devices. Combined Python coverage is
323 ROS-side tests plus eight native tests, with 118 subtests.

## Lifecycle review and fix

The new dashboard resource view can query policy IPC. The Manual Leader
registry previously received the full dashboard-state callback, which would
now perform that query during Leader Start/conditional-hold/cleanup. Its
callback now reads cached ROS arm state and static physical-resource metadata
directly. It does not depend on catalog/preparation availability.

Policy-store shutdown now runs in aiohttp cleanup, off the event loop, after
Leader shutdown and request drainage. It no longer closes the policy bridge
before those consumers finish. A regression exercises the Leader ownership
check without calling the expanded state provider and checks Leader-before-
policy cleanup ordering.

Manual/FK/IK/Leader modules, `runtime_node.py`, `hold_owned`, scoped
cancellation and `no_progress_s` remain intact. The frontend retains Manual
state/operation/visibility callbacks and adds the policy page alongside them.
No new device owner or ROS IDL was introduced.

## exFAT publication

The host reported `/mnt/ssd` mounted read/write as exFAT. A uniquely named
task-owned scratch directory tested the actual Linux
`renameat2(RENAME_NOREPLACE)` implementation: a fresh directory published, and
an existing destination rejected with `DESTINATION_EXISTS`/409 while both
payloads remained intact.

The actual upload store then transferred a 31-byte text fixture, finalized it
to `cataloged` and rejected a duplicate destination. Its deliberately tiny
fixture inspector performed no model loading. The scratch directory was
removed after the test. No existing SSD model or robot resource was opened.

Machine reports and logs are under the isolated test root:
`merged-suite-result-final.json`, `merged-python-tests-final.log`,
`merged-js-tests-final.log`, `merged-native-tests.log` and
`exfat-publication-report.json`. A final read-only check found production clean
at `9284cd760166dd53d6525ec6287c105422117650`, with the service active
(main PID 52243). Production activation remains with the coordinating frontend
task.
