# Manual FK, keyboard IK and named targets

Manual Cartesian jogging uses LeRobot 0.7.0 and Placo in the native Python 3.12 worker. The live DIRECT operation owns its arm and integrates manual input at the configured control frequency. FK, model diagnostics, named targets and the public calculation endpoints keep their existing interface.

## Integration API

Construct one `ManualControl` per configured arm. FK and display geometry run with NumPy. Calling `prepare_jog()` loads the arm's persistent LeRobot model. The production dashboard prepares it when it describes an arm and advertises `native_jog: true` on success. Standalone callers can use the native virtual environment and call `step()` or `analog_step()` directly.

```python
calculator = ManualControl(arm_config)
calculator.describe()
calculator.fk(positions_rad, gripper_percent)
calculator.step(positions_rad, gripper_percent,
    keys=['KeyJ'], dt_s=0.033, level=2, shift=False,
    position_only=False, reference_positions=None, reference_gripper=None)
calculator.preset('home', positions_rad, gripper_percent, preserve_wrist=False)
calculator.roll_preset('minus_90', positions_rad, gripper_percent)
```

`positions_rad` is the exact full configured body-joint dictionary. The gripper is a separate finite percentage: 0 closed, 100 open. Do not fill a missing observation with zero/50 or infer a jaw angle from that percentage. Finite body angles are accepted for observations, IK seeds and generated targets without a YAML/URDF absolute-angle gate. The servo position registers are the angle authority. Calibration still defines the coordinate conversion; no calibration file or normalization convention is rewritten.

`describe()` returns `available`, `reason`, `frame_id`, `model_base_frame`, `tcp_frame`, `model_sha256`, `joint_names`, `keymap`, `speed_levels`, `default_level:3`, `max_dt_s:0.1`, `position_only`, `poses:{home:{available,reason?},fold:{available,reason?}}`, a derived `presets:{home:bool,fold:bool}`, `wrist_roll_reference_rad`, and `roll_presets_rad`. Missing URDF makes FK/IK unavailable; missing named poses do not disable ordinary FK/IK. Geometry is explicitly not a collision check.

`step()` returns:

```json
{"status":"OK","reason":"","target":{"positions":{"joint":0.1},"gripper":37,"gripper_unit":"percent"},"geometry":{},"residuals":{"position_m":0.00001,"orientation_rad":0.0001},"step_fraction":1,"dt_s":0.033,"level":2}
```

Statuses: `OK`, `IDLE`, `BLOCKED`, or `PARTIAL` (jaw input can progress while body IK is blocked). Each jog is a differential movement request. Actual progress, position residual and angular residual describe the returned target. A blocked body holds its reference. Input/schema/configuration errors raise `ValueError`; missing capabilities/presets raise its subclass `ManualUnavailable`. Preset/roll responses contain `status`, `name`, `target`, and nullable `geometry` when no model is configured.

FK geometry is JSON-safe:

```text
{
  frame_id, model_base_frame, tcp_frame,
  units: {position:'m', joint:'rad', gripper:'percent'},
  position_m:[x,y,z], rotation_matrix:[[...],[...],[...]],
  elevation_rad, tool_roll_rad, azimuth_rad,
  joints:[{name, urdf_name, role:'joint'|'gripper', position_m:[x,y,z],
           axis_base:[x,y,z], position_rad:number|null}],
  segments:[{from:'base'|jointName, to:jointName|'tcp'|tipName, start_m:[...], end_m:[...], role?}],
  tcp:{position_m:[x,y,z], frame:tcpFrame}, gripper_percent, gripper_visual
}
```

The TCP includes its fixed URDF transform. The gripper marker stays at its hinge and has `position_rad:null`. For the SO-101 moving-jaw link, FK also returns fixed and moving finger segments. The moving finger uses the previous controller's representative 65 mm local -Y tip and maps normalized opening linearly to the URDF revolute limits. `gripper_visual` reports this explicitly as schematic; its `visual_angle_rad` is not a calibrated aperture measurement. This mapping never changes control targets or the measured percentage. Other models retain null `gripper_visual` rather than receiving guessed fingers. `frame_id` is the configured ROS alias for the URDF root; this calculator performs no TF lookup. That alias must describe the same base frame. `rotation_matrix` is the actual TCP rotation; semantic elevation/roll use the operator-tool axes (default TCP rotation multiplied by Rz(π), matching the previous SO-101 UI). The UI owns health coloring: black normal spheres, red only from current reported fault/stall evidence; no geometry computation implies health.

