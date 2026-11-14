# Manual control dashboard — November 13, 2026

Manual now selects Joint targets, IK keyboard jog or Leader arm from one dropdown.
The shared lower panel renders the configured URDF as a projected skeleton on
white: black measured joints/links, gray dashed target, red specifically reported
motor fault/stall. Drag orbits; wheel zooms; Reset restores the view. The schematic
is FK, not a collision model. Unknown/stale telemetry never invents or clears an
issue. The marker returns to black only after current following/reference and
health evidence clears that kind of issue. Generic errors remain generic.

Controls keep the station font, aligned rectangular actions and compact rounded
range sliders. Decorative panel/page indices are removed. On short desktop
screens the page can scroll instead of crushing the pose view; the upper control
area scrolls independently while the action row and FK remain separate.

Keyboard IK uses J/K for X−/+, L/H for Y−/+, F/D for Z−/+, I/U elevation−/+,
G/S roll−/+, [/] close/open. Levels 1–5 select speed, Shift temporarily selects
maximum. Tab selects position-only. R/T/E select reference−90°/reference/+90°.
N requests the configured Home in every manual mode, Space preserves the wrist
at Home, M requests configured Fold, and Esc stops. Keys do not operate while
editing an input/select/text field. Releasing keys, blur or hiding the panel
ends held jog intent. Mode changes stop the old producer. The existing DIRECT
stream retains single motor ownership, windows, pickup and normal limiting.
No game controller is included in this revision.

Home preserves the admitted gripper reference when available, including under
contact. The historical Home [0, −50, 65, 0, 0] degrees is an explicit optional
configuration, installed with `scripts/configure-manual-home.py --config PATH
--arm ID`. The old Fold target is outside this Pi's declared shoulder bounds;
it is unavailable, with no change to those limits. Neither preset is an
all-zero fallback. No new physical motion validation is implied by UI tests.

Pure FK/IK/preset routes are `/api/manual/{arm}/describe` (GET) and
`/fk`, `/step`, `/preset`, `/roll` (POST). They calculate only. Motion still
uses the existing DIRECT websocket. Only one solve is pending per panel; old
results cannot restart control after Stop, mode changes or disconnection.
See [calculator contracts](manual-control-backend.md) and [leader bridge](leader-input.md).

Leader uses an actual native input host, with host/device dropdowns populated
from its OS inventory. The bridge needs its matching existing calibration; it
never starts calibration automatically. No USB leader was present on the Mac
at implementation time. An absent host/device is shown as unavailable, not a
sample device. Before moving from Leader to Home/another mode, stop confirmation
is checked. Cleanup uses an operation/session-scoped native hold so late cleanup
cannot stop a replacement operation.

## Telemetry collector

The records-queued metric is retained. It counts durable local telemetry awaiting
external acknowledgment, not robot commands. The course guide page 1 requires
wireless upload plus live/history dashboard and unattended 72-hour recovery;
page 4 section 5.4 calls for expected/received messages and explanations of gaps.
The exact large counter is a UI choice, not a rubric item.

A read-only audit found 50,342 local rows, 128 acknowledged and 50,214 queued,
with no collector endpoint configured. The 128 acknowledgments were the earlier
loopback HTTPS test, not proof of an off-device wireless deployment. Collector
and outbox software exist; choosing/deploying an external collector and the
72-hour measured run remain distinct work. The dashboard names the missing
configuration rather than hiding the queue.

Leader **Gripper follow / O** can freeze the last sent gripper reference while
body poses continue. Re-enabling uses fresh calibrated leader samples; this
setting neither grants permission nor renews input liveness. The FK overlay in
Leader mode is labeled Leader and uses the host's observed pose, never the
Joint-mode draft. IK readouts show measured TCP/elevation/roll/jaw values next
to the corresponding jog buttons.

### Display-only tracking indicators

The native snapshot now exposes the controller's existing per-joint no-progress
ages. An ACTIVE, fresh, lead-limited body joint with admitted-reference error
above its normal tolerance and at least 1 s without measured progress is shown red
as `TRACKING_NOT_PROGRESSING`. `manual_control.stall_indicator_s` may tune the
indicator threshold. The controller's existing outside-envelope tracking state
also appears red for the affected body joint. These diagnostics do not add a
stall-stop deadline, throttle input or change motor limits. Ordinary lead
limiting and normal gripper contact are not labeled as stalls. Recovery still
requires fresh following feedback within tolerance; stale data cannot clear it.
The FK view requests fresh geometry up to 20 Hz, independently of 100 Hz control.

### End-effector axes — November 13, 2026

The FK renderer includes the wrist-to-TCP segment (restored at the operator's
request) and draws X/Y/Z at the actual measured TCP, oriented by the columns of
its FK rotation matrix.
The base grid remains the only grid. The gripper hinge, joint fault markers,
TCP calibration, kinematics and control are unchanged. Both frame indicators
are included in the view fit so their labels remain visible while orbiting.

### Visible gripper fingers — November 13, 2026

The view now draws the fixed finger to the TCP and the hinged moving finger,
using the older controller's SO-101 schematic geometry. Measured gripper
percentage animates the black finger; target/leader percentage drives the
gray overlay. The hinge keeps its existing fault color, and the EE axes remain.
The normalized percentage maps to the URDF jaw limits with a representative
65 mm finger length; this is an opening illustration, not calibrated aperture
metrology. Only the dashboard's device-free FK calculation and rendering changed.
