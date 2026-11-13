# Manual controls deployment — November 13, 2026

Final Pi source revision: `2096835` (manual feature base `465f8ef`, display-only
tracking diagnostics `4dde194` / compact wording `d32b28a`). Local development
checkout: `/private/tmp/soarm-dashboard-build`. Deployment used Git bundles and
cherry-picks into `/home/pipi/so-101/ros_system`; no GitHub push.

The approved Joint / IK / Leader interface is live at
`https://ubuntu-pi.tail521c60.ts.net:8443/#operations`.
Native/ROS ownership and the 100 Hz control loop remain intact. New FK/IK routes
calculate targets only; the existing DIRECT stream sends them. The UI offers
20 Hz FK updates, named key mappings, speed 1–5 / Shift and Home N. The existing
rounded sliders remain compact. See [manual guide](manual-dashboard.md),
[calculator contract](manual-control-backend.md), and [input-host setup](leader-input.md).

The explicit established Home descriptor was installed with:

```bash
.venv/bin/python scripts/configure-manual-home.py \
  --config local/pi-writable.yaml --arm arm1
```

It preserves the gripper and other configuration, saving a private local backup.
Fold remains unavailable because the historical shoulder angle exceeds this
profile's configured lower bound; those bounds were not changed.

## Verification

- 136 Python tests plus 76 subtests passed on the Pi with mock devices, including
runtime/continuity, native-dispatch boundaries, ROS serialization, API access,
manual calculators, leader bridge and diagnostic transport.
- 74 frontend tests passed on the Pi, including 17 asynchronous manual races and
3 joint-indicator state tests.
- The final display-only diagnostic follow-up passed 96 relevant Pi tests plus
20 subtests. This diagnostic flags sustained measured body-joint nonprogress
without introducing a stall-stop timeout. Gripper contact is excluded.
- 40 read-only IK calculations against the configured live model succeeded:
median 2.01 ms, maximum 3.30 ms. These were calculations, not movement commands.
- Live HTTPS metadata, FK calculation, source discovery and new assets passed.
Home was available; Fold unavailable; no input hosts were registered.
- The native Mac OS inventory had no USB leader attached. Real leader pickup and
physical keyboard jog/Home execution were not tested in this update.
- Browser checks confirmed simulated jog/speed/Home/Stop, real read-only mode
switching, source-unavailable state, black spheres and isolated simulated red
elbow. Screenshots and design comparison are in `design-qa.md`.

Both service reloads were preceded by a fresh idle/no-permission check. Final
read-only native observation:

| Check | Result |
|---|---|
| Phase | IDLE |
| Permission / active operation | false / empty |
| Joint feedback | 100.0 Hz |
| Torque | ON |
| Startup goal writes | 0 |
| Max measured position change from pre-deploy | 0.0 rad |
| Calibration hashes | unchanged |
| Camera feeds | 3 fresh feeds |
| No-progress timers | empty |

No physical move, torque-off, calibration/PID change or policy execution was
issued. Source reloads were coordinated with the independent Policies-page
agent; that feature remains a separate integration.

## Collector status

The telemetry queue is preserved and the UI states that the collector is not
configured. Local durable telemetry and HTTPS collector code exist. The current
profile has no external collector URL; the prior 128 acknowledgments were a
loopback verification. Off-device wireless receipt and the 72-hour run therefore
remain unverified. This is deployment/evidence work, not a reason to delete the
counter. Course guide references: page 1 Connect/Survive, page 4 section 5.4.
