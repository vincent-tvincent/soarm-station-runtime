# Native leader input hosts

This bridge makes the Control panel's Leader host/device dropdown reflect actual OS serial
devices on a connected Mac or Linux input host. The browser does not discover or
open USB on its own. An unconfigured serial adapter is visible but cannot be
started; a configured candidate is checked against live motor calibration only
after the operator presses **Unlock / follow**. Existing LeRobot calibration files can be
reused unchanged; an additional input-host profile associates a file with its
physical USB device.

## Reuse an existing leader calibration

Use the input computer's existing compatible LeRobot environment with `aiohttp`
and `pyserial` installed. YAML profiles additionally need PyYAML; the supplied
template uses JSON. No ROS installation is needed on the input computer.

Keep these files on the computer connected to the **leader's USB cable**. In a
Mac-leader/Pi-follower setup, that means the Mac. The follower does not need to
be reconnected, and no calibration file needs to be rewritten on either arm.

Use this layout inside the runtime checkout on the input computer:

```text
local/leader-input/
├── input-host.json          # Computer identity, device match and calibration path
├── calibration/
│   └── leader.json          # Copy of this leader's existing LeRobot calibration
└── station-ca.pem          # Public Station certificate, for a private TLS setup
```

`local/` is ignored by Git. A calibration JSON and an input-host JSON are
different files: pass **input-host.json** to `--config`, not the motor-calibration
file itself. The calibration file contains the per-motor IDs, homing offsets
and ranges; the input-host file says which device uses it.

### 1. Copy the existing calibration

From the runtime repository root on the input computer:

```bash
mkdir -p local/leader-input/calibration
cp -n /path/to/your/existing/leader-calibration.json \
  local/leader-input/calibration/leader.json
cp -n config/leader-input.example.json local/leader-input/input-host.json
```

These commands preserve existing destination files. If an input-host profile
already exists, add or update its relevant leader entry rather than replacing
the whole file. Give each additional leader a distinct calibration filename.

Use the calibration file saved for that physical leader. LeRobot normally
stores it under its calibration directory in
`teleoperators/<teleoperator-name>/<id>.json`; a custom `calibration_dir` or
`HF_LEROBOT_CALIBRATION` changes the location. Use the path printed by the
calibration command or the working configuration you already use. Copying this
file does not run calibration or write motor registers.

### 2. Match the USB device

Use the same compatible Python environment that you use for LeRobot:

```bash
export SOARM_STATION_INPUT_PYTHON=/path/to/your/lerobot-venv/bin/python
scripts/run-leader-input --list
```

This lists OS device metadata without opening a motor port. In
`local/leader-input/input-host.json`, replace
`REPLACE_WITH_LEADER_USB_SERIAL` with the device's actual `serial` value.
If the adapter has no unique serial number, use an explicit `match.port`
instead. The profile's `id` is a logical name; it is not the USB serial number.
Set `host_id` uniquely for each input computer, and use `label` fields for the
names shown in the dashboard.

The supplied template already refers to `"calibration": "calibration/leader.json"`.
That path is relative to **input-host.json**, so it resolves to the file in the
layout above. Absolute paths are also accepted. The SO-101 template maps the
five body joints directly and keeps the gripper separate; adjust `joint_map`
only when the selected follower uses different joint names.

Check the complete local configuration:

```bash
scripts/run-leader-input --config local/leader-input/input-host.json --list
```

The intended device should report `configured: true`. This checks discovery,
file structure and mapping; it does not yet verify calibration against the
physical motors or begin following. No matching profile, an ambiguous match,
missing file or invalid calibration is reported in the device's `reason` field.

### 3. Connect the input host to Station

For a Station installation using its generated private certificate, copy its
**public certificate** to the input computer once:

```bash
scp user@station-host:/path/to/soarm-station-runtime/local/tls/cert.pem \
  local/leader-input/station-ca.pem
```

Then launch the bridge with your Station URL:

```bash
scripts/run-leader-input \
  --config local/leader-input/input-host.json \
  --url https://your-station-host:8443 \
  --ca local/leader-input/station-ca.pem
```

Use the hostname covered by the certificate. If Station uses a certificate
already trusted by the input computer, `--ca` can be omitted. Trusted Tailscale
peers use Station's existing access policy; no private key needs to be copied.

**If the bridge was already running, stop that bridge and restart it with the
updated profile.** Its configuration is loaded at startup; dashboard Refresh
does not reload the local JSON. Station itself does not need a restart.

### 4. Select it in the dashboard

