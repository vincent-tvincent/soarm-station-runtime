# Policy management workspace

The Policies workspace is the third top-level page after Operations and ROS graph. Its main views are Profiles, Model library, Uploads, Sources and Run policy. Create/Edit and Import are subviews of Profiles. The application keeps one shared `StationClient`; the policy page does not create another motion owner.

## Editing behavior

- Form and Raw YAML/JSON edit one document. Form changes preserve comments and advanced fields without dedicated form controls.
- Leaving an unsaved profile offers **Save profile**, **Discard changes**, or **Cancel**. A failed save, invalid raw text or revision conflict keeps the editor open.
- Closing/reloading the browser warns about unsaved drafts or an active browser upload. It does not attempt asynchronous saving during unload.
- Ctrl/Cmd+S saves while the profile editor is visible. Switching Form/Raw is not a save and does not activate a model.
- Saves carry the exact opened file revision. External changes produce a conflict instead of last-writer-wins replacement. Compare the latest saved text, discard/reload, save a copy, or explicitly retain the local draft against the latest base and save again.
- An edit made while a save/validation/read is pending is never overwritten or marked valid by that older response. Save acknowledgment advances the saved baseline without erasing a newer draft.
- A profile import is previewed before saving. Duplicate IDs require a new ID or explicit revision-matched replacement. Import does not apply a model.

## Model selection and sources

The library uses a searchable, scrollable list grouped by configured source. Search is across the backend catalog, not just visible rows. Selecting a model updates its source-relative reference and derives its category/input rows from metadata. Model selection does not reinterpret arbitrary weights as a different architecture.

Sources is read-only. Edit the host's configured source file externally, then use **Reload source config**. A successful reload adopts the validated list and starts a changed-source scan. **Refresh models** finds files copied into already configured roots. The applied model/session is independent of catalog refresh.

Profiles are canonical files in one backend profile directory. Root-list configuration and profile files are separate authorities; catalog indexes are caches. The UI displays the actual backend locations rather than assuming a browser-local path.

## Uploads

Upload a checkpoint folder or ZIP to one configured writable source and a new destination directory. The browser never opens a general remote filesystem picker. Model weights, configuration and saved processors belong in the bundle.

Files are read in at most 128-KiB slices, hashed and transferred as bounded raw chunks. The browser does not buffer an entire model in RAM. Pause stops this browser's sender while leaving staged chunks on the server. After reload, reselect the original bundle to resume; recorded chunk hashes are verified against the local selection before any new chunk is sent. Filename/size alone are not sufficient identity.

Uploads continue while switching application views. They remain unselectable until server verification/publication/indexing finishes. A server error, interrupted network, name conflict or unavailable destination is shown explicitly. Cancellation affects only the selected upload's staging data. Existing artifacts are not overwritten.

## Apply and run

Apply pins the displayed saved profile revision and requests native preparation. It does not enable torque, grant control permission or start an operation. Applying during an active operation offers an explicit after-current boundary instead of replacing that operation.

Run policy uses the shared `StationClient` for explicit enable, MODEL INLINE/STREAM submission, acknowledged instruction updates and Stop. This panel only updates/stops the operation identity it started. ACT does not require a language instruction; language-conditioned profiles do. Local/remote model compatibility and preparation status come from the backend. No synthetic profiles or model-ready states are embedded in production UI code.

## Modules and integration hooks

- `web/policy-management.js`: view rendering, navigation dialogs and orchestration.
- `web/policy-draft.js`: pure shared-document state, conflict and asynchronous-save handling.
- `web/policy-api.js`: same-origin management API and bounded upload transport.
- `web/policy-management.css`: styles scoped to `#policy-manager`.
- `web/yaml-vendor.js` and `web/LICENSE-yaml.txt`: pinned local YAML parser; no runtime CDN dependency.
- `scripts/preview-policy-management.py`: loopback-only QA wrapper with temporary metadata fixtures, real management CRUD when backend modules are available, and no native preparation/robot connection.

The shared application shell forwards connection/state/operation events to the component and awaits `beforeLeave()` before leaving Policies. `setVisible()` controls management polling. Operations, camera capture, manual-mode rendering and ROS graph layout remain owned by their existing modules.

Focused tests:

```sh
node --test tests/test_policy_draft.mjs tests/test_policy_api.mjs tests/test_policy_navigation.mjs
```

Tests use synthetic inputs/mocked network only. Browser QA and live deployment evidence must be recorded separately; these tests do not establish physical policy task success.
