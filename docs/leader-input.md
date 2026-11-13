# Native leader input hosts

This bridge makes the Manual Leader host/device dropdown reflect actual OS serial
devices on a connected Mac or Linux input host. The browser does not discover or
open USB on its own. An unconfigured serial adapter is visible but cannot be
started; a configured candidate is checked against live motor calibration only
after the operator presses **Start**. No leader was opened while implementing or
testing this feature.

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
never resumes a stopped operation after reconnect. Follower pickup, gripper
clutch, bounds and expiry remain authoritative in the production runtime.

The gateway's existing trusted-loopback/Tailscale access middleware applies.
Trusted peers do not need a new login token. TLS is still verified using the
configured CA/system trust store. A legacy `SOARM_STATION_TOKEN`, if explicitly
present, is sent in the Authorization header, never a URL. No token is generated
or demanded by the input-host bridge.

## Host installation and launch

Use the input host's compatible LeRobot environment with `aiohttp`, `pyserial`,
and PyYAML when using YAML configuration. The robot bus reader supports the
selected LeRobot 0.7 `SerialMotorsBus` API and the historical 0.6
`FeetechMotorsBus` API through a version-specific import fallback. Hardware
compatibility still needs a supervised check; passing mocked tests is not that
check. No ROS installation is needed on the input host.

The launcher uses `SOARM_STATION_INPUT_PYTHON` when set, then this checkout's
`.venv/bin/python`, then `python3`. Run from a checkout containing these files:

```bash
# Enumerate only: no device opening, calibration or network connection.
SOARM_STATION_INPUT_PYTHON=/path/to/compatible/venv/bin/python \
  scripts/run-leader-input --config /path/to/input-host.json --list

# Advertise to the existing gateway. This does not start sampling or arm anything.
SOARM_STATION_INPUT_PYTHON=/path/to/compatible/venv/bin/python \
  scripts/run-leader-input --config /path/to/input-host.json \
  --url https://your-gateway-tailnet-name:8443 --ca /path/to/gateway-ca.pem
```

Without `--config`, the bridge advertises actual serial devices as unconfigured.
Do not substitute a guessed port or calibration. Create an explicit local
profile after identifying the leader; the following is a **schema example with
placeholders**, not a fabricated discovered device:

```json
{
  "host_id": "operator-mac",
  "label": "Operator Mac",
  "leaders": [{
    "id": "leader-profile",
    "label": "Calibrated leader",
    "match": {"serial": "ACTUAL_USB_SERIAL"},
    "calibration": "/actual/path/to/leader-calibration.json",
    "motor_model": "sts3215",
    "joint_map": {
      "shoulder_pan": "shoulder_pan",
      "shoulder_lift": "shoulder_lift",
      "elbow_flex": "elbow_flex",
      "wrist_flex": "wrist_flex",
      "wrist_roll": "wrist_roll"
    },
    "gripper_source": "gripper"
  }]
}
```

Match fields are exact `serial`, `port`, `vid` and `pid`. Multiple matching
profiles are an error. Stable serial matching is preferable when unique;
otherwise use the checked physical path. Relative calibration paths resolve
against the host configuration file. The five body values are read using the
degree normalization and explicitly converted to ROS radians; the separate jaw
value remains percent. The mapping must exactly match the selected follower's
joint names. Calibration is compared, never rewritten.

The sampler runs in one bounded child. A native connect/read timeout or Stop
terminates that child if necessary, releasing its OS handles. It shares the
existing physical-device lock naming convention. An occupied device, invalid
calibration or mismatch stops the requested run rather than opening a second
owner or invoking a calibration wizard.

## Gateway integration hooks

The only shared-file work is performed by the dashboard integrator:

```python
from soarm_station.leader_gateway import LeaderHostRegistry

registry = LeaderHostRegistry(
    state_provider=dashboard.state,
    hold_callback=hold_matching_run,
)
registry.register_routes(app)
```

`state_provider()` returns the current status including `arms` and
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
| POST `/api/input-hosts/{id}/start` | Explicit `device_id,arm_id,operator_session_id,runtime_boot_id,controller_session,hz` (50–100); optional boolean `follow_gripper` (default true) and `gripper_reference` in percent. Requires existing permission/fresh follower feedback. Returns `accepted,run_id,state=STARTING,follow_gripper,heartbeat_interval_ms,browser_lease_ms`. |
| POST `/api/input-hosts/{id}/heartbeat` | `run_id,operator_session_id`, about every 500 ms while STARTING/READY/PICKUP/RUNNING. An expired lease cannot be revived. |
| POST `/api/input-hosts/{id}/settings` | Matching `run_id,operator_session_id,follow_gripper` boolean. Forwards a same-run setting change; does not enable permission or renew liveness. |
| POST `/api/input-hosts/{id}/stop` | Same matching run/session fields. Returns `stopped,hold_confirmed,state,reason`; wrong or absent ownership never issues a global hold. |
| WS `/ws/input-host` | Native bridge hello/inventory/state/stopped; gateway start/stop/refresh. This is not a browser USB API. |

Each device entry contains `id,label,configured,reason,port,serial,joint_names`
and available VID/PID/profile metadata. `configured=true` means a matching local
profile/calibration file is structurally usable; it does not claim that motor
identity/calibration has already been verified. Runtime states distinguish
STARTING, READY, PICKUP, RUNNING, STOPPING, ERROR and OFFLINE. Pose/rate fields are
observed bridge reports, not fictional defaults; null means no sample.

The browser must heartbeat during startup as well as active input. Its 2.5-second
lease prevents an unattended gateway timer from keeping a closed control page
alive. Source/client loss causes a scoped stop/hold; reconnect only advertises
again. The **Home** shortcut first stops the matching leader run and waits for
both `stopped` and `hold_confirmed`; only then may the UI enable a new manual/Home
operation. Never implement Home by jumping the leader coordinate offset.

The **Gripper follow** toggle (UI shortcut **O**) changes only the jaw command
source. Disabled follow retains the last successfully sent jaw reference while
body poses continue from fresh leader samples. Before the first sent command,
use a valid supplied `gripper_reference`, otherwise the current follower
`gripper_percent` from control feedback. Missing feedback never becomes a
fabricated 50% or 0% target. Enabling follow resumes fresh calibrated leader jaw
samples through the existing runtime pickup/clutch and motion bounds. It does
not reset the operation or change coordinates. `hosts[].active.follow_gripper`
is the desired setting; `active.gripper_reference` reports the most recent
observed sent jaw target. `latest_pose` remains the real leader pose, even when
its jaw is not being followed.

## Verification

Tests inject OS inventory, calibration files, fake motor buses, mocked WebSockets
and fake samplers. They open no serial hardware and perform no physical motion.
Check real serial identity, existing leader calibration, source latency, pickup,
stop/disconnect and rate behavior in a separately authorized hardware session.
Inventory availability, sampling, follower admission and physical following are
distinct milestones.
