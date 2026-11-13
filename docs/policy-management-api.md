# Policy-management API

All routes use `/api/v1`. The dashboard's existing access middleware applies to
these routes. JSON responses include `schema_version: 1`; errors include
`code`, `message` and optional `details`. Validation errors use HTTP 400,
missing resources 404, revision/destination conflicts 409, request limits 413
and unavailable native execution 503.

## Sources and catalog

| Method and path | Request | Response |
|---|---|---|
| GET `/policy-sources` | — | Loaded `revision`, `config_path`, fixed `profile_store`, source-file `text`, `roots` and optional `config_error` |
| POST `/policy-sources/reload` | `{expected_revision?}` | Adopted source state, `changed_source_ids` and optional `scan_job` |
| GET `/policies` | Query `q`, `source_id` | `revision`, `models`, source `groups`, `total` |
| GET `/policies/{id}` | — | `model` with its complete file inventory |
| POST `/policy-scans` | `{source_ids?: [id]}` | `job` |
| GET `/policy-scans/{id}` | — | `job` |

Reload reads the startup-configured host file; its request cannot replace source
paths or the profile directory. An invalid reload keeps the loaded revision.
Root records expose `id`, `label`, configured/resolved paths, `enabled`,
`allow_upload`, online/writable/space information, `state` and `reason`.

Model records contain `id`, `name`, `category`, `format`, `source_id`,
`relative_path`, `resolved_path`, `artifact_revision`, `size_bytes`, `state`,
`selectable`, `reason`, `issues`, `camera_inputs`, state/action features,
cadence, language/calibration declarations and files. Unknown contract fields
remain unknown. `selectable` permits profile selection; it does not acknowledge
native preparation. Content revisions use `sha256:<digest>`.

List/group rows include `file_count`, `files_truncated` and at most 32 file
summaries; individual model GET returns the full inventory. Job states are
`queued`, `running`, `complete` or `failed`, with `phase`, `progress`, `errors`
and timestamps. Scanning and hashing run outside the device-control thread.

## Profile files

| Method and path | Request | Response |
|---|---|---|
| GET `/policy-profiles` | — | `profiles` summaries and list `revision` |
| POST `/policy-profiles` | `{text}` or `{document}` | Created detail; never overwrites |
| GET `/policy-profiles/{id}` | — | Profile detail |
| PUT `/policy-profiles/{id}` | `{text, expected_revision}` or document equivalent | Saved detail |
| POST `/policy-profiles/validate` | `{text}` or `{document}` | Validation |
| POST `/policy-profiles/refresh` | `{}` | Refreshed profile list |
| POST `/policy-profiles/import-preview` | `{text}` | Parsed candidate, validation and any existing-file conflict |
| POST `/policy-profiles/import` | `{text, id, mode, expected_revision?}` | Imported detail |
| GET `/policy-profiles/{id}/export` | Query `format=yaml` or `json` | Download attachment |

Detail contains `id`, `name`, file-content `revision`, `text`, semantic
`document` and `validation`. Validation contains `valid`, `ready`, `errors`,
`warnings`, `category` and optional `resolved_model`. A schema-valid unresolved
profile can be saved with `valid:true, ready:false`. This is still a draft.

Updates and replacement imports require the exact expected revision. A conflict
returns the current revision/text in `details`; clients should retain their
draft. Import mode is `new` or `replace`. The profile ID becomes the canonical
`<id>.yaml` filename. See [the example](../config/policy-profile.example.yaml)
for the complete document shape. Declared comments, advanced fields and
`extensions` survive structured edits through the round-trip parser.

Local execution specifies `kind: local` and `device`; its `model` pins
`source_id`, `relative_path` and `artifact_revision`. Remote execution specifies
`kind: remote`, `endpoint`, `deployment`, `expected_artifact`, optional
`host_label`, `connection_profile` and `instance`, without a local model path.
Bindings name the arm, camera IDs and optional state/action feature mappings.
Missing trained contract declarations can be supplied explicitly; known
artifact declarations remain authoritative.

## Inspection, preparation and runtime

