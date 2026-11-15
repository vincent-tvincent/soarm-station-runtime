# Gamepad control

November 15, 2026. This describes the current Gamepad implementation in the dashboard. Gamepad-specific deployment and physical-controller validation are separate from source tests and the existing keyboard/manual runtime. The Gamepad mode uses the same selected arm, DIRECT stream, permission, pickup, command-window and runtime-limit machinery; it does not connect to motors itself.

## Start and stop

1. Connect a controller to the computer running the browser. Open the authenticated dashboard and select the intended arm.
2. In **Control**, select **Gamepad · Xbox / DualSense**. Press a controller button if the browser has not exposed it yet, then choose it in **Controller**. A single discovered controller may be selected for display automatically; discovery never starts control.
3. Release **RT/R2** completely. The arm needs valid measured feedback and an available analog IK backend.
4. Press **A/Cross** or the onscreen **Start**. This is an explicit control request through the normal manual flow. A button already held during detection/reconnection is ignored until released and pressed again.
5. Squeeze **RT/R2** to choose the arm-speed ceiling, then deflect the sticks. Start retains the current gripper opening until LT/L2 pickup.
6. Press **B/Circle**, onscreen **Stop**, or the existing Escape/hold control to stop. Stop takes priority over simultaneous gamepad actions. Reconnection does not restart an operation.

Use the controller's browser-provided **standard mapping**. The labels cover common Xbox and PlayStation layouts, but a controller name alone does not establish correct mapping. Nonstandard, incomplete, disconnected or nonfinite samples are rejected. No custom native gamepad driver or input-host process is required for this browser mode; the separate Leader arm mode remains a different input source.

## Controls

Directions below are in the existing configured arm/control frame, not camera-screen coordinates. If a control-frame yaw is configured, it rotates the translation axes in the same way as keyboard IK.

| Control | Operation |
| --- | --- |
| Right stick up / down | +X / −X (keyboard J / K) |
| Right stick left / right | +Y / −Y (keyboard H / L) |
| Left stick up / down | +Z / −Z (keyboard D / F) |
| Left stick left / right | Semantic tool roll − / + (keyboard G / S) |
| D-pad up / down | Fine elevation + / − (keyboard U / I) |
| D-pad left / right | Fine tool roll − / +; overrides left-stick roll while pressed |
| RT / R2 | Arm translation/orientation speed ceiling |
| LT / L2 | Gripper closing amount after pickup; more squeeze requests more closure |
| A / Cross | Start control |
| B / Circle | Stop / hold |
| X / Square | Lock/unlock gripper opening |
| Y / Triangle | Configured Home, using the normal Home path and preserved gripper reference |
| LB / RB or L1 / R1 | Decrease/increase selected speed level, within 1–5 |
| View / Create | Show/hide controller bindings |
| Menu / Options | Cycle primary camera |
| Left stick click / L3 | Reset FK viewer orbit/zoom |
| Right stick click / R3 | Cycle camera layout |

Actions are press-edge driven, not repeated every polling frame. Holding opposing D-pad directions cancels that component; holding both D-pad left/right suppresses left-stick roll rather than falling through to it. The D-pad uses 20% of the selected orientation rate before RT scaling. No additional gamepad button is assigned to Fold; any existing onscreen Fold capability remains configuration-dependent.

## Analog scaling

The default selected level is **2 / Normal**. These are requested ceilings; joint velocity limits, available IK movement and actual system cadence can reduce achieved motion.

| Level | Full-RT translation | Full-RT orientation |
| --- | ---: | ---: |
| 1 / Fine | 15 mm/s | 10°/s |
| 2 / Normal | 40 mm/s | 25°/s |
| 3 / Coarse | 80 mm/s | 45°/s |
| 4 / Fast | 120 mm/s | 70°/s |
| 5 / Maximum | 160 mm/s | 100°/s |

Each stick has a radial deadzone of 0.08. Outside it, the remaining magnitude is linearly remapped to 0–1; partial deflection stays partial. Each trigger has a 0.02 deadzone with the remaining travel linearly remapped to 0–1. The displayed trigger percentages use these processed values.

The arm rate is `selected ceiling × processed RT × processed stick component`. For example, level 2 at 35% RT has a 14 mm/s translation ceiling; a half-scale stick requests about 7 mm/s along that direction. Combined XYZ translation is norm-capped so diagonal movement does not exceed that ceiling. RT is applied once in the backend, not again to already scaled axes.

**RT does not gate the gripper.** After pickup, LT can open/close the jaw while RT is released and the arm is stationary. The analog jaw follows an absolute opening target, subject to the arm's configured gripper percentage-per-second limit, independently of the selected arm speed level.

## Gripper lock and pickup

The motor-facing convention is **0% opening = closed, 100% = open**. LT represents the opposite quantity, closing amount. In Following state the requested opening is `100 × (1 − processed LT)`.

- **Waiting for pickup:** Start/unlock preserves the held opening. Squeeze LT upward through the displayed pickup marker before taking over. The marker is `min(100%, held closing amount + 3 percentage points)`. With a held opening of 35%, closing is 65%, so the marker is 68% processed LT.
- **Following:** LT determines the absolute opening target. Releasing LT requests opening; RT may remain released.
- **Locked:** X/Square captures the current held/admitted gripper reference and preserves it while the sticks continue operating the arm. LT release cannot reopen a locked jaw. The onscreen lock button uses the same behavior.
- **Unlock:** returns to pickup, retaining the captured opening. If LT is already above the pickup marker, release it below the marker and squeeze again; an already-held trigger cannot cause an immediate jump. At the fully closed endpoint, release/repress is still required.