## Keyboard behavior and timing

| Keys | Increment |
| --- | --- |
| J / K | base/control-frame X + / − |
| L / H | Y − / + |
| F / D | Z − / + |
| I / U | tool elevation − / + |
| G / S | semantic tool roll − / + |
| [ / ] | jaw close / open (numeric percentage decreases / increases) |
| 1–5, Shift | selected level; Shift temporarily selects maximum |
| R / T / E | configured motor wrist-roll reference −90° / reference / +90° |
| N / Space / M | configured Home / Home preserving wrist / Fold |

The caller handles one-shot keys and position-only toggle (Tab); `step` receives held motion keys, `level`, `shift`, and `position_only`. It accepts browser `KeyboardEvent.code` names and lower-case motion letters/brackets. Level 3 (Coarse) is the default; level 2 remains Normal. Input rates for levels 1–5 are 15/40/80/120/160 mm/s, 10/25/45/70/100 °/s and 10/30/60/90/120 jaw percentage points/s, as in the prior input router. These are requested rates, not measured hardware speeds; configured per-joint/gripper velocity limits still apply. XYZ diagonals are normalized. Optional `manual_control.control_frame_yaw_rad` rotates the XY control frame.

Position-only ignores orientation keys/constraints. Ordinary XYZ/elevation jogging freezes the wrist-roll motor unless G/S is active, avoiding an extra independent roll constraint on the five-axis arm. G/S solves position, elevation and tool roll with free azimuth. R/T/E instead select an absolute **motor wrist-roll** target and preserve the other joints; repeated presses do not accumulate angles.

LeRobot's `RobotKinematics` supplies the model, frame task and Placo solver. Each tick starts from the native owner's admitted joint reference. A sliding target follows the requested translation with a transverse correction toward the line through the gesture's start pose. A masked orientation task maintains elevation and allows free azimuth. During roll input it also requests tool-axis spin. Position-only removes the orientation weight. The jaw remains independent.

Placo enforces the configured joint velocities for the time step. Station disables model angle inequalities so the servo retains absolute-angle authority. Near a weak task direction, a trial that approaches or crosses a singular branch is recomputed from the same seed with joint regularization. Both solves use LeRobot's existing Placo solver and the same time allowance. The reported fraction reflects forward progress along the requested motion. Exact reach limits still exist.

Live keyboard and gamepad input use the existing owned DIRECT WebSocket and typed ROS command stream. A `MANUAL_JOG` packet specifies normalized translation, elevation, roll, profile, position-only and a jaw rate or absolute opening. The native control tick integrates the most recent admitted intent while its server-issued input window is fresh. On expiry it holds the last joint reference. Receiving neutral changes the next tick to hold. A new DIRECT joint target replaces the jog, including for Home and Fold. Feedback reports jog status, progress and the admitted body and jaw reference.

The calculation-only HTTP endpoints retain their existing request and response shapes. They use a separate solver instance in native Python via local IPC. An external calculation cannot alter a live gesture's anchor or Placo task state.

## Body-angle authority and display metadata

`min_rad`/`max_rad` are optional legacy display/model metadata. They do not
restrict manual seeds, Home/Fold/roll poses or generated joint targets.
`display_min_rad`/`display_max_rad` can supply an explicit UI extent. Missing or
unusable display extents fall back to ±π; a display range is never a command
admission rule. The descriptor reports `application_angle_limits:false`.

For SO-101 dashboard resources, readable calibration takes precedence: its raw
minimum/maximum map to radians with `(raw - (min + max)/2) * 2π / 4095`, matching
LeRobot DEGREES normalization. Resources retain `min`/`max` for UI compatibility
and mark them `range_enforced:false`; `range_source` identifies calibration,
configured display metadata or the default. This reads the calibration file,
not the bus; the native owner separately checks calibration against EEPROM.

The Cartesian LeRobot/Placo adapter explicitly disables Placo joint-limit
constraints. FK residual checks, finite values, joint mapping, normalized
quaternions, angular chart math and velocity integration remain. The URDF still
supplies joint axes, origins, types and TCP geometry; changing a revolute joint
to continuous is not required. The jaw remains a separate 0–100% quantity.

## Explicit Home reference configuration