Open **Control → Leader arm**, press **Refresh**, select the input computer,
then select the configured leader. The native bridge must remain running while
using it. Press **Unlock / follow** to check calibration and start sampling. The follower
stays held during **ALIGNING**; match the raw calibrated leader pose to the held
follower to begin following. Body pickup uses 0.12 rad per joint (about 6.9°),
and 10 percentage points for the jaw when **Gripper follow** is enabled. The
leader overlay and sampling rate keep updating during alignment. There is no
alignment countdown. **Freeze / Esc** holds the follower; unlock and align to
resume. Home first stops following, moves to its configured target, and can
hand back to the leader through the same unlock-and-align sequence.

If Start reports a calibration mismatch, select the existing file that belongs
to the attached motors. The bridge does not automatically overwrite motor
calibration. This guide configures an existing calibration; it does not replace
LeRobot's calibration procedure when a device actually needs one.

## Data path and ownership

```text
Input host: OS inventory -> local leader profile -> read-only native sampler
    -> existing HTTPS/WSS 8443 gateway -> DIRECT+STREAM -> sole follower runtime
```

The input host makes outbound connections only. `/ws/input-host` advertises
inventory/status and receives explicit start/stop messages. Selected leader input
uses the existing `/ws/control` contract and original Pi-issued windows; each
sample is read after a window arrives. The bridge never calls permission-enable,
never calibrates/configures motors, never opens follower/camera devices, and
never resumes a stopped operation after reconnect. Follower joint bounds, velocity limits and actual input-source freshness remain
authoritative in the production runtime. Leader targets are clipped to those
existing bounds so an out-of-range axis does not reject the entire stream.

The gateway's existing trusted-loopback/Tailscale access middleware applies.
Trusted peers do not need a new login token. TLS is still verified using the
configured CA/system trust store. A legacy `SOARM_STATION_TOKEN`, if explicitly
present, is sent in the Authorization header, never a URL. No token is generated
or demanded by the input-host bridge.

## Profile and launcher reference

The launcher uses `SOARM_STATION_INPUT_PYTHON` when set, then this checkout's
`.venv/bin/python`, then `python3`. The reader supports the selected LeRobot 0.7
`SerialMotorsBus` API and historical 0.6 `FeetechMotorsBus` API through a
version-specific import fallback. Compatibility with a particular device still
needs verification; mocked tests do not establish physical following.

Without `--config`, the bridge advertises actual OS serial devices as
unconfigured. The complete SO-101 profile structure is in
[config/leader-input.example.json](../config/leader-input.example.json).

Match fields are exact `serial`, `port`, `vid` and `pid`. Multiple matching
profiles are an error. Stable serial matching is preferable when unique;
otherwise use the checked physical path. Relative calibration paths resolve
against the host configuration file. The five body values are read using the
degree normalization and explicitly converted to ROS radians; the separate jaw
value remains percent. The mapping must exactly match the selected follower's
joint names. Calibration is compared, never rewritten.

The sampler runs in one child. A native connection/driver error or Stop
terminates that child if necessary, releasing its OS handles. It shares the
existing physical-device lock naming convention. An occupied device, invalid
calibration or mismatch stops the requested run rather than opening a second
owner or invoking a calibration wizard.

## Gateway integration hooks

The only shared-file work is performed by the dashboard integrator:

```python
from soarm_station.leader_gateway import LeaderHostRegistry

registry = LeaderHostRegistry(
    state_provider=lambda: {
        "arms": dict(dashboard.states),
        "resources": dashboard.public_resources,
    },
    hold_callback=hold_matching_run,
    start_state_provider=read_current_native_state,
)
registry.register_routes(app)
```

`start_state_provider()` may be async. Station supplies a fresh native-controller
snapshot for each Start, with feedback age including the IPC round trip. This
avoids rejecting a successful permission grant while waiting for the next ROS
status publication. The endpoint rechecks host/device/run state after that read
before reserving a leader run. The native operation admission still validates
permission and ownership when the actual control stream opens.

`state_provider()` supplies the cached ROS view for cleanup, including `arms` and
`resources.arms[].joints`. `hold_matching_run(arm_id,
expected_controller_session, operator_session_id, expected_operation_id)` is async or synchronous and
returns `{"success": bool}`; it must retain the supplied resource/session scope
and use the runtime's atomic `hold_owned` check. An absent expected operation ID
may stop only when there is no active operation and the principal/session still
match. It must
not hold an unrelated controller or issue a permission-enable request. The
registry independently refuses mismatched controller/operation identities.
Mount under the existing access middleware. Startup/shutdown hooks are included;
no additional listener or ROS node is created.

