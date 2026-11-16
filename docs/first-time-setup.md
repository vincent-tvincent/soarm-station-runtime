# First-time setup after LeRobot arm configuration

Station reuses your existing LeRobot calibration. Its configuration binds each
device to a host, calibration file, robot model and dashboard identity. You do
not need to recalibrate simply to use Station.

This guide uses the working setup as its example: one Pi-connected SO-101
follower, three cameras and a Mac-connected leader. The templates retain its
model/settings while replacing personal device identifiers with placeholders.

## 1. Keep the two calibration files and choose the hosts

Complete both motor setup **and calibration** with LeRobot. Setting motor IDs
and baud rates alone is not calibration. Keep the JSON generated for each arm;
the path printed by LeRobot or its `calibration_dir` setting identifies it.
The normal calibration cache has `robots/<type>/<id>.json` for followers and
`teleoperators/<type>/<id>.json` for leaders; names vary by version.

| Component in the working example | Connected to | Configuration |
| --- | --- | --- |
| SO-101 follower, `arm1` | Pi USB | `local/robot.yaml` and follower calibration |
| `forward`, `overview`, `wrist` cameras | Pi USB | Camera entries in `robot.yaml` |
| SO-101 leader | Mac USB | Native input-host profile and leader calibration |
| Browser | Any computer reaching Station | Opens the Pi's dashboard; no ROS installation required |

The leader does not need a direct USB connection to the Pi. If both arms are
attached to one Linux host, run the input bridge there instead, using the
leader's separate port and calibration. Stop any official teleoperation or
recording program still using those ports before Station opens them.

## 2. Install Station on the follower host

