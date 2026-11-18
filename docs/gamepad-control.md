# Gamepad control

November 15, 2026. Use **Control → Gamepad · Xbox / DualSense** in the dashboard.
Connect the controller to the browser computer, press a button to expose it to
browser discovery, and select it in **Controller**. One discovered controller
may be selected automatically; selection does not start control.

Center the sticks and release the D-pad, then press **A/Cross** or **Start**.
Stick deflection directly selects motion speed within the chosen profile.
Neither trigger controls arm speed. **B/Circle**, **Stop**, or Escape ends the
operation with support torque retained. Home uses the existing configured pose.

## Mapping

| Control | Operation |
| --- | --- |
| Left stick up/down | +X / −X |
| Left stick left/right | +Y / −Y |
| Right stick up/down | +Z / −Z |
| Right stick left/right | Tool roll − / + |
| D-pad up/down | Fine elevation + / − relative to ground |
| D-pad left/right | Fine tool roll − / +; overrides stick roll |
| LB / L1 | Select LT/L2 as gripper trigger |
| RB / R1 | Select RT/R2 as gripper trigger |
| Selected trigger | Absolute closing amount, after pickup |
| X / Square | Lock/unlock current gripper opening |
| A / Cross | Start |
| B / Circle | Stop / hold |
| Y / Triangle | Configured Home |
| Right stick click / R3 | Cycle speed profiles 1 → 2 → 3 → 4 → 5 → 1 |
| Left stick click / L3 | Cycle primary camera |
| Menu / Options | Cycle Equal / Primary / Floating camera layout |
| View / Create | Show/hide bindings |

The initial gripper source is LT/L2. One shared gripper percentage/bar displays
only the selected trigger, with LT/L2 or RT/R2 beside its heading. The black
source button shows which trigger owns the jaw. The onscreen left/right source buttons perform the same selection
as the shoulder buttons. Selecting the current source again does nothing;
simultaneously pressing both shoulder buttons leaves the source unchanged.
The Guide/PS system button is unused. The onscreen **Reset view** still resets FK.

Directions use the configured base/control frame, not the camera image. Any
configured control-frame yaw rotates XYZ as in keyboard IK. Input uses the
browser's standard Gamepad mapping; unsupported mappings are shown explicitly.
A name containing Xbox or DualSense alone is not evidence of a correct mapping.

## Gripper handoff

The jaw convention is 0% opening = closed, 100% = open. After pickup, the selected
trigger requests `opening = 100 × (1 − processed trigger squeeze)`.

On a source change, the current admitted jaw opening is immediately copied into
the held reference and stream target. Pending results from the old trigger are
discarded. Body targets and the running operation are preserved. The new trigger
enters **Waiting for pickup**, even when the old trigger was locked.

Pickup requires a fresh upward crossing of `held closing + 3 percentage points`,
capped at 100%. For a held opening of 35%, the marker is 68% processed squeeze.
If the newly selected trigger is already above that marker, release below it and
squeeze again. Holding it above the marker cannot jump the jaw. Moving the
inactive trigger has no effect on the gripper or body.

X/Square locks the admitted opening. Pressing it again enters the same pickup
state on the selected trigger. Releasing a locked trigger cannot open the jaw;
releasing it while **Following** requests opening. Trigger switching takes
precedence over X in the same sample; Stop/Home take precedence over both.

While **Following**, squeeze the trigger to grasp. When the jaw stops closing
against an object, the panel shows **Auto-locked**. You can release the trigger
and carry the object with the sticks. Press **X / Square** to unlock, then cross
the displayed pickup marker to take control of the jaw again. If you start with
the jaw already closed, squeeze to pick up the trigger, then release it to open
the jaw. Auto-lock becomes available on the next closing stroke. A full squeeze
from an open position can lock at the closed endpoint. Each pickup can lock once.