| Method and path | Request | Response |
|---|---|---|
| POST `/policy-profiles/inspect-draft` | `{text}` or `{document}` | `validation`, `descriptor`, `admitted:false`, optional `job` |
| POST `/policy-profiles/{id}/inspect` | `{expected_revision}` | Same inspection envelope |
| POST `/policy-profiles/{id}/prepare` | `{expected_revision, mode}` | `job`, current `applied`, `pending` |
| GET `/policy-preparations/{id}` | — | `job`, current `applied`, `pending` |
| GET `/policy-runtime` | — | Applied/pending state, jobs, descriptors and capabilities |

Remote inspection is asynchronous. Poll its job until terminal; successful
`job.result` contains `descriptor`, `admitted:false` and refreshed `validation`.
The descriptor cache is short lived and keyed by the exact remote execution
reference/settings. Preparation freshly rechecks remote identity.

Preparation mode `idle` requires an idle arm. `after_current` explicitly defers
until the current operation and hold confirmation finish. Neither mode stops
an active operation, grants permission or starts policy inference. A local
ready acknowledgment means weights/processors loaded and a paused session;
it reports whether warmup/inference was performed. Remote ready means its
descriptor was verified; native session admission happens on explicit Start.

Runtime contains `applied`, `pending`, `applied_by_arm`, `pending_by_arm`,
`runtime_profiles`, `active_runs`, `recent_runs`, `jobs`, `descriptors` and
`capabilities`. Singular applied/pending fields are convenient recent records;
multi-arm clients should use the per-arm maps. Prepared records include
profile identity/revision, execution, arm, artifact, language-conditioning and
immutable `runtime_profile_id`. These IDs appear in the existing resources
response and are used by the existing MODEL operation API. A newer draft does
not change an active run's pinned identity.

Without a native bridge, real file/catalog/upload operations remain available,
while runtime is explicitly unavailable and cannot become falsely ready.
Profile preparation and execution remain separate from physical task success.

## Resumable model upload

Create with POST `/model-uploads`:

```json
{
  "source_id": "project",
  "destination_name": "cup-act-v2",
  "kind": "folder",
  "files": [{"path": "config.json", "size": 1537}]
}
```

`kind` is `folder` or `archive`; optional per-file `sha256` is hexadecimal.
Creation returns an upload record with `id`, `kind`, `chunk_size:131072`, source,
destination, state, byte counts and file manifest. Destination is one new
directory name under an eligible configured source.

PUT raw bytes to
`/model-uploads/{id}/files/{fileIndex}/chunks/{offset}` with
`Content-Type: application/octet-stream` and `X-Chunk-SHA256: <hex digest>`.
Chunks are at most 128 KiB. Exact verified retries are idempotent; conflicting
data at an admitted interval returns 409.

| Method and path | Meaning |
|---|---|
| GET `/model-uploads` | Compact upload summaries |
| GET `/model-uploads/{id}` | Full manifest and verified `{offset,size,sha256}` chunk receipts |
| POST `/model-uploads/{id}/finalize` with `{}` | Begin background verification/publication |
| POST `/model-uploads/{id}/cancel` with `{}` | Remove only this upload's staging |

Chunk acknowledgments are compact. Resume clients reselect files and compare
recorded chunk hashes; file names/sizes alone do not establish a safe match.
UI pause stops its sender locally while the durable upload remains resumable.
States include `uploading`, `paused`, `verifying`, `cataloged`, `failed` and
`canceled`. A disabled/offline/replaced source pauses transfer/publication.

Publication validates package completeness, hashes and contained ZIP/TAR/TGZ
extraction, then atomically creates a new destination without overwriting.
Catalog scans exclude staging. Success returns artifact identity and a scan
job; `model_id` becomes available after that scan. Publication does not prepare
or start the model.

## Hosting

```python
manager = PolicyManager(config, resources=resource_provider, bridge=None)
mount_policy_routes(app, manager)
```

The host owns authentication/access middleware and JSON request limits. Cleanup
can use `await asyncio.to_thread(manager.close)`. Blocking file/hash operations
stay off the aiohttp event loop. See [management](policy-management.md) for
startup paths, dependency ownership and native preparation semantics.
