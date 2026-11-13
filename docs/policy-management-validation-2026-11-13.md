# Policy-management backend validation — November 13, 2026

Staged source baseline: `4046c70925ac905ae7f85f50608129749e62c782`, including the existing private-network dashboard access and camera/manual recovery changes. This backend work does not edit visual files or deploy into the live checkout. The Manual coordinator integrates first; this patch must be three-way merged onto that newer baseline before coordinated policy activation.

## Automated verification

- An isolated Linux test checkout and its own Python 3.14 venv under `/var/tmp/soarm-policy-test-2656393cbc` ran the complete candidate suite: **274 passed, 8 native-only skipped, 88 subtests passed** in 9.73 s. Installed ROS interfaces were read-only; the live Python environment/checkouts were not modified.
- Hardware-free local tests cover atomic whole-source adoption, invalid reload retention, live source eligibility, overlap dedup, metadata-only scanning, stable content revisions, incomplete/changed/missing artifacts, and unknown implementation status.
- Profile tests cover raw/structured round trips, comments/declared advanced fields/extensions, optimistic revisions and external edits, concurrent stores, import collisions/copy/replace, wrong native-server format, parser limits and unresolved saveable drafts.
- Upload tests exercise raw 128-KiB SHA256 chunks, idempotent resume/conflicts, compact receipts, source identity/disable pause, space/inventory limits, integrity and archive containment, staging-only cancel, no-overwrite publication, publication recovery and automatic source scans. Both Linux `renameat2` and the macOS preview's checked `renameatx_np` primitive were exercised; no unsafe rename fallback is used.
- Manager and real loopback aiohttp tests exercise the mountable APIs with temporary directories, including real CRUD/import/export/CAS/scans/uploads. Preview has no runtime bridge and cannot report a fake prepared policy. Existing dashboard middleware remains the access authority when mounted in Station.
- Native helper tests verify strong compact-inventory pins, artifact mutation rejection, installed-plugin registration ordering, real-input/masked-slot separation, loader pause/gate, DESCRIBE without admission, and safe descriptor filtering.
- Runtime tests prove no implicit permission grant/revoke, send or hold during preparation; prepared-session reuse through first MODEL Start and semantic reset; immutable IDs; completion-boundary deferral; cold/failure cleanup; active MODEL guards and run provenance. Large inventories stay out of per-action profile hashing/copying and IPC. The memory estimate uses the verified compact pin's inventory.
- A regression keeps native `capabilities.language` (text generation) separate from action `language_conditioned`; the latter remains an explicit trained/profile contract.

## Installed native SDK checks

Read-only installed LeRobot 0.7.0 source inspection confirmed remote DESCRIBE/admission separation and the plugin registration helper used by its CLI. Tiny CPU preprocessing tensors, without model weights/transports/devices, produced two real image views plus one native false-mask view for both installed SmolVLA and PI05 preprocessors. Required views remain required; profiles cannot invent masks in place of the pinned artifact declaration.

An isolated Python 3.12 process read the existing small synthetic ACT export at `training/exports/synthetic_act_fixture`, approximately 44.98 MB. The staged helper verified its content, loaded actual weights and saved processors in 7.79 s, and returned a paused native session with gate WAIT. It performed **no inference, warmup, admission, motor connection or actuation call**.

A second isolated process started only MockArm/MockCamera resources and exercised the staged `policy_prepare`/`policy_job`/`policy_runtime` path with that real ACT checkpoint. Native acknowledgment completed, published an immutable runtime ID and a compact effective profile, while permission stayed false, operation stayed null and mock goalwrites stayed 0. The loaded session remained WAIT/paused and was closed during cleanup. This proves load/processor acknowledgment, not a manipulation skill or inference quality.

## Readiness and remaining qualification

Local prepared means weights/processors loaded and native contract checked; `warmup_performed:false` and `inference_test_performed:false` are explicit. Remote prepared means the advertised descriptor/identity was checked without taking a session; admission still happens on explicit Start. Custom implementations must be installed in the native environment; catalog selection/upload does not install them.

No policy task was executed on the real arm. No credential/token, calibration or PID content was copied/changed, no policy source file was edited on the live host, no live model session was admitted/prepared, and no live process was restarted by this backend staging task. Physical task success, useful VLA fitting, GPU capacity and network deployment performance remain separate qualifications.

Real remote-server admission is intentionally not part of inspection; its no-admit behavior is verified against the installed API and fakes. The existing native execution path continues to handle actual admission, queue/freshness semantics and source-loss outcomes when a deliberately started operation uses a remote profile.

## Integration boundary

Native IPC hooks: `policy_capabilities`, `policy_inspect`, `policy_prepare`, `policy_job`, `policy_runtime`. The new MODEL-only preparation-busy field and supervisor install hook are narrow additions. Manual `hold_owned`, conditional cancellation and FK/IK/Leader hooks remain separately owned and must be preserved in the merge. There is no new ROS IDL.

The coordinating frontend owns new policy assets and shared navigation hooks. Backend adds only their explicit asset names and mountable routes, plus dynamic prepared-profile resource metadata. See [management](policy-management.md). Integration needs the new round-trip YAML dependencies from the updated locks, the merged native worker code and a coordinated activation; management reload/save thereafter does not restart physical ownership.

Isolated machine reports: `/var/tmp/soarm-policy-test-2656393cbc/ros-tests-final.log`, `native-load-report.json`, `native-registry-report.json`. These contain no token values. The test process stopped its mock/native session during cleanup. No Git remote was added and no GitHub push occurred.
