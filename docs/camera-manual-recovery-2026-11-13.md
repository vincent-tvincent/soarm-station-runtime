# Camera recovery and manual Start — November 13, 2026

The user reported intermittent camera connection failure and confirmed that
Start manual stream was greyed out. Live inspection found fresh captures on all
three cameras, roughly 100 Hz arm feedback, no active operation and permission
OFF. The button was gated by a separate Enable control click. Browser camera
sockets had no retry path after a close; server logs contained closing-transport
errors. A fixed 15-second heartbeat failure was not reproduced on aiohttp3.14;
it is not claimed as the root cause.

## Changes

- Camera-only reconnect with 0.5–5-second backoff and recovery after a silent
  socket. Explicit disconnect/removal cancels retries. Old decode results cannot
  overwrite a replacement stream. Camera recovery does not reopen control or
  issue permission/hold requests.
- Server camera handlers now receive PING/CLOSE concurrently, terminate cleanly
  after connection loss, and avoid redundant WebSocket compression of JPEGs.
- Start manual stream is available with a selected writable arm and fresh
  feedback. Its explicit click requests permission when needed and starts the
  existing stream. Opening the page does not enable motion. Pending starts
  respect Hold/disconnect; unedited controls refresh from current measurements.

## Verification

- 54 frontend tests passed locally; 28 transport tests passed on the Pi.
- 6 camera lifecycle tests passed; the Pi's combined camera/access/route run
  passed16 tests and57 subtests.
- A hardware-free browser test deliberately closed every camera every2seconds.
  Each camera reconnected69times while the simulated manual stream remained
  active (3033 observed command sequence). One-click Start worked from disabled
  permission without touching the separate Enable button. No browser errors.
- All3 real camera sockets remained open during a concurrent30-second Tailscale
  read: forward431frames, overview504, wrist502; maximum received gaps were
  0.203/0.205/0.185seconds. These are receiver counts, not a capture-FPS claim.
- Live browser inspection showed all3 feeds updating and Start manual enabled.
  The physical Start button was not pressed by the assistant.

Source commit:2b7d012. Deployed code:1a9d2cf. Only dashboard_node was reloaded
(49774→50235); service48080, lerobot_node48107 and nativeworker48198 stayed the
same. Native boot/session and goal-write count were unchanged. At deployment,
permission was OFF and goalwrites0. No physical movement command or calibration
change was issued by the assistant.

Evidence: ignored local/camera-recovery-deployment.json and
local/camera-recovery-js-tests.log; outer-project
output/dashboard-ui/manual-ready.jpg and camera-live-read.json.
