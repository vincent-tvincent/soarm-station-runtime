# Manual FK, keyboard IK and named targets

November 13, 2026. Implemented as a **device-free target calculator**, not a second controller. Offline tests are evidence for model math and request semantics; they do not establish physical clearance, tracking accuracy or Pi timing. No hardware/service action is performed by this module.

## Integration API

Import `ManualControl` from `soarm_station.manual_control` and construct one instance per explicit configured arm. It needs Python + NumPy only; it can run in the ROS Python 3.14 process using `asyncio.to_thread`. It does not import ROS, Torch, Placo, LeRobot or serial modules. Model arrays are private; calls use local solver state. Recreate the calculator when model/configuration revisions change.

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

`positions_rad` is the exact full configured body-joint dictionary. The gripper is a separate finite percentage: 0 closed, 100 open. Do not fill a missing observation with zero/50 or infer a jaw angle from that percentage. FK can display finite observations outside command bounds; generated targets must remain inside the **configured** joint bounds. This calculator does not silently replace those bounds with different URDF bounds. Deployment configuration must already reflect the selected calibrated model/domain.

`describe()` returns `available`, `reason`, `frame_id`, `model_base_frame`, `tcp_frame`, `model_sha256`, `joint_names`, `keymap`, `speed_levels`, `default_level:2`, `max_dt_s:0.1`, `position_only`, `poses:{home:{available,reason?},fold:{available,reason?}}`, a derived `presets:{home:bool,fold:bool}`, `wrist_roll_reference_rad`, and `roll_presets_rad`. Missing URDF makes FK/IK unavailable; missing named poses do not disable ordinary FK/IK. Geometry is explicitly not a collision check.

`step()` returns:

```json
{"status":"OK","reason":"","target":{"positions":{"joint":0.1},"gripper":37,"gripper_unit":"percent"},"geometry":{},"residuals":{"position_m":0.00001,"orientation_rad":0.0001},"step_fraction":1,"dt_s":0.033,"level":2}
```

Statuses: `OK`, `IDLE`, `BLOCKED`, or `PARTIAL` (jaw input can progress while body IK is blocked). Failed IK retains the supplied reference and reports failure; it never promotes a best-effort numerical pose to success. Input/schema/configuration errors raise `ValueError`; missing capabilities/presets raise its subclass `ManualUnavailable`. Preset/roll responses contain `status`, `name`, `target`, and nullable `geometry` when no model is configured.

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

The caller handles one-shot keys and position-only toggle (Tab); `step` receives held motion keys, `level`, `shift`, and `position_only`. It accepts browser `KeyboardEvent.code` names and lower-case motion letters/brackets. Level 2 is Normal/default for this UI. Input rates for levels 1–5 are 15/40/80/120/160 mm/s, 10/25/45/70/100 °/s and 10/30/60/90/120 jaw percentage points/s, as in the prior input router. These are requested rates, not measured hardware speeds; configured per-joint/gripper velocity limits still apply. XYZ diagonals are normalized. Optional `manual_control.control_frame_yaw_rad` rotates the XY control frame.

Position-only ignores orientation keys/constraints. Ordinary XYZ/elevation jogging freezes the wrist-roll motor unless G/S is active, avoiding an extra independent roll constraint on the five-axis arm. G/S solves position, elevation and tool roll with free azimuth. R/T/E instead select an absolute **motor wrist-roll** target and preserve the other joints; repeated presses do not accumulate angles.

The solver uses URDF rotational-chain FK, analytic translational Jacobians, bounded damped least-squares and checked final residuals. A local orientation chart avoids a reference-axis singularity; full six-dimensional pose is not imposed. It tries progressively smaller increments down to 1/64 when the full increment does not converge. Default computation budget is 40 ms, capped at 100 ms, with at most 24 iterations per attempt. Checks are cooperative, not a hard real-time guarantee. No collision/path safety claim is made by these lines/spheres.

Use one pending solve per browser. Compute `dt_s` from actual elapsed held-input time when producing the next request; it is capped at 100 ms and never accumulated into catch-up requests. At 60 ms round-trip, fixed 1/30-s increments would unnecessarily halve the requested speed. Pass the latest **admitted/applied** reference when available rather than repeatedly integrating from lagged measured joints. Do not accumulate unresolved Cartesian targets. Keep request operation/session/generation identity in the UI and drop responses after key release, mode/arm change, stop, Home, or disconnect. The calculator does not validate network identity and does not refresh any input window.

## Minimal existing-runtime hooks

1. Instantiate from already-loaded explicit `arm_config`; expose read-only computation endpoints through the authenticated dashboard (`describe`, `fk`, `step`, `preset`/`roll`). Offload solve work with `asyncio.to_thread`; bound the caller's pending work and response sizes.
2. FK takes fresh cached measured positions. The UI overlays independently sourced motor health; all current faults/unknowns retain their actual provenance.
3. Starting manual control still opens the existing DIRECT+STREAM operation with its existing permission, READY, pickup, sequence, window and input-expiry behavior. Feed approved calculator targets into that same client; no new writer/ROS action/driver or permission bypass.
4. Before publishing a returned target, recheck current browser operation identity/generation. The runtime still performs current-state/unit/limit/health checks. Calculator success is not actuation permission.
5. Home/Fold/roll targets are explicit user requests sent through the same admitted DIRECT stream/limiter, not a new serial path. Mode switches/Stop/Home invalidate pending keyboard solves and clear held keys. Do not replay targets after reconnect.

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
  solve_budget_ms: 40.0
```

Omitted/null `gripper` also preserves the supplied measured percentage. A finite explicit percentage is supported. Fold remains unavailable: the old Fold shoulder value −104.79° exceeds the current configured −100° bound, so do not import it silently or relax limits to enable a button. Unknown pose names and incomplete/out-of-range configured poses reject; there is no all-zero fallback.

## Offline verification

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=python python -m unittest discover -s tests -p test_manual_control.py -v
```

Nine tests cover an independently calculated two-link FK chain/fixed TCP, SO-101 geometry and separate jaw, every motion-key sign, speed/Shift/diagonal semantics, XYZ and elevation/roll residuals, bounded joint travel, reference integration with lagged measured state, no catch-up, blocked IK, explicit missing data/model, and configured absolute Home/Fold/roll behavior. Local verification uses macOS Python with NumPy 2.2.6; target-Pi and physical acceptance remain separate.

## Manual panel concurrency verification

`web/manual-panel.js` uses separate intent generation and held-key epochs. Start/Home/roll are single-flight user intents; Stop remains available while one is pending. Cancellation invalidates delayed responses and retires late grants/runs rather than sending a target. Key release invalidates a solve without canceling an explicit Home/Start. Normal jogging updates its accepted reference, uses measured pose separately, and keeps at most one pending request.

Run `node --test tests/test_manual_panel.mjs` for 17 hardware-free cases covering delayed permission/Start/Home/roll/leader replies, mode/arm/disconnect cancellation, late uncertain permission cleanup, current reference advancement, no parallel solves, and Home reseeding. These tests instantiate behavioral methods with an injectable minimal client; they do not simulate browser rendering or physical motion.
