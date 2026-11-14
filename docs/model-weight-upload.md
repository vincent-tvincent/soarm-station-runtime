# Uploading and using model weights

Use this guide to add an exported LeRobot checkpoint to SOArm Station, create a saved execution profile, and prepare it for an explicit policy run. The Policies workspace is the third dashboard page after Operations and ROS graph.

The normal sequence is **Upload or copy → Refresh/catalog → Create profile → Validate and Save → Apply model → Start policy**. Uploading, saving and applying do not start robot motion. Training and demonstration collection belong to the separate training repository.

## Where the files belong

| Execution choice | Weight/config/processor files | Station profile |
|---|---|---|
| Local inference | A configured model-source folder on the robot/backend host | The backend's single profile directory |
| Remote inference | The inference computer that runs `lerobot-policy-server` | On the robot/backend, referencing the remote endpoint and deployment |

The dashboard's model uploader writes to its own backend host. Selecting a remote inference profile does not forward uploaded weights to that computer. Transfer those weights to the inference host with its ordinary file/download tools, then configure its server as described under [Remote inference](#remote-inference).

A browser-selected file/folder is an upload source on the operator's computer. A configured model-source path is a destination on the backend computer. These are different filesystems when the dashboard is opened remotely.

## Configure model-source folders

The default source file is `config/model-sources.yaml`, relative to the runtime checkout. The backend's startup `policy_management.source_config` setting can select a different file. The supplied source points at the ignored `local/models` directory; create it on the backend host if needed:

```bash
mkdir -p local/models
```

Edit the source file with a host editor, such as VS Code Remote SSH:

```yaml
schema_version: 1
roots:
  - id: project
    label: Project policies
    path: ../local/models
    enabled: true
    allow_upload: true
  - id: ssd
    label: SSD policies
    path: /mnt/ssd/models
    enabled: true
    allow_upload: true
```

Relative paths resolve against the source file's directory. Each `id` is a stable name used by saved profiles; the label is for display. A destination is offered for upload only when it is enabled, online, writable by the backend, and permits uploads.

- **Changed the source configuration?** Open Policies → Sources and click **Reload source config**. A successful reload adopts the complete new list and starts a scan of changed sources. Invalid configuration leaves the last loaded revision active. The robot service does not need to restart.
- **Copied files into an existing source?** Open Model library and click **Refresh models**. This scans the currently loaded folders; it does not reread the source file.
- **Edited profile files externally?** Click **Refresh profiles**. Profiles live by default in `local/policy-profiles/<profile-id>.yaml`; their location is startup-owned and is not changed by source reload.

