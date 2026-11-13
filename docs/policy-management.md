# Policy/model management

The management service indexes host-owned LeRobot artifacts and saves execution profiles. It does not install model implementations, edit physical-device configuration or grant motion permission. Upload, Save, Inspect, Prepare and Start are separate actions.

## Startup-owned locations

The runtime's `policy_management` startup settings are:

```yaml
policy_management:
  source_config: ../config/model-sources.yaml
  profiles_dir: policy-profiles
  cache_db: policy-management.sqlite3
  run_dir: policy-runs
```

Those relative values resolve against the runtime YAML file. Defaults are the checkout's `config/model-sources.yaml`, `local/policy-profiles`, `local/policy-management.sqlite3` and `local/policy-runs`. `profile_store` is accepted as a startup alias for `profiles_dir`. The profile directory cannot be changed by source reload.

Create the example writable source with `mkdir -p local/models`. Edit `config/model-sources.yaml` through host tools:

```yaml
schema_version: 1
roots:
  - id: project
    label: Project policies
    path: ../local/models
    enabled: true
    allow_upload: true
```

Source paths resolve against this source file. Reload validates the entire candidate before adoption and scans added/changed sources automatically. Invalid candidates keep the loaded revision; missing paths remain visible as offline. Refresh models separately discovers files copied into unchanged roots. Roots are not edited through a filesystem-browser API.

## Artifacts and profiles

Discovery recognizes standard LeRobot checkpoints and Station export bundles, checks configuration, weights, saved processors/state and content identity, and does not import checkpoint code or load networks. A `station-policy.yaml` registration sidecar can explicitly declare missing trained feature mappings, camera roles, calibration and cadence. Architecture/category comes from checkpoint configuration. An unknown type requires its installed native implementation; an extension alone does not make weights runnable.

Profiles live only as `<profile-id>.yaml` in the configured profile directory. Raw YAML/JSON and structured saves use the same semantic document. Round-trip YAML preserves comments/recognized advanced fields/extensions. Updates require the expected file-content revision; external edits return a conflict instead of overwriting. Import preview does not save or apply. Schema-valid unresolved references may be saved as drafts, while preparation remains disabled.

Local profiles pin source ID, relative artifact path and SHA256 revision. Remote profiles pin endpoint/deployment/expected artifact and optional instance/startup connection name, with no Pi checkpoint path. The backend rechecks content/descriptor identity before preparation. Joint order/units and camera viewpoints are not guessed from filenames. Required cameras cannot be made optional in a profile; masks must come from pinned artifact/native empty-camera declarations.

## Uploads

Choose a configured enabled, online, writable source with `allow_upload:true` and a new destination directory. Uploads use 128-KiB raw chunks and SHA256 receipts. Same-offset identical retries are idempotent; conflicting receipts reject. Full receipts are returned only by GET upload detail for verified resume; lists/chunk acknowledgments stay compact.

Staging is excluded from catalog scans. Finalization checks coverage, hashes, companion files, safe archive extraction and space, then atomically publishes without overwriting any existing destination. Linux uses `renameat2(RENAME_NOREPLACE)`; the hardware-free macOS preview uses the SDK-defined `renameatx_np(RENAME_EXCL)`. No unsafe rename fallback is used. Source disable/removal/path identity changes pause further transfer/publication. Cancel removes only the upload's own staging. Successful publication refreshes that source; it never applies or starts a policy.

## Preparation and execution

Native preparation runs on a bounded background queue in the separate Python 3.12 environment. A local acknowledgment means weights/processors loaded, contract checked and session paused. It explicitly does **not** mean warmup, inference or physical task success was tested. Installed third-party plugins register through the native CLI helper; no package installation or checkpoint Python execution is performed by discovery.

`idle` preparation rejects an active operation. `after_current` queues until operation completion/hold confirmation; a healthy continuous stream has no new artificial lifetime deadline. Different idle local models use a visible cold replacement; failure after releasing the prior idle model requires explicit reapplication. Existing active runs are not mutated by source/profile edits. There is one resident local session per arm; preparations are serialized and use a conservative available-memory estimate rather than promising arbitrary model capacity.

Remote inspection performs DESCRIBE only and does not admit a client/session or change server weights. Remote preparation verifies its descriptor and records the native instance; admission happens only on explicit MODEL Start through the existing runtime. Native text-generation capability is not action language-conditioning: the latter needs an explicit trained/profile contract.

Prepared runtime IDs are immutable, bounded names exposed in live resources. Operation starts pin profile revision, artifact/source identity and remote instance. Large artifact inventories reside in private pins rather than per-action hashes/copies/IPC. Starting a prepared local model reuses its paused weights; native reset/instruction semantics remain responsible for mutable queues. No management action implicitly grants permission or submits a move.

## API and embedding

The [API reference](policy-management-api.md) lists request shapes, response
envelopes, asynchronous jobs and upload transport.

Routes are mounted by `mount_policy_routes(app, manager)` under `/api/v1`; the host app supplies the existing access middleware. They include `policy-sources`, `policy-scans`, `policies`, `policy-profiles`, `model-uploads`, `policy-preparations` and `policy-runtime`. Errors have `schema_version`, `code`, `message`, and optional `details`. Revision/name conflicts use 409; invalid input 400; unavailable runtime 503.

`PolicyManager(config, resources=provider_or_dict, bridge=None)` and `mount_policy_routes` can be hosted in an isolated loopback preview with temporary roots. Actual CRUD/catalog/upload code runs there; native preparation remains honestly unavailable without a bridge. `manager.close()` is synchronous/idempotent and can run through `asyncio.to_thread` during async cleanup.

Runtime hooks are `policy_capabilities`, `policy_inspect`, `policy_prepare`, `policy_job`, `policy_runtime`. Physical ownership, register dispatch, permission, freshness windows and existing typed operations retain their original authority. No new ROS IDL is required.
