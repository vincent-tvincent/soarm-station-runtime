# Reference Pi deployment

This document describes one installation. Its private profiles and credentials are not included in the repository. General setup starts with the [developer startup guide](../startup-and-debug.md).

## Deployment snapshot — November 13, 2026

| Item | Value |
| --- | --- |
| Host | `ubuntu-pi.tail521c60.ts.net` |
| SSH account | `pipi` |
| Runtime checkout | `/home/pipi/so-101/ros_system` |
| Training checkout | `/home/pipi/so-101/training` |
| Dashboard | `https://ubuntu-pi.tail521c60.ts.net:8443/` |
| ROS/native environments | Python 3.14 ROS Lyrical / Python 3.12 LeRobot 0.7.0 |
| Writable profile | `local/pi-writable.yaml` |
| Read-only fallback | `local/pi.yaml` |
| Service | User unit `soarm.service`; enabled, with user lingering configured. |

Private operator credentials, HTTPS keys, DDS credentials and calibration stay under ignored local paths. The certificate is `local/tls/cert.pem`; operator/collector tokens are in `local/secrets.env`.

## Foreground debugging on this installation

After connecting to the host, enter Bash and run:

```bash
cd /home/pipi/so-101/ros_system
systemctl --user stop soarm.service
source /opt/ros/lyrical/setup.bash
source ros2_ws/install/setup.bash
ros2 launch soarm_system system.launch.py \
  config:="$PWD/local/pi-writable.yaml" log_level:=debug respawn:=false
```

Press Ctrl-C and wait for shutdown before returning to the service:

```bash
systemctl --user start soarm.service
systemctl --user status soarm.service
```

## Validation snapshot

The foreground check reported approximately 100 Hz feedback, three camera feeds, motion permission OFF, torque ON, zero startup goal writes, 12.0 V and a maximum motor temperature of 34 C. The service was restored after testing. These are dated observations, not a live status endpoint.

Camera acquisition has measured about 14–18 FPS despite nominal 30-FPS requests. The qualified elbow profile uses a 0.07-rad completion tolerance because unchanged stock gains leave about 3.2 degrees of following error. These measurements apply to this installation, not every SO-101 deployment.

No production learned-policy profile or external collector endpoint was registered at this snapshot. Physical task-level VLA performance remains unqualified. See [physical motion validation](../physical-motion-validation-2026-11-12.md), [foreground validation](../foreground-validation-2026-11-13.md) and [clean-checkout evidence](../reproducibility-2026-11-13.md).
