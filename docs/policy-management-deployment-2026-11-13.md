# Policies workspace deployment — November 13, 2026

Deployed runtime source: `88f1951a4937f8b34873de202cb3efd5fbe76365`, fast-forwarded from the finalized Manual-control revision `9284cd7`. The third top-level **Policies** page is live at `https://ubuntu-pi.tail521c60.ts.net:8443/#policies`.

The deployment added only `ruamel.yaml==0.18.16` to the ROS Python 3.14 environment and that package plus `ruamel.yaml.clib==0.2.15` to native Python 3.12. LeRobot, Torch and ROS were not upgraded. Existing editable/symlink installations required no IDL or colcon rebuild. The service was stopped and restarted once after a fresh idle/no-permission check, activating dashboard and native preparation support together.

The physical startup YAML, calibration, credentials and Manual Home configuration were preserved. The default model source is `local/models`, with saved profiles under `local/policy-profiles`. Both are initially empty; no synthetic demonstration profile was installed on the robot. Source paths remain configurable in `config/model-sources.yaml` and reloadable through Sources.

## Verification

- 150 final frontend tests passed, including editor races, revision conflicts, exact profile/revision/arm preparation selection, delayed preparation outcomes, transport and existing Manual behavior.
- The merged backend passed 323 ROS-side Python tests plus 118 subtests; eight native-only checks skipped there passed separately in the native environment. A later resource-list follow-up passed four focused dashboard tests. Native synthetic ACT preparation loaded weights/processors while paused; it performed no inference or motor connection.
- Real temporary-backend browser checks covered CRUD, raw/form editing, external-file conflict handling, navigation Save/Discard/Cancel, keyboard save and an approximately 4.2 MB checkpoint-folder upload. See [visual QA](policy-management-design-qa.md).
- All six checked status/resource/management metadata endpoints, new JavaScript/CSS/YAML assets and the existing Manual module returned successfully over HTTPS. The live browser showed Connected and the configured project source online, with no console errors.
- After activation: IDLE, permission OFF, no active operation, torque ON, 100.0 Hz feedback, zero startup goal writes and three fresh JPEG camera feeds. Maximum measured joint change from the pre-deployment sample was **0.0 rad**. Physical YAML and calibration hashes were unchanged.

No production model was prepared or started. Remote-server connectivity and physical learned-policy task performance remain separate from this UI/API deployment verification. Existing policy preparation never implicitly enables motion.