No pose is enabled automatically. The following Home values come from the historical `calibration/presets/waveshare_follower_home.json`: pan/lift/elbow/flex/roll = [0, −50, 65, 0, 0] degrees. This is a **configuration reference**, not new physical validation. Apply under the chosen arm only after confirming the intended model/calibration; preserve its other fields.

```yaml
manual_control:
  poses:
    home:
      positions:
        shoulder_pan: 0.0
        shoulder_lift: -0.8726646259971648
        elbow_flex: 1.1344640137963142
        wrist_flex: 0.0
        wrist_roll: 0.0
      gripper: preserve
  wrist_roll_reference_rad: 0.0
  control_frame_yaw_rad: 0.0
  operator_tool_rpy: [0.0, 0.0, 3.141592653589793]
```

Omitted/null `gripper` also preserves the supplied measured percentage. A finite explicit percentage is supported. The named-pose template includes the operator-observed Fold from August 5, 2026, with its original joint values converted to radians and jaw preservation. On November 15 the user approved extending the shoulder lower bound and rounding it down to **−105°** (`-1.8325957145940461 rad`); the vendored URDF matches that bound. That historical lower-bound adjustment is now display/model metadata; installing the preset does not require matching a software angle threshold. The recorded Fold target is unchanged. Unknown pose names, incomplete named poses and nonfinite values still reject; there is no all-zero fallback.

Recorded Fold degrees: shoulder pan `-0.7032967033`, shoulder lift `-104.7912087912`, elbow flex `95.2527472527`, wrist flex `74.8571428571`, wrist roll `-0.9230769231`. The saved calibration hash is `c816f1bbaa8122bb40475699f5a74724fbe8cce65634f02d14b5e8e0e4265814`, matching this deployment. This preset import does not execute Fold or constitute a new physical path validation.

## Verification

Run `scripts/test` on the Pi to exercise math and the simulated native controller with the LeRobot interpreter, then the gateway and ROS tests with the ROS interpreter. Run `node --test tests/*.mjs` for browser input, lifecycle and transport coverage. The saved near-extension case in `tests/test_manual_ik_regression.py` checks held lateral movement, elbow reversals, transverse error, elevation, joint velocities and immediate direction reversal. `tests/test_native_jog.py` exercises uneven packet arrivals, neutral, an expired input window and replacement by an absolute goal using a simulated clock and plant.

## Analog manual input

`analog_step` accepts normalized `translation:[x,y,z]`, `elevation`, `roll`,
`speed_scale`, optional absolute `gripper_target` in opening percent, `dt_s`,
`level`, and the same measured/admitted references as keyboard jogging. The
HTTP entry is `POST /api/manual/{arm}/analog-step`. Diagonal translation is
norm-capped without amplifying small input; the trigger scale multiplies body
rates only. The absolute jaw target advances independently under the existing
jaw velocity limit. Both producers share the corrected solver and per-arm
calculation lock. This endpoint calculates a target and never dispatches it.

## Live arm conditioning — November 16, 2026

The dashboard displays **Conditioning**, a dimensionless reciprocal condition
number `sigma_min / sigma_max`. It varies continuously as the measured arm pose
approaches a singularity: 0 denotes numerical singularity and values nearer 1
indicate more balanced motion capability. It is not a physical distance, joint
limit margin, safety percentage or execution restriction. Rank and the measured
timestamp are no longer displayed.

The reported scope is the full configured body arm, in every control mode.
The separate jaw does not participate. This describes loss of the arm's own
independent motion directions; it is not a task-specific IK success prediction.

For each rotational joint, the geometric Jacobian column is
`[axis × (TCP − origin); axis]`. To avoid mixing metres and angular units, the
linear rows are divided by a fixed characteristic model length `L`:

```text
J_scaled = [ J_linear / L ]
           [ J_angular    ]
conditioning = smallest_singular_value(J_scaled) / largest_singular_value(J_scaled)
```

`L` is the sum of URDF translation lengths after the first actuated joint through
the TCP. It does not change with the measured pose or an unrelated fixed base
placement. Uniformly scaling all model lengths preserves the indicator. For the
bundled SO-101 model, `L = 0.47794512444016124 m`. This scaling is our explicit
metric convention; changing the model/TCP can change its values.