The GUI provides a catalog, grouped by source folder, with search across all groups. It does not provide a remote filesystem browser, rename/move/delete tools, or a source-folder editor. See [management configuration](policy-management.md#startup-owned-locations) for startup path overrides.

## Prepare a complete checkpoint

Upload an exported checkpoint package, not a training output directory full of unrelated runs. A useful layout is:

```text
cup-policy/
├── station-policy.yaml                 # explicit Station registration, if needed
└── pretrained_model/
    ├── config.json
    ├── model.safetensors
    ├── policy_preprocessor.json
    ├── policy_postprocessor.json
    └── other files referenced by those processors
```

A flat package with these checkpoint files directly at its root is also recognized. `station-policy.yaml` belongs at the artifact root: beside `pretrained_model/` in the layout above, or beside `config.json` in a flat package. Select the outer `cup-policy` folder when uploading so the sidecar is included.

The catalog requires:

- A valid checkpoint `config.json`, including its model `type`.
- Nonempty `model.safetensors` or `pytorch_model.bin`; supported sharded exports instead need their weight index and every referenced shard.
- A saved processor pair: `policy_preprocessor.json` / `policy_postprocessor.json`, or the recognized `preprocessor.json` / `postprocessor.json` pair.
- Every state/statistics/companion file referenced by those processor descriptions. Preserve the exported relative paths.
- Enough trained metadata to construct the intended robot mapping before preparation.

Station export bundles with a schema-version-1, LeRobot-0.7.0 `manifest.json` and file digests are also recognized. Prefer the training repository's exporter when available. Do not change a bundle's declared hashes or metadata merely to silence a compatibility error.

Use real files in the package. Symlinks, device files and other special files are not accepted as model artifacts; materialize a symlink-based model-cache snapshot into an ordinary export directory first. ZIPs should contain the package directly or one enclosing package directory, without unrelated sibling directories. The server can unwrap a single enclosing directory; it cannot infer which of several unrelated directories is the model.

A lone `.pt`, `.pth`, ONNX file or arbitrary state dictionary is not a complete LeRobot export. Renaming a file to `model.safetensors` does not convert its contents or supply architecture and processor code.

### Declare trained state, action and camera meaning

The checkpoint may already carry this information. If it does not, add a `station-policy.yaml` sidecar, or supply missing declarations through a profile's advanced `contract` fields. Known checkpoint declarations remain authoritative; registration cannot override their shape, units, cadence or meaning.

This sidecar is an **illustrative six-channel SO-101 contract**, assuming that the actual exported checkpoint uses the listed feature order, two `[3,480,640]` image inputs and 30-Hz actions. Replace these values with the contract used during training. In particular, `calibration_revision` must match the trained calibration reference and the target arm's configured reference.

```yaml
schema_version: 1
name: Cup policy
contract:
  action_fps: 30
  language_conditioned: false
  calibration_revision: REPLACE_WITH_TRAINED_CALIBRATION_REVISION
  state_features:
    - {name: arm1/shoulder_pan.pos, joint: shoulder_pan, unit: degree}
    - {name: arm1/shoulder_lift.pos, joint: shoulder_lift, unit: degree}
    - {name: arm1/elbow_flex.pos, joint: elbow_flex, unit: degree}
    - {name: arm1/wrist_flex.pos, joint: wrist_flex, unit: degree}
    - {name: arm1/wrist_roll.pos, joint: wrist_roll, unit: degree}
    - {name: arm1/gripper.pos, joint: gripper, unit: percent}
  action_features:
    - {name: arm1/shoulder_pan.pos, joint: shoulder_pan, unit: degree}
    - {name: arm1/shoulder_lift.pos, joint: shoulder_lift, unit: degree}
    - {name: arm1/elbow_flex.pos, joint: elbow_flex, unit: degree}
    - {name: arm1/wrist_flex.pos, joint: wrist_flex, unit: degree}
    - {name: arm1/wrist_roll.pos, joint: wrist_roll, unit: degree}
    - {name: arm1/gripper.pos, joint: gripper, unit: percent}
  camera_inputs:
    - key: observation.images.front
      role: Scene view
      shape: [3, 480, 640]
      masked: false
    - key: observation.images.wrist
      role: Tool view
      shape: [3, 480, 640]
      masked: false
```

`name` in each feature declaration identifies the trained vector component; `joint` binds it to the configured arm's logical joint. Preserve the trained ordering. The current arm adapter requires every body joint and the gripper exactly once in both state and action mappings. Body units are `degree` or `radian`; the gripper uses `percent`. The declared action cadence is separate from the motor-dispatch loop's frequency.

Camera keys identify trained inputs. A profile maps them to configured camera IDs; it does not change the learned viewpoint, calibration or action space. The SO-ARM SmolVLA editor may label roles Global/Wrist/Additional, but the selected checkpoint determines the actual keys. π0.5 and other layouts use checkpoint-defined rows. Required images cannot be made optional by leaving a camera unplugged. Current native masked-camera handling is qualified for explicit SmolVLA and π0.5 empty/masked slots; those slots remain unbound to real cameras and need at least one real view.

Make metadata changes before selecting a content revision. Adding or editing a sidecar changes the artifact identity: refresh the catalog, select the new revision and save the profile again.

## Upload through the GUI

1. Open **Policies → Uploads**.
2. Select **Checkpoint folder** or **ZIP archive**. Choose the exported package on your computer.
3. Choose one configured writable destination source. The GUI does not accept an arbitrary remote destination path.
4. Enter a **new model directory name**, such as `cup-policy-v2`. Existing directories are never overwritten or silently merged.
5. Click **Upload model**. The browser reads bounded slices, so a multi-gigabyte checkpoint is not buffered in browser RAM.
6. Follow Upload activity through transfer and verification. Only after publication and catalog indexing should the model become selectable.

Switching dashboard views does not stop the sender. Pause stops the browser transfer while retaining verified server chunks. After a reload or interrupted connection, reselect the original package and resume the job. The client compares recorded chunk hashes with those files before sending new bytes; matching names and sizes alone are insufficient. Cancellation removes only the selected upload's staging, not an existing model directory.

The server checks declared sizes, chunk/file integrity, complete checkpoint companions, archive containment and destination eligibility. Files remain under a scan-excluded `.station-uploads/` area until an atomic no-overwrite publication. A copied/uploaded artifact may still be incomplete, incompatible or require an unverified implementation. **Transfer complete**, **catalog selectable**, and **native prepared** are distinct states.

The browser exposes folder/ZIP upload. The underlying API additionally accepts TAR/TGZ archives. Current server bounds include 128-KiB chunks, 4,096 uploaded files, 8,192 archive entries and a 256-GiB package limit; available disk space and the HTTP manifest-size limit can reject a package earlier. These are ceilings, not recommended model sizes or available-memory guarantees.

## Copy through SSH or another host tool

You can copy a package directly into a configured source without the upload API. For the default source, the result might be `local/models/cup-policy-v2/` with the package layout above. Finish the copy before refreshing, then click **Refresh models**.

Incomplete or changing copies stay unavailable. For a repeatable workflow, copy to a temporary sibling location excluded from discovery and publish the finished directory under its final unique name, or use the uploader's built-in staging. A refresh never replaces the model of an active run.

## Create, pin and apply a profile

1. In **Model library**, search across the configured folders and select the intended checkpoint. Inspect its category, inputs, revision and issues.
2. Choose **Create profile**, or open an existing profile and use **Choose from model library**. Selection fills `source_id`, `relative_path` and the catalog's `artifact_revision`.
3. Select the target arm explicitly and bind every real camera input. Use Raw YAML/JSON for supported advanced declarations and settings that have no form control.
4. **Validate**, then **Save profile**. Validation may accept a structurally correct unresolved draft while reporting `ready:false`; that draft cannot yet be prepared.
5. Click **Apply model** and follow the preparation result. Applying during an active operation can be queued for after that operation and hold confirmation finish. It does not silently interrupt the current operation.
6. In **Run policy**, choose the intended run mode/duration, supply an instruction for a language-conditioned policy, and explicitly click **Start policy**. ACT normally does not need an instruction. Stop ends the operation through the existing owner.

For example, the GUI could save this local profile after selecting a real catalog entry. The digest below is a placeholder: use the exact returned `sha256:<64 hex digits>` value, not a hand-written identifier.

```yaml
schema_version: 1
id: cup_policy
name: Cup policy
execution:
  kind: local
  device: cpu
model:
  source_id: project
  relative_path: cup-policy-v2
  artifact_revision: REPLACE_WITH_CATALOG_ARTIFACT_REVISION
bindings:
  arm: arm1
  cameras:
    observation.images.front: forward
    observation.images.wrist: wrist
extensions: {}
```

Profiles are saved in one canonical backend directory. **Import profile** accepts a small Station YAML/JSON configuration; it does not upload weights. Native inference-server YAML is a different format and cannot be imported as a Station profile. Form and Raw share one draft; unsaved changes, invalid text and revision conflicts remain visible instead of being silently discarded. See [editor behavior](policy-management-frontend.md#editing-behavior).

### What preparation establishes

The deployed local loader uses LeRobot 0.7.0's installed policy/configuration classes, saved pre/post-processors and synchronous inference engine. It verifies the pinned artifact again, checks dimensions/order/units/camera contracts, loads the weights, and returns a paused session. Local preparation deliberately does not perform inference or validate physical task success.

The category shown in the catalog is not an end-to-end compatibility promise. A custom policy package and its dependencies must already be installed in the native `.runtime-venv`; discovery/upload does not install packages or execute code from the checkpoint directory. Its policy, processors and queued-action behavior must fit the current adapter. Outputs after postprocessing must be the configured arm's absolute joint targets and percent gripper target; arbitrary velocity, latent-controller, incompatible relative-action, or unrelated-robot outputs are not automatically converted by selecting a file.

Catalog recognition is broader than native loading. An accepted `.bin` or sharded-weight inventory still needs a compatible `from_pretrained` loader in the selected installed policy. Prefer its normal LeRobot export; a complete catalog inventory alone does not prove that the native loader can deserialize it.

Named mapping widths must match the checkpoint's declared state/action dimensions. A general base π0.5 checkpoint is not automatically an SO-101 policy. Use a compatible adaptation/export and its actual processors, normalization and camera layout. Local CPU/CUDA/MPS/auto choices depend on the native environment's available device; selecting CUDA does not install a driver or a CUDA-enabled PyTorch build.

One resident local session is managed per arm. Changing an idle local model can require unloading/loading and a visible delay. A failed replacement requires explicit reapplication. Large-model memory checks are conservative admission checks, not a claim that every uploaded model will fit or meet an action-rate target.

## Remote inference

Keep the checkpoint and processors on the inference computer. Install the native LeRobot remote dependencies and the chosen policy's dependencies there. Start from [the supplied policy-server configuration](../config/policy-server.example.yaml), then set the server-local checkpoint path, device, deployment name, reachable private endpoint, and exact observation/action contract.

Run on that computer:

```bash
lerobot-policy-server --config_path=/absolute/path/to/cup-server.yaml
```

When using this checkout's prepared native environment, `scripts/serve-policy /absolute/path/to/cup-server.yaml` invokes that CLI from `.runtime-venv`. See [LeRobot 0.7.0 remote inference](https://huggingface.co/docs/lerobot/v0.7.0/en/remote_inference) for server startup, supported chunk-policy contracts and networking. A locally executable policy is not necessarily remotely compatible.

The Station profile uses `execution.kind: remote`, endpoint, deployment and `expected_artifact`, with no local `model` reference. Obtain the exact serving artifact identity from the server's descriptor/diagnostics; current Station inspection requires that identity to be supplied and checks it. Optional `instance` pins a particular server instance. Trained state/action units/order, calibration reference and whether actions are language-conditioned must be declared in the profile contract when the descriptor does not establish them. The native server's `language` capability means optional text generation, not whether a VLA consumes an action instruction.

**Read server descriptor** performs discovery only. Remote Apply checks the descriptor and prepares an instance-pinned runtime profile; admission to the native inference session happens only on explicit Start. To change the server's weights, update/restart its deployment on that computer or run a separate prepared deployment. The Pi dashboard does not rewrite remote checkpoint paths. Startup-owned `policy_management.connections` can provide named transport settings; credential-file paths cannot be supplied through a raw GUI profile.

## API upload procedure

All paths below are relative to the Station origin and inherit its existing access policy. Use the configured HTTPS certificate trust. Private-network owner access and a configured legacy bearer token are supported by the existing gateway; no separate uploader credential channel is introduced.

1. Create one upload with an inventory of **every** file. Sizes are exact byte counts; each path is relative to the selected package root. Optional `sha256` is a complete-file hexadecimal digest. A request has this shape (numbers here are illustrative):

   ```json
   {
     "source_id": "project",
     "destination_name": "cup-policy-v2",
     "kind": "folder",
     "files": [
       {"path": "pretrained_model/config.json", "size": 1537},
       {"path": "pretrained_model/model.safetensors", "size": 1000000},
       {"path": "pretrained_model/policy_preprocessor.json", "size": 321},
       {"path": "pretrained_model/policy_postprocessor.json", "size": 287},
       {"path": "station-policy.yaml", "size": 2048}
     ]
   }
   ```

   POST it to `/api/v1/model-uploads`. The response includes `id`, `kind`, `chunk_size` (currently `131072`), `files`, `state`, `total_bytes` and `received_bytes`. For an archive, use `kind:"archive"` with exactly one ZIP/TAR/TGZ inventory entry.

2. For each file index in that admitted manifest, send its bytes in chunks of at most `chunk_size`. Use the original byte offset, not a chunk sequence number:

   ```http
   PUT /api/v1/model-uploads/{upload_id}/files/{file_index}/chunks/{byte_offset}
   Content-Type: application/octet-stream
   X-Chunk-SHA256: {64-character hexadecimal SHA256 of this exact chunk}

   {raw binary bytes}
   ```

   The last chunk may be shorter; a zero-length file needs no PUT. Repeating an acknowledged offset with the same length/hash is idempotent. Changed or overlapping content returns a conflict. Chunk acknowledgments and upload-list responses are compact; they do not resend all receipts on every chunk.

3. POST `{}` to `/api/v1/model-uploads/{upload_id}/finalize`. Poll GET `/api/v1/model-uploads/{upload_id}` until the background verifier is terminal. `cataloged` means publication succeeded; follow its `scan_job` to completion and refresh the model list. `model_id` may appear after that scan. A cataloged custom artifact may still have unverified implementation or compatibility issues.

4. For resume, GET the upload detail, compare the same destination/kind and exact file inventory, then hash every recorded `{offset,size,sha256}` interval against the reselected local files **before** sending any new bytes. Send only missing intervals. Do not treat matching filenames/sizes as content identity. The browser implementation in [policy-api.js](../web/policy-api.js) follows this procedure with bounded slices.

5. POST `{}` to `/api/v1/model-uploads/{upload_id}/cancel` to abandon that upload's staging. There is no server pause endpoint: the browser pauses its sender locally, while receipts remain durable. Sources that become unavailable also pause progress until the admitted source is eligible again.

These requests upload and publish files only. Profile Apply and operation Start are separate endpoints/actions.

### Endpoint quick reference

| Purpose | Method and path | Request/body |
|---|---|---|
| Inspect loaded source settings | GET `/api/v1/policy-sources` | None |
| Reload source file | POST `/api/v1/policy-sources/reload` | `{}` or `{expected_revision}` |
| Start/poll catalog scan | POST `/api/v1/policy-scans`; GET `/api/v1/policy-scans/{id}` | `{source_ids?: [id]}` for POST |
| Search/list or inspect artifact | GET `/api/v1/policies?q=...&source_id=...`; GET `/api/v1/policies/{id}` | None |
| List/read profiles | GET `/api/v1/policy-profiles`; GET `/api/v1/policy-profiles/{id}` | None |
| Create/update profile | POST `/api/v1/policy-profiles`; PUT `/api/v1/policy-profiles/{id}` | `{text}`; update also requires `expected_revision` |
| Validate or refresh profiles | POST `/api/v1/policy-profiles/validate`; POST `/api/v1/policy-profiles/refresh` | `{text}` or `{document}` for validation; `{}` for refresh |
| Preview/import profile | POST `/api/v1/policy-profiles/import-preview`; POST `/api/v1/policy-profiles/import` | `{text}`; import also supplies `id`, `mode` (`new` or `replace`), and replacement `expected_revision` |
| Export profile | GET `/api/v1/policy-profiles/{id}/export?format=yaml` | `format=json` is also supported |
| Inspect local/remote contract | POST `/api/v1/policy-profiles/inspect-draft`; POST `/api/v1/policy-profiles/{id}/inspect` | Draft `{text}`/`{document}`; saved `{expected_revision}` |
| Prepare/poll | POST `/api/v1/policy-profiles/{id}/prepare`; GET `/api/v1/policy-preparations/{job_id}` | `{expected_revision,mode}` for POST; mode is `idle` or `after_current` |
| Read applied state | GET `/api/v1/policy-runtime` | None |
| Create/list/read upload | POST `/api/v1/model-uploads`; GET collection or `/{id}` | Creation inventory above |
| Transfer chunk | PUT `/api/v1/model-uploads/{id}/files/{index}/chunks/{offset}` | Raw bytes plus chunk-hash header |
| Finalize/cancel upload | POST `/api/v1/model-uploads/{id}/finalize` or `/cancel` | `{}` |

Inspection/preparation can return an asynchronous `job`; poll it rather than interpreting HTTP success as model readiness. Complete response envelopes and operation interfaces are in [the management API reference](policy-management-api.md) and [runtime reference](runtime-reference.md).

## Troubleshooting

| Result | Next action |
|---|---|
| Source missing from upload destinations | Check the loaded source ID, `enabled`, `allow_upload`, host permissions, mount availability and free space; reload after source-file edits. |
| Model absent after an SSH copy | Finish the copy, Refresh models and inspect scan errors. Keep the checkpoint/config/processors together. |
| `incomplete` / missing companion | Restore the missing processor, state/statistics or weight-shard file from the export. |
| `unverified` implementation | Install the matching policy package/dependencies in the native environment, then use explicit preparation to check it. Upload does not install code. |
| `DESTINATION_EXISTS` | Choose a new directory/version name. Existing content is never overwritten. |
| `CHUNK_HASH_MISMATCH` / resume content mismatch | Reselect the original bytes or start a new upload for the changed export. |
| Profile is saved but cannot Apply | Resolve its reported artifact revision, named joint/unit mapping, camera keys, calibration or other trained-contract errors. |
| `REVISION_CONFLICT` | Retain the draft, compare the newer saved profile and explicitly reload, rebase or save a copy. |
| Artifact changed after selection | Refresh the catalog, inspect/select the new content revision and save the profile again. |
| Runtime/device/memory unavailable | Use the supported native environment/device or a compatible remote deployment. A metadata-only preview cannot prepare a real policy. |

## Verification scope

This guide follows the implemented catalog, profile, upload, binding and native-preparation contracts. The repository has hardware-free coverage for source reload, incomplete packages, profile revisions, exact chunk hashes, interrupted uploads, archive containment, no-overwrite publication and explicit bindings. Useful checks are:

```bash
PYTHONPATH=python:tests .venv/bin/python -m pytest -q \
  tests/test_policy_catalog.py tests/test_policy_profiles.py \
  tests/test_policy_uploads.py tests/test_policy_http.py tests/test_policy_bindings.py
node --test tests/test_policy_api.mjs tests/test_policy_draft.mjs
```

Those checks use fixtures and temporary stores; they do not run a trained network or establish physical task success. The native adapter tests use an SDK stand-in to check preparation boundaries. See [recorded deployment verification](policy-management-deployment-2026-11-13.md) for what was actually verified on the reference host.