The tested host is Ubuntu 26.04 aarch64 with ROS 2 Lyrical. Install ROS, SROS2,
colcon, a compiler/CMake and Python venv support first; LeRobot's arm setup does
not install these. The bootstrap installs Python dependencies and builds the
ROS packages. See [startup prerequisites](startup-and-debug.md#install-the-development-environment).

On the Pi:

```bash
git clone https://github.com/vincent-tvincent/soarm-station-runtime.git
cd soarm-station-runtime
export ROS_DISTRO=lyrical
scripts/bootstrap
```

Generate local TLS/access and DDS credentials. Replace `YOUR_STATION_HOSTNAME`
with the hostname you will actually open in the browser; `localhost` is also
included automatically:

```bash
.venv/bin/python scripts/init-local --name YOUR_STATION_HOSTNAME
scripts/init-security
```

Keep generated credentials local. Bootstrap creates `.venv` for ROS and a
separate Python 3.12 `.runtime-venv` for LeRobot; normal launch selects both.

## 3. Bind the follower, cameras and storage

Copy the physical-device template and **your follower's** calibration:

```bash
mkdir -p local
cp -n config/so101-station.example.yaml local/robot.yaml
cp -n /path/to/your/follower-calibration.json local/follower-calibration.json
ls -l /dev/serial/by-id/
ls -l /dev/v4l/by-id/
ls -l /dev/v4l/by-path/
```

`cp -n` preserves existing destination files. Edit `local/robot.yaml`:

| Field | What to set |
| --- | --- |
| `device_id` | Your station name; the working Pi uses `ubuntu-pi`. |
| `arms[0].id` | Dashboard/API resource ID, `arm1`; not a USB serial number. |
| `arms[0].backend` | `so101` for this physical arm. `mock` is simulated. |
| `arms[0].port` | Your follower's actual `/dev/serial/by-id/...` path. |
| `arms[0].calibration` | `follower-calibration.json`, resolved beside `robot.yaml`. |
| `arms[0].read_only` | `false` for the control-capable adapter. Launch does not start an operation. |
| Camera `device` | The actual capture path for that camera. |
| Camera `id` / `label` | Stable binding ID and human-readable dashboard name. |
| `database` | Telemetry path; the template uses `local/telemetry.sqlite3`. |

The template already includes the working five joint names, radian limits,
speed settings, gripper percentage convention, bundled URDF and Home/Fold
examples. **Model/motion settings are separate from motor calibration.** These
values reproduce the configured station; adapt them if your model, assembly,
operating range or desired poses differ. Use each arm's own calibration JSON.

The working cameras request 640×480 at 30 FPS. Actual rate depends on exposure,
lighting and USB setup. Prefer unique `/dev/v4l/by-id` identities; our setup uses
`by-path` bindings to fixed USB connectors. `/dev/videoN` numbering can change.
Remove unused camera entries or use `cameras: []` for joint/leader control without
video. Camera intrinsic/extrinsic calibration is separate; this example does
not invent those measurements. Camera labels do not rename model tensor inputs.

The relative file paths resolve to this layout:

```text
soarm-station-runtime/
├── models/so101/so101.urdf
└── local/
    ├── robot.yaml
    ├── follower-calibration.json
    ├── telemetry.sqlite3
    └── tls/
        ├── cert.pem
        └── key.pem
```

Our working Pi uses an already-mounted SSD. To match that layout, change these
fields while retaining the other monitoring settings:

```yaml
database: /mnt/ssd/soarm-station/telemetry.sqlite3
monitoring:
  storage_mount: /mnt/ssd
  period_s: 10
  retention_days: 30
  storage_limit_mb: 512
```

This does not format or mount a disk. Without an SSD, keep the template's local
database path and omit `storage_mount`.

## 4. Start Station

From the Pi checkout:

```bash
source /opt/ros/lyrical/setup.bash
source ros2_ws/install/setup.bash
ros2 launch soarm_station_system system.launch.py \
  config:="$PWD/local/robot.yaml"
```

Open `https://YOUR_STATION_HOSTNAME:8443/`, trusting the configured certificate.
Our working deployment uses its Tailscale hostname. Automatic access applies to
actual loopback/Tailscale peers; ordinary LAN clients use the configured token
flow. See [access and discovery](web-api.md#connection-access-and-discovery).

The dashboard should show `arm1`, measured motor/joint status and configured
cameras. Joint, keyboard IK and gamepad control work independently of a leader.
Start an operation explicitly when ready. The first DIRECT target converges
from the current measured/held reference under the configured motion limits.

For background service operation, stop the foreground launch first, then run:

```bash
scripts/install-service "$PWD/local/robot.yaml"
```

The service and foreground launch run the same application. Run one hardware
owner for each device set.

## 5. Configure the leader on its USB host

On the Mac, clone the same repository. The input bridge does not require ROS.
Use the LeRobot environment that already works with the leader; it also needs
`aiohttp` and `pyserial`.

```bash
git clone https://github.com/vincent-tvincent/soarm-station-runtime.git
cd soarm-station-runtime
export SOARM_STATION_INPUT_PYTHON=/path/to/your/lerobot-venv/bin/python
mkdir -p local/leader-input/calibration
cp -n /path/to/your/leader-calibration.json \
  local/leader-input/calibration/leader.json
cp -n config/leader-input.example.json local/leader-input/input-host.json
scripts/run-leader-input --list
```

Edit `local/leader-input/input-host.json` with the serial from inventory. Our
configured structure is:

```json
{
  "host_id": "operator-mac",
  "label": "Operator Mac",
  "leaders": [{
    "id": "waveshare_leader",
    "label": "SO-101 leader",
    "match": {"serial": "YOUR_LEADER_USB_SERIAL"},
    "calibration": "calibration/leader.json",
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

`waveshare_leader` is a chosen profile ID. `match.serial` is the actual USB
controller serial. The calibration path is relative to this JSON on the Mac,
not the Pi's filesystem. Copying calibration alone does not create the binding.

Copy the Pi's public TLS certificate using your SSH user and checkout path:

```bash
scp user@YOUR_STATION_HOSTNAME:/path/to/soarm-station-runtime/local/tls/cert.pem \
  local/leader-input/station-ca.pem

scripts/run-leader-input \
  --config local/leader-input/input-host.json \
  --url https://YOUR_STATION_HOSTNAME:8443 \
  --ca local/leader-input/station-ca.pem
```

The bridge advertises devices without starting following. Keep it running and
restart it after editing its profile; dashboard Refresh does not reload the
local JSON. The current setup does not install an automatically starting Mac
companion. See [the leader guide](leader-input.md#reuse-an-existing-leader-calibration)
for complete field and connection details.

## 6. Use the configured system

In **Control → Leader arm**, select **Operator Mac → SO-101 leader** and press
**Unlock / follow**. The bridge verifies existing motor calibration. The follower
stays frozen while the live leader overlay updates: align the leader with the
held follower pose to begin following. **Freeze / Esc** holds the follower.
After Freeze or Home, unlock and align again; there is no alignment countdown.
The gripper participates in pickup only when **Gripper follow** is enabled.
This is the native bridge path; browser virtual-serial forwarding is not implemented.

For ACT/VLA deployment, use Policies to select a model execution profile, its
local/remote execution target and camera bindings. See [policy management](policy-management.md).
Models are optional for manual control and monitoring. Episode recording and
training remain in the separate training workflow.

## What needs configuration after official LeRobot setup?

| Item | Reuse or configure? |
| --- | --- |
| Motor IDs, baud, homing offsets and calibrated ranges | Reuse LeRobot setup and each arm's own calibration. |
| Host and USB bindings | Configure in the two Station/input-host profiles. |
| Camera devices and names | Configure on the follower host. |
| Model, software motion limits and named poses | Use/review the SO-101 example; importing calibration does not create these. |
| Dashboard hostname/certificate and storage | Configure for this deployment. |
| Weights and camera tensor bindings | Configure when deploying a trained policy. |

Personal files stay under Git-ignored `local/`. Public examples contain no
personal calibration values or private credentials. Schema/model checks do
not establish a physical validation on a different arm.
