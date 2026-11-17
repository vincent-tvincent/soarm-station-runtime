# Servo authority and control continuity — November 17, 2026

This revision removes independent application joint-angle restrictions and
several control-session interruption paths. Servo configuration and calibration
remain unchanged. The separately coordinated LeRobot/Placo manual-solver migration
is not part of this revision.

## What changed

- Body joint targets are validated for names, units and finite values, without
  YAML/URDF minimum/maximum rejection or clipping. This applies to direct/model
  outputs, leader forwarding, manual IK, Home/Fold/roll presets and browser
  numeric/payload entry. Placo angle limits are explicitly disabled in the
  Cartesian adapter. Slider extents expand to include measured/entered values;
  SO-101 resource ranges describe calibrated servo limits, not application gates.
- Measured-lead caps, reference-envelope rejection and inferred tracking/stall
  shutdowns are removed. Velocity slew remains for smooth dispatch. Tracking
  lag/stall remains visible as diagnostics.
- DIRECT sample/control/feedback delays pause advancement while retaining the
  same operation and permission. Legacy sampled input drops old targets and
  resumes from a fresh sample; persistent targets retain their latched intent.
  DIRECT has no profile-imposed lifetime ceiling. Explicit requested durations
  still work; finite trajectories and model timing keep their own contracts.
- An invalid established WebSocket packet returns `input_rejected`, rather than
  closing the entire control stream. Busy IK responses, transient HTTP failures,
  stale initial pickup and unconfirmed sparse updates no longer terminate a
  healthy keyboard/gamepad operation. Actual disconnection still ends its owned
  operation and requires a fresh Start.
- Automatic cleanup uses captured owner/operation identities. Old responses,
  canceled intents and completed requests cannot issue an arm-wide Hold against
  a replacement operation. The explicit Hold button remains available.
- HTTP action submissions honor client request IDs and are idempotent while
  retained. Scoped cancellation works during pending ROS admission and can
  prevent a delayed POST from submitting at all. Unknown cancellation is not
  reported as completed motion. Request/cancellation metadata is bounded; it
  contains no queue of commands to replay.
- Valid integer-valued JSON angles are converted to ROS floating-point arrays.
  This fixes rejection of otherwise valid targets such as zero-angle waypoints.

Calibration-based coordinate conversion, jaw percentage normalization, packet
representation, finite-value checks, current identities, sole hardware ownership,
velocity interpolation, FK residual checks and real hardware errors remain.

## Verification

- 828 Python tests and 160 subtests passed in the sourced Pi ROS environment;
  8 tests were skipped. All 400 browser tests passed.
- A complete isolated mock ROS graph verified delayed sample pause/resumption
  under the same operation ID, nonfatal malformed packets, targets beyond old
  angle bounds, idempotent HTTP submission, completed-old cancellation leaving
  a replacement operation running, and Stop-before-POST producing no goal.
- The actual installed Pi LeRobot 0.7/Placo adapter numerically solved from a
  −107° shoulder pose outside the former −105° software minimum. FK position
  residual was 6.21×10⁻¹⁷ m. This was numerical computation, not arm motion.
- During an idle interval, the serial owner was stopped before a read-only bus
  inspection. All six servos reported Operating_Mode 0, Phase 12 and
  Angular_Resolution 1; EEPROM calibration matched the existing JSON. No servo
  setting, torque or target was written by that inspection.

Live deployment/read-only checks are recorded in the project deployment note.
Physical following, endpoint saturation and newly requested motion have not
been exercised autonomously as part of these tests.