The thin SVD returns five singular values for the SO-101's 6×5 matrix. Do not
append a sixth zero: six independent Cartesian directions are structurally
unavailable on this five-axis arm. Values below the floating-point SVD tolerance
report zero; a model with no nonzero characteristic length reports unavailable.

`POST /api/manual/{arm}/fk` adds this object at
`measured.jacobian.conditioning`:

```json
{
  "available": true,
  "reciprocal_condition": 0.06634749774247155,
  "characteristic_length_m": 0.47794512444016124,
  "matrix_kind": "scaled_geometric",
  "scope": "full_arm",
  "singular_values": [1.9086059456, 1.1895622594, 0.9215250833, 0.1867435001, 0.1266312287],
  "reason": ""
}
```

The example is computed at the historical Home pose used by the unit tests,
not live hardware. A captured near-extension pose gives `0.0011629599103152586`
with the same scale. Both had previously displayed rank 5/5.

The legacy raw-Jacobian rank and singular-value API fields remain available to
existing clients, but are not used by the dashboard display. Plain `fk()`, target
geometry, IK constraints and motor dispatch are unchanged. Missing/stale feedback
shows an em dash rather than a fabricated zero. Small nonzero values retain three
significant figures, switching to scientific notation when needed.

Validation includes an independently derived planar Jacobian, continuous decline
before rank loss, exact singularity, uniform model scaling, missing model scale,
measured-versus-target selection and stale data. The timestamp component was
removed entirely. Background diagnostics do not open devices or execute motion.

Background: [Modern Robotics, Manipulability](https://modernrobotics.northwestern.edu/nu-gm-book-resource/5-4-manipulability/)
explains singular-value ratios and the different units of linear/angular motion;
the fixed model-length normalization above is this application's choice.

## Shared FK viewport — November 16, 2026

The Operations page reserves the lower half of its operator-panel content area
for one shared FK view. Status, Control and Payload occupy an independently
scrolling upper half. Tab changes preserve the viewer, orbit/zoom and measured
conditioning; the FK element is never duplicated or recreated by a tab switch.

Pose visibility is separate from Control input focus. Measured pose/leader
observations continue to refresh on Status and Payload, while keyboard/gamepad
input remains scoped to the visible Control tab. Leaving Operations suspends
pose requests. Control rates, stream ownership and motor behavior are unchanged.

Desktop layout follows the available viewport instead of forcing a 900-pixel
workspace. Short desktop viewports use a compact page heading/metrics row.
Narrow layouts stack the camera section above a bounded operator panel and keep
the same equal split. FK status details have a hover tooltip when abbreviated.

Validation: all 345 JavaScript regressions pass, including shared-pose updates
with Control hidden and keyboard/gamepad focus isolation. Mock browser renders
cover Status, Control, Payload and narrow layout; DOM measurements confirm equal
halves, one live FK element and no desktop document overflow.

Control action buttons are pinned beneath the scrolling input area: **Start,
Stop, Home and Fold** remain visible while joint, IK, leader or gamepad inputs
scroll. The action row is shown only for the Control tab. Browser layout checks
at 1280×720, 2560×1440 and 3840×2160 confirmed that all four buttons remain above
the shared FK view and outside the input scroll container.


## Operations summary bar and torque control

The bar shows joint feedback rate/age, measured conditioning, current reported
errors, measured torque, and mean motor temperature/voltage. Means use finite,
current readings from the selected arm's body motors and gripper. Missing/stale
readings are excluded, with coverage in the tooltip; no available readings show
an em dash. Torque distinguishes On, Off, Mixed and Unknown. Error summaries use
reported runtime faults, motor issue latches and diagnostic errors; ordinary
operator Hold is not an error. Stale data never implies an all-clear state.
Conditioning reuses the shared FK computation rather than adding another poll.

The separate toolbar torque button invokes `/api/torque`. Its label follows
measured torque; pending changes are acknowledged by the motor owner. Control
permission remains separate. Disabling removes holding force, so its tooltip
explicitly calls for supporting the arm. No automatic torque changes are made
by monitoring, page navigation or summary calculations.


The torque feature was validated with 351 browser/unit regressions and 149 Linux
controller/ROS/gateway tests, including complete torque readback and cancellation
races. UI checks at widths 700, 1280, 1440 and 3840 verified all seven summary
cells without horizontal overflow; a local in-memory mock exercised both button
states. Hardware torque transitions are intentionally not part of automated
production deployment checks.
