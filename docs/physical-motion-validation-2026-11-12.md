# Native writable control qualification — November 12, 2026

This dated report uses the package/interface names of its tested revision. For current SO-Arm Station commands, see [startup](startup-and-debug.md) and [name migration](name-migration.md).

This is the initial physical baseline. Later session lifetime/permission/reference behavior is revised in [the November13 continuity report](continuous-operation-2026-11-13.md); initial normal-completion re-enable behavior is historical.

The user explicitly approved native LeRobot connection and controlled motion testing on the secured Pi. This supersedes the initial read-only deployment restriction. The active service profile is `local/pi-writable.yaml`; `local/pi.yaml` remains the read-only fallback. No calibration or PID change was performed.

## Connection and startup

Native LeRobot 0.7.0 `SerialMotorsBus` connected to the verified follower at `/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B3D042855-if00`. Live calibration exactly matched the existing file, SHA256 `c816f1bbaa8122bb40475699f5a74724fbe8cce65634f02d14b5e8e0e4265814`. Three seconds produced 300 fresh native reads at 99.997 Hz, maximum observed gap 10.15 ms. Torque stayed OFF and goal-write count stayed zero during that first connection-only check.

The writable service still starts with logical motion permission disabled and emits no startup goals. First authorized dispatch stages a freshly measured raw pose, explicitly enables torque when needed, verifies enable, and then sends bounded targets. A later torque loss faults instead of silently rearming. Connection/disconnection do not configure gains, overwrite calibration or disable support torque.

## Real motion results

The production HTTPS → typed ROS action → sole native hardware owner path passed **16 phases**:

- measured-pose arming;
- out/return motion for shoulder pan, shoulder lift, elbow flex, wrist flex and wrist roll, using approximately 0.15 rad / 8.6-degree requested strokes;
- gripper opening and return using a ten-percentage-point stroke;
- a coordinated timed joint trajectory and return;
- hold while a trajectory was active;
- a final baseline command.

The physical stream test then sent 250 DIRECT commands in 2.5 seconds and measured 0.0721 rad / 4.13-degree pan excursion. Disconnect latched HOLD with permission disabled and torque preserved. A second stream sent heartbeats without fresh poses; its actual operation result was `DIRECT_INPUT_EXPIRED`. No autonomous learned policy was executed.

Fresh feedback observed during the full motion run was 100.0006 Hz, with maximum observed source gap 13.66 ms. The hold-service round trip during motion was 17.72 ms; that is software confirmation latency, not a claim of instantaneous physical stopping. Recorded aggregate voltage reached a minimum of 11.7 V, and maximum recorded motor temperature was 35 C. This is not a full-load power/current qualification.

## Accuracy and failures retained

The initial 0.08-rad tests passed pan and shoulder motion but stopped on elbow tracking timeout. Those failed traces remain under ignored `local/controlled-motion-initial.json` and `local/controlled-motion-interpolator.json`.

The first trace exposed a controller error: limiting every new goal relative to measured position restricted the servo error to one tiny control step. The corrected limiter advances from the previous commanded goal, while keeping the velocity limit and a 0.12-rad measured target-lead bound.

A separate elbow diagnostic verified the native motor actually accepted an 85.06-degree goal while measured position settled near 88.22 degrees: about **3.17 degrees of physical following error**, with P=16, I=0, D=32 unchanged. This agrees with the earlier documented stock-gain elbow residual. No precision-control success is claimed.

The qualified local profile therefore declares `goal_tolerance_rad: 0.07` (4.01 degrees) for the elbow and 0.03 (1.72 degrees) for the other body joints. The full run's maximum reported elbow endpoint error was 3.16 degrees; other body-joint errors reached 1.41 degrees, and gripper endpoint errors reached 0.48 percentage points. The physical script allows a further 0.01-rad observation/settling margin in its separate post-result check and retains the actual values. A tighter application needs an explicitly validated accuracy profile; loosening completion tolerance does not improve physical accuracy.

Successful timed-path completion now preserves the approved final motor goal, rather than replacing it with a lagging measurement and losing support torque. Explicit cancel, expiry and hold still use a fresh measured pose and only target motors actually measured torque-on. Completion also requires the endpoint itself to have been dispatched. A failed hold cannot produce a successful operation result. Confirmation is tracked per operation, so a later unrelated fault does not rewrite historical success. Fault recovery uses a fresh controller session and resets write/hold state; it cannot replay a pending goal on reconnect.

## Final state and regression evidence

- 29 runtime regression tests passed, including new native arming/hold, torque-loss, stalled-plant interpolation, final-goal and per-joint tolerance cases.
- An independent native read found all six Torque_Enable registers equal to 1 and all six Status registers equal to 0.
- Every motor retained P=16, I=0, D=32. Calibration matched byte-for-byte. No gains/calibration were written.
- Writable-service restart changed the runtime boot identity, produced zero goal writes, kept permission false and torque ON, and showed zero measured joint change across the check.
- The arm was visually checked in the shared camera views and left holding its compact pose. The native writable service remains active; motion requires a new explicit permission grant.

## Reproduction

These commands move real hardware and were explicitly authorized for this session. Preserve access to power, use the correct resource ID, and stop the service before a standalone native connection check so only one owner opens the serial bus.

```bash
cd /home/pipi/so-101/ros_system
scripts/test
# Against the already-running writable service:
set -a; source local/secrets.env; set +a
.venv/bin/python scripts/controlled-motion-check.py \
  --config local/pi-writable.yaml --arm arm1 --amplitude-rad 0.15 \
  --output local/controlled-motion-qualified.json --execute
.venv/bin/python scripts/controlled-stream-check.py \
  --config local/pi-writable.yaml --arm arm1 \
  --output local/controlled-stream.json --execute
```

Evidence files are ignored by Git: `local/native-connection.json`, `local/elbow-goal-readback.json`, `local/controlled-motion-qualified.json`, `local/controlled-stream.json`, `local/native-final-independent.json`, and `local/writable-restart-result.json`. The training repository and upstream LeRobot sources were unchanged. No repository remote was added and nothing was pushed.
