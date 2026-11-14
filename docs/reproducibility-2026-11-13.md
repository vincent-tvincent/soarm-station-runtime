# Clean-checkout reproducibility check — November 13, 2026

This dated report uses the package/interface names of its tested revision. For current SO-Arm Station commands, see [startup](startup-and-debug.md) and [name migration](name-migration.md).

Verified source commits: runtime `b9a10c9e3d9586f589801c74a30ca608ba6adcd7`; training `5d73b04d162d1929689a6e58779fa36af2d83474`. Both were cloned from local Git into new disk-backed `/var/tmp` directories with `git clone --local --no-hardlinks`. Ignored deployment files, environments, datasets, credentials and build outputs were not copied. Temporary clone remotes were removed. No repository was pushed.

## Wrapper locations

- `scripts/run`: foreground startup wrapper. Finds the repository root from its own path, selects a deployment YAML, sets the core Python import path and DDS settings, loads host-local credentials, sources ROS/colcon/venv environments and executes `ros2 launch soarm_system system.launch.py`.
- `scripts/install-service` + `deploy/soarm.service.in`: optional user-systemd deployment wrapper. Generates paths for the current checkout/user and starts the same foreground wrapper.
- `ros2_ws/src/soarm_system/launch/system.launch.py`: ROS launch definition. Starts application nodes using the ROS Python environment and optional model/TF/recording components from configuration.
- `ros2_ws/src/soarm_system/soarm_system/runtime_node.py`, `python/soarm/worker.py`, and `python/soarm/ipc.py`: ROS-to-native-process bridge. One logical ROS hardware owner starts the separate pinned LeRobot environment and communicates through bounded local IPC.
- Runtime and training `scripts/bootstrap`: repository-tracked environment/dependency installation, with runtime colcon build.

These wrappers are committed source, not an untracked laptop setup. A subsequent startup revision removes the launch-time dependency on wrapper-supplied variables: after bootstrap/private initialization, source ROS and the colcon overlay, then use ordinary `ros2 launch`. See [foreground startup and its separate verification](startup-and-debug.md). The full checkout, both environments and local credentials remain required; a colcon install tree alone is not a complete deployment.

## Verified clean runtime flow

On the existing Ubuntu26.04.1/aarch64/ROS2 Lyrical host, from a new clone:

```bash
scripts/bootstrap
.venv/bin/python scripts/init-local --name localhost
scripts/init-security
scripts/run config/mock.yaml
```

For this side-by-side test, the tracked mock example was copied to ignored `local/repro.yaml` and given a distinct mock arm/camera ID, local data/socket paths and HTTPS port18443. Fresh certificates, tokens and DDS CA credentials were generated inside the clone. No deployed credentials or calibration were copied. Only authenticated GET requests were made; the mock was never armed and no physical device was opened.

Results:

- New ROS Python3.14.4 `.venv` and native Python3.12 `.runtime-venv` installed successfully from the committed dependency files.
- `soarm_interfaces` and `soarm_system` built from scratch in2min21s.
- Configuration validation and fresh private initialization passed; the credentials file contained the two expected key names on separate lines, without exposing values.
- The clean mock graph started and returned its distinct device/arm/camera identities through authenticated HTTPS.
- Camera endpoint returned a valid JPEG; measured feedback was valid; permission remained false and mock torque OFF. The initial startup-window rate was78.18Hz, not a steady-state performance qualification.
- Native dependency check reported124 installed packages compatible.
- Imported `soarm` resolved to the new clone, not the deployed source or venv.
- Mock service was stopped after verification; clone Git status stayed clean.

## Verified clean training flow

```bash
scripts/bootstrap
.venv/bin/soarm-training --help
.venv/bin/python -m pytest -q tests
```

The new clone created its own Python3.12.16 environment, installed the pinned dependencies and editable package, and exposed the training CLI. Both existing tests passed. Imports reported LeRobot0.7.0 and CPU PyTorch2.11.0+cpu; ACTPolicy, SmolVLAPolicy and dataset APIs imported successfully. `soarm_training` resolved to this fresh clone. No demonstration collection or model fitting was run during this check.

## Scope and remaining portability work

This is a **clean-checkout/new-venv check on the same supported host**, not a clean operating-system installation. The preinstalled ROS distribution, colcon/compiler/system Python packages and existing package-download caches were reused. The native/training venvs were new; the ROS venv intentionally uses distribution site packages. No x86_64, other ROS distribution, other Linux distribution or GPU-install matrix was executed.

Bootstrap assumes required OS/ROS/build packages already exist; it does not provision arbitrary Linux installations. Runtime lock files are the tested Linux CPU set, not a cross-platform artifact/hash lock. Hardware use still requires the new operator's own stable device paths, permissions, calibration, host profile and registered policy assets. Private Pi deployment files are correctly absent from a clone. There is no clean-machine CI matrix yet. The clean-checkout test above used the committed bootstrap/wrapper; direct foreground ROS startup was subsequently verified separately. Conventional standalone ROS-only packaging remains a separate improvement.

The initial attempt placed large test environments in RAM-backed `/tmp` and hit its quota. This was corrected by rerunning in disk-backed `/var/tmp` with a disk-backed TMPDIR. Disposable failed copies were removed; the successful checkouts and generated test credentials were removed after verification. This setup failure is not counted as a successful bootstrap.

The deployed real service stayed active; no physical goals were enabled or issued for these checks. Final cached state was HOLD/permission OFF/torque ON, approximately100Hz feedback, and all three camera feeds were fresh with no current camera error. Production code, hardware profile, calibration and deployed environments were unchanged.
