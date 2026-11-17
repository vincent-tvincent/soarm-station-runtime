# Camera startup and recovery

Each camera has one isolated capture process supervised by the native worker.
That process alone owns the device lock, OpenCV capture and JPEG encoding. A
stuck open, read or release can be terminated without restarting the arm's
control thread or the other cameras. The supervisor waits for the old process
to exit before opening a replacement. If the kernel cannot reap it, the camera
reports `RECOVERY_BLOCKED`; it never creates overlapping capture owners.
Linux parent-death signaling also terminates capture children if the native
worker dies. No camera recovery grants motion permission or resumes an operation.

The requested width, height, FPS and FourCC, each setter's result, and the
subsequent driver-reported mode are recorded. A false setter return is visible
even when the decoded image eventually matches. FPS remains a requested/driver
value, not a claim about measured delivery rate. A different reported FourCC
fails startup; unavailable readback is shown as unknown.

Decoded dimensions are checked on every frame before JPEG encoding/publication.
Startup can discard up to eight mismatched frames to drain an old capture mode.
If the mismatch persists, or a running camera changes dimensions, the process
reports the actual dimensions and recovers. Frames are never cropped, stretched
or silently resized to conceal a driver mismatch. A camera that cannot produce
the configured mode remains unavailable until the device or profile is corrected.

The process shares one fixed 16-KiB latest-metadata slot, not a FIFO of old
frames. JPEG files are written atomically in a camera-specific subdirectory of
`frame_dir`; at most four completed frames are retained. Paths include a capture
identity so a restarted process cannot overwrite a previously published frame.
Failed/recovering snapshots have `ready: false`, an empty `path`, and a stale
`age_ms`, preserving existing policy freshness checks.

Optional settings belong in each `cameras` entry:

```yaml
startup_timeout_s: 10
frame_timeout_s: 3
retry_initial_s: 1
retry_max_s: 15
```

Defaults are 10 seconds for startup, `max(3, 3 / fps)` seconds without a new frame,
and an exponential retry delay from 1 to 15 seconds. Values must be finite and
between 0.05 and 300 seconds; the maximum retry cannot be below its initial delay.
These deadlines apply only to camera capture recovery. They do not change
teleoperation timing. A recovery first requests cooperative exit, then terminates
and, if necessary, kills the capture process. Separate processes add Python/OpenCV
memory overhead but let blocked native capture calls be reclaimed independently.
OpenCV uses one internal worker thread per capture process.

The existing `/soarm_station/health` topic includes a `DiagnosticStatus` named
`camera:<id>` with a `status` JSON value. It reports `state`, `ready`, `error`,
`requested_mode`, `negotiated_mode`, `actual_mode`, `setter_results`, `warnings`,
`restart_count`, `capture_pid`, frame sequence/age and `runtime_snapshot_age_ms`.
Filesystem paths are omitted. A stale native snapshot is reported as
`RUNTIME_UNAVAILABLE`. The dashboard separately measures the age of images it
actually receives, so capture readiness does not imply successful ROS delivery.

Validation uses fake driver frames, simulated JPEGs and isolated child processes:
wrong decoded modes never encode/publish, setter failures remain visible, blocked
open/read/release owners are reaped before recovery, metadata/files remain bounded,
and another camera and mock arm keep running. Physical reconnect behavior is a
separate deployment check; no physical device is opened by these tests.
