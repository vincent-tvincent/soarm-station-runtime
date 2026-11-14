# GitHub source and Pi deployment

The runtime repository is `vincent-tvincent/soarm-station-runtime`. It is public.
The training repository remains separate. Git contains source, example
configuration, tests and documentation; each installation owns its ignored
`local/` configuration, credentials, calibration, models and runtime data.

## Development

Clone the public repository, make source changes, run the checks
appropriate to the change, and push the reviewed revision. Use a feature branch
for concurrent development. Do not push credentials, model weights or datasets.
See the README for mock startup and the separate ROS/native Python environments.

## Update the Pi

The existing runtime checkout is `/home/pipi/so-101/ros_system`. Its `origin`
points to this GitHub repository over HTTPS. Public fetches require no GitHub
credentials on the Pi. Publishing changes still requires an authorized writer.
Git fetch does not restart services or grant motion permission.

```bash
cd /home/pipi/so-101/ros_system
git status --short
git fetch origin
git log --oneline HEAD..origin/main
git diff --stat HEAD..origin/main
```

Resolve local source changes before updating; retain all ignored installation
files. Inspect the change's dependency and migration notes. Do not replace an
active operation as a deployment shortcut. When every affected arm is idle and
motion permission is off, activate an approved revision:

```bash
systemctl --user stop soarm-station.service
git merge --ff-only origin/main
# Install a documented dependency delta or rebuild affected ROS packages if needed.
systemctl --user start soarm-station.service
systemctl --user is-active soarm-station.service
```

Check service logs, fresh arm feedback, torque/permission/operation state,
cameras and the dashboard after activation. Hardware shutdown preserves support
torque. A source pull is not proof of successful deployment or physical policy
performance. Pure static web-asset changes can be served without a robot-service
restart; verify the browser loaded the intended asset revision.

There is no automatic GitHub-triggered deployment or motion. See
[startup and debugging](startup-and-debug.md) for foreground launch, rebuilds and
service troubleshooting.