The held-opening source prefers the fresh runtime gripper command reference, then the current owned manual target, then measured opening when establishing a reference. An unavailable opening cannot be replaced with a fabricated 0% or 50% value. Locking changes only the jaw target/reference and invalidates any older outstanding result that could restore a previous closing request.

## Focus, source changes and idle operation

Gamepad motion advances only while the Control/Gamepad pane is active and the browser window is focused. Leaving the pane, hiding the document or losing focus suspends new analog advancement and discards outstanding results. The existing stream retains its last target; it may still finish settling toward that target. This is not a claim of instantaneous physical immobility.

When returning to the active, focused pane, release RT/R2 there before resuming arm-rate input. A neutral trigger observed only in the background does not satisfy this requirement. Gripper lock remains locked; an unlocked gripper returns through pickup. Focus restoration alone does not issue a new operation or replay queued movement.

Controller disconnect, source replacement or invalid samples stop the owned control path and require a fresh explicit Start. Changing controllers stops the old source. Stop, Home, mode/arm changes and disconnect invalidate asynchronous results; late responses cannot restore an obsolete target. Continuous same-direction analog adjustments need not restart the solver on every small noise change, but release, reversal and RT neutralization discard incompatible pending motion. An older jaw result cannot overshoot the latest absolute opening request.

There is **no inactivity timeout based on unchanged controls**. A steady held stick or an unchanged browser `Gamepad.timestamp` is not treated as stale merely because its value does not change. An active idle controller does not automatically end its session. This does not remove the existing network command-window, runtime feedback/source-expiry, device-fault or explicit Stop behavior; those are separate from an arbitrary “you have not moved recently” timer.

## Developer boundary

- [gamepad-input.js](../web/gamepad-input.js) is a pure standard-Gamepad interpreter: deadzones, rates, action edges, pickup and lock. It owns no DOM, device polling, clock, permission or transport.
- [gamepad-panel.js](../web/gamepad-panel.js) reads `navigator.getGamepads()` on animation frames, handles source/focus state, and permits at most one calculation in flight. It sends measured state plus the current accepted reference to the pure calculator. The first calculation uses 1/60 s; later calculations use elapsed time capped at 100 ms. It never sends catch-up bursts.
- [manual-panel.js](../web/manual-panel.js) retains the explicit Start/Stop/Home ownership and generation checks. [transport.js](../web/transport.js) retains the existing authenticated DIRECT stream. The nominal 100 Hz command timer is distinct from browser polling and IK-request cadence; it is not a measured gamepad end-to-end rate.
- [manual_control.py](../python/soarm_station/manual_control.py) implements `analog_step`; [manual_http.py](../python/soarm_station/manual_http.py) exposes authenticated `POST /api/manual/{arm}/analog-step`. It shares the per-arm solve lock with keyboard step. This is calculation only, not a new actuation endpoint or motor owner.

Example calculation payload:

```json
{
  "positions": {"shoulder_pan": 0.0, "shoulder_lift": -0.8, "elbow_flex": 1.1, "wrist_flex": 0.0, "wrist_roll": 0.0},
  "gripper": 35.0,
  "translation": [0.5, 0.0, 0.0],
  "elevation": 0.0,
  "roll": 0.0,
  "speed_scale": 0.35,
  "gripper_target": 35.0,
  "dt_s": 0.033,
  "level": 2,
  "reference_positions": {"shoulder_pan": 0.0, "shoulder_lift": -0.8, "elbow_flex": 1.1, "wrist_flex": 0.0, "wrist_roll": 0.0},
  "reference_gripper": 35.0
}
```

Those positions are illustrative request syntax, not a physical pose to execute. Use the selected arm's exact named joint set and real measured/reference data. Axes must be finite and within −1…1, speed scale within 0…1, and gripper target within 0…100 or null. Null leaves the reference jaw unchanged. Oversized XYZ diagonals are normalized; subunit magnitudes are preserved. The calculator returns the existing target/geometry/status format. BLOCKED is a calculation result, not permission revocation; smaller converged increments are reported with `step_fraction` and shown in the UI. The corrected solver has no 40 ms elapsed-time acceptance cutoff; see [manual backend details](manual-control-backend.md).

## Verification and limits

Relevant hardware-free commands:

```bash
node --test tests/test_gamepad_input.mjs tests/test_gamepad_panel.mjs tests/test_manual_panel.mjs
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=python python -m unittest discover -s tests -p test_manual_analog.py -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=python python -m unittest discover -s tests -p test_manual_ik_regression.py -v
```

The source tests cover axis signs, deadzones/scaling, diagonal caps, button edges, lock/pickup, source/focus changes, stable unchanged inputs, late-result rejection, one pending solve, HTTP validation and shared locking. The captured positive-Y IK regression was also checked as isolated Pi math; that is not gamepad-driven physical validation.

Simulated Gamepad samples and hardware-free browser checks do **not** establish compatibility with a particular physical Xbox/DualSense model, USB/Bluetooth connection, browser/OS mapping, grip retention, actual opening width, physical clearance, sustained control rate or end-to-end latency. Record those separately when the selected controller and robot are explicitly tested. Browser trigger percentage and modeled jaw angle are not measurements of gripping force or aperture. No training/dataset collection capability is added to production Gamepad mode.