| Method/path | Contract |
| --- | --- |
| GET `/api/input-hosts` | `hosts[]` has `id,label,platform,connected,state,reason,devices[],active,latest_pose,sample_age_ms,report_age_ms,measured_hz`. No placeholder host/device is added. |
| POST `/api/input-hosts/{id}/start` | Explicit `device_id,arm_id,operator_session_id,runtime_boot_id,controller_session,hz` (50–100); optional boolean `follow_gripper` (default true), `gripper_reference` in percent, and `pickup` (default false; the dashboard sends true). Pickup captures an authoritative native held/measured reference, never a browser-supplied pose. Requires existing permission/fresh follower feedback. Returns `accepted,run_id,state=STARTING,follow_gripper,heartbeat_interval_ms,browser_lease_ms`. |
| POST `/api/input-hosts/{id}/heartbeat` | `run_id,operator_session_id`, Status check for this run; it does not renew or expire control. A late request returns `accepted:false, code:RUN_ENDED` and the real terminal reason without affecting another run. |
| POST `/api/input-hosts/{id}/settings` | Matching `run_id,operator_session_id,follow_gripper` boolean. Forwards a same-run setting change; does not enable permission or renew liveness. |
| POST `/api/input-hosts/{id}/stop` | Same matching run/session fields. Returns `stopped,hold_confirmed,state,reason`; wrong or absent ownership never issues a global hold. |
| WS `/ws/input-host` | Native bridge hello/inventory/state/stopped; gateway start/stop/refresh. This is not a browser USB API. |

Each device entry contains `id,label,configured,reason,port,serial,joint_names`
and available VID/PID/profile metadata. `configured=true` means a matching local
profile/calibration file is structurally usable; it does not claim that motor
identity/calibration has already been verified. Runtime states distinguish
STARTING, READY, ALIGNING, RUNNING, STOPPING, ERROR and OFFLINE. Pose/rate fields are
observed bridge reports, not fictional defaults; null means no sample.

The native input stream owns the run lifetime. There is no browser-heartbeat
expiry; `browser_lease_ms` is null. A delayed UI poll does not stop a healthy
leader stream. Use **Stop / Esc**, disable control or disconnect the input host
to end following. Dashboard disconnect/page exit sends a Hold request using
fetch keepalive; it is a request, not an independently guaranteed crash detector.
If the browser disappears without delivering that request, the native stream
can continue while fresh leader input arrives. Source/control-socket loss still
stops the operation, and reconnect only advertises again.

The **Home** shortcut stops the matching leader run and waits for `stopped`
and `hold_confirmed` before starting the Home operation. Unlock retires only the panel-owned Home stream, confirms its hold and obtains
a fresh permission grant before opening the next leader run. Pickup waits for
alignment with the newly captured held pose before sending movement commands.
A delayed sample that misses its Pi-issued window is discarded and freshly read
rather than causing an extra 50-ms bridge abort. Native feedback/source-loss
handling remains in the controller. Result messages preserve the native terminal
reason so a late UI status request does not mask it with a heartbeat mismatch.
The separate default 100-ms sampler IPC deadline is also removed. A pending
read ends with its control stream or explicit Stop; native driver errors still
propagate, and the controller still stops advancement on actual source loss.

The **Gripper follow** toggle (UI shortcut **O**) changes only the jaw command
source. Disabled follow retains the last successfully sent jaw reference while
body poses continue from fresh leader samples. Before the first sent command,
use a valid supplied `gripper_reference`, otherwise the current follower
`gripper_percent` from control feedback. Missing feedback never becomes a
fabricated 50% or 0% target. Enabling follow resumes fresh calibrated leader jaw
samples through the existing runtime motion bounds and velocity limits. It does
not reset the operation or change coordinates. `hosts[].active.follow_gripper`
is the desired setting; `active.gripper_reference` reports the most recent
observed sent jaw target. `latest_pose` remains the real leader pose, even when
its jaw is not being followed.

## Verification

Tests inject OS inventory, calibration files, fake motor buses, mocked WebSockets
and fake samplers. They open no serial hardware and perform no physical motion.
Check real serial identity, existing leader calibration, source latency, initial convergence,
stop/disconnect and rate behavior in a separately authorized hardware session.
Inventory availability, sampling, follower admission and physical following are
distinct milestones.


## Continuous teleoperation lifetime — November 16, 2026

The bridge and command-line operator now explicitly open a `persistent_target`
DIRECT stream, like keyboard and gamepad control. Input gaps, browser polling
and alignment have no session expiry. A control-loop delay or stale measured
feedback pauses dispatch without discarding the run; fresh feedback resumes the
same operation. Profile stream-duration/tracking timers do not terminate this
teleoperation contract. A caller-specified operation duration is still honored.

Pi-issued packet windows still reject delayed/replayed samples; a rejected old
sample does not end the session. New samples use fresh windows. Explicit Freeze,
control-socket closure, host disconnect and actual hardware faults still stop
control. Reconnection advertises the input host and does not automatically resume.

A joint reference displaced outside its tracking band recovers inward at the
configured velocity while other joints continue. This software tracking state
is distinct from a reported servo protection fault. Status shows both per motor,
including the reported reason and target-versus-measured error.
