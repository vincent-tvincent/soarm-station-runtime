# Foreground startup validation — November 13, 2026

Source: runtime commit `73b282d`, tested on the reference Pi installation. See [developer startup instructions](startup-and-debug.md) for general usage and [deployment details](deployments/pi.md) for this host.


Verified on the deployed Pi, separately from earlier physical-motion qualification:

- Rebuilt `soarm_system`; sourced ROS suite: **62 passed, 8 skipped**, plus four passing subtests. Seven launch-environment checks cover root discovery, token parsing/precedence, missing prerequisites and preservation of native-venv interpreter symlinks. A ROS serialization regression covers initial integer-valued native samples.
- Direct launch from a shell with wrapper/private environment variables unset: `--show-args`, mock profile with `log_level:=debug`, and native writable profile with `respawn:=false` all passed after corrections. Full launch used only standard ROS/colcon setup and `ros2 launch`.
- Mock authenticated status and valid camera JPEG passed; permission stayed OFF. Short ROS topic-rate inspection measured approximately 95–96 Hz under debug logging; this is a diagnostic sample, not a performance qualification.
- Native writable foreground run: fresh feedback **99.999 Hz**, all three authenticated camera JPEGs valid, permission OFF, measured torque ON, native health **writes=0**, voltage 12.0 V, maximum temperature 34 C.
- Second-terminal node discovery (five-second discovery window), joint/status echo, action listing, parameter lookup and verbose camera-topic inspection passed. Default short secure discovery can initially return only a subset of nodes.
- Individual `ros2 run --prefix ...` hardware-node and dashboard commands passed together with valid mock feedback; the seven launch-environment tests also passed in native Python 3.12.
- Ctrl-C stopped both full graphs cleanly with no remaining worker/node process or listener on port 8443. No motion/enable command was issued.

Testing found and corrected the ROS launch environment-action import, core/ROS Python path precedence, native-venv symlink handling, and integer-to-float conversion at initial ROS status publication. Obsolete generated metadata from the former `soarm-system` core package was archived under ignored `local/debug-startup-stage/`; current core source/package naming remains `soarm-core`.

This is not a new clean-OS or cross-platform installation qualification. Hardware profiles, calibration, physical limits and motion-permission rules were not changed.

The existing user service was restored after testing. Its native writable profile again reported valid approximately100 Hz feedback, all three camera feeds, permission OFF, torque ON and zero startup goal writes. No remote or GitHub push was performed.