The Pi reads the gripper servo's overload flag while a closing goal leads the
measured jaw. An ordinary grasp can stop the jaw before overload protection
trips. The encoder also triggers the lock when the issued goal and requested
opening are both at least 3 percentage points past the jaw and it moves less
than 0.5 percentage points for 300 ms of continuous observations. The hold
retains up to 2 percentage points of the existing closing offset. The percentage
shown in the panel is jaw opening.
[Waveshare ST servo status register](https://docs.waveshare.com/Memory_Map_Explanation/ST_Servo_Memory_Map_Explanation)

## Speed

| Profile | Maximum translation | Maximum orientation |
| --- | ---: | ---: |
| 1 / Fine | 15 mm/s | 10°/s |
| 2 / Normal (default) | 40 mm/s | 25°/s |
| 3 / Coarse | 80 mm/s | 45°/s |
| 4 / Fast | 120 mm/s | 70°/s |
| 5 / Maximum | 160 mm/s | 100°/s |

Each stick has an 8% radial deadzone, with the remaining magnitude remapped
linearly to 0–1. Requested rate is `profile maximum × processed stick component`.
XYZ diagonal magnitude is capped at 1. D-pad fine motion uses 20% of the selected
orientation rate. R3 or an onscreen profile change invalidates an older pending
calculation and resets the elapsed-time anchor. No trigger factor is applied.

Each gripper trigger has a 2% deadzone with the remaining range remapped to 0–1.
The single gripper bar displays processed squeeze; the red marker is the pickup
point for the selected trigger. The jaw remains subject to its configured velocity
limit independently of the body speed profile.

The **Motion speed** percentage/bar shows the current body command as a fraction
of the selected profile: 0% at neutral, up to 100% at full input. It uses the
larger of normalized XYZ magnitude, elevation magnitude and roll magnitude;
neither gripper trigger affects it. The adjacent **mm/s** value is the normalized
XYZ magnitude times the selected translation rate. An angular request also shows
its **°/s** rate. Stopped, suspended or unavailable control shows 0%. These are
requested speeds, not measured motor/TCP speeds; a constrained IK result can
move more slowly. R3 still cycles profiles 1–5.

The numerical Jacobian rank sits in the shared **Arm pose** header, so it remains
visible in Gamepad, keyboard IK, joint targets and leader modes. Its calculation
and rank-loss color mapping are unchanged.

## Source and asynchronous behavior

Motion advances while the Control/Gamepad pane is active and the browser is
focused. After returning from a hidden/unfocused pane, center the sticks and
release the D-pad while focused. Neutral seen only in the background does not
count. Trigger values do not gate body resume. Lock stays locked; an unlocked jaw
returns through pickup. The last target can still finish settling during a pause.

Device disconnect or invalid samples stop the owned source. Reconnecting and
held buttons cannot automatically start a new operation. Source selection,
Stop, Home, mode/arm changes and focus loss invalidate obsolete responses.
The browser samples the selected device when sending input and during display refresh. The native worker integrates the latest admitted rate at its control tick. A steady held stick continues through repeated fresh packets. Neutral and expired input windows hold the admitted position. The operation lifetime is independent of stick activity.

## Implementation and verification

The gamepad interpreter supplies normalized motion and jaw intent. The live browser sends `MANUAL_JOG` over the owned DIRECT stream. The arm's LeRobot/Placo model generates joint targets in the native worker. `POST /api/manual/{arm}/analog-step` is also available to programs requesting an individual calculation. See the [manual backend](manual-control-backend.md) for the math and transport contracts.

Run `node --test tests/test_gamepad_input.mjs tests/test_gamepad_panel.mjs tests/test_manual_panel.mjs`.
The source tests cover handoff races, input scaling, profile changes, source/focus
transitions, lock/pickup and button edges. Browser QA uses simulated inputs and an
in-memory runtime. Physical Xbox/DualSense USB/Bluetooth mapping, actual grip
retention and end-to-end robot motion remain separate validation steps.
