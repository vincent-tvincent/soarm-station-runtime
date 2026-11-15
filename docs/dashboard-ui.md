# Station dashboards

The browser UI opens and connects automatically over the station's existing
Tailscale connection or on the station itself. There is no operator-token form.
Open `/` for Operations or `/#graph` for the generic ROS explorer. The small
Reconnect control retries a lost connection; loading the page does not grant
motion permission or resume an operation.

The gateway trusts the actual loopback/Tailscale socket peer. Existing bearer
credentials remain compatible for clients outside that private connection;
`web.trusted_network_access: false` restores bearer-only API access when needed.
No token is embedded in the page, stored in the browser or placed in a URL.

## Manual controls update

The approved Joint / keyboard IK / Leader modes and measured FK view are now
available. See [manual controls](manual-dashboard.md) and the
[deployment evidence](manual-deployment-2026-11-13.md).

## Operations

The first configured arm is selected automatically when the page loads. You can
choose another arm; normal status updates preserve that choice. Selecting an arm
loads its monitoring views without enabling motion. The right panel switches between **Status**, **Control**
and **Payload**, leaving the camera region wide. Status lists each motor's
position, torque, temperature and voltage. A current column appears only when
at least one current reading is supplied. Null values stay unavailable, and
source/staleness is visible. Clicking a motor reveals its reported goal and
fault availability. The SO-101 adapter currently reports no electrical current;
raw servo load is not labeled as amperes.

Camera layouts preserve native image ratios and never crop. **Equal** is the
initial layout. Configure each visible camera name with `cameras[].label` beside
its device binding; captions, accessible names and policy choices follow that
label while camera IDs and model-input keys stay stable.

- **Equal** maximizes a common image area using row/column packing. Same-ratio
  feeds have identical dimensions; mixed-ratio feeds have equal image area.
  Window boundaries follow the image and its caption, rather than stretching
  a black container. The packing algorithm responds to available width/height.
- **Primary** places the supplementary feeds in a horizontal dock immediately
  above the primary image. Click a feed to promote it. Extra feeds scroll in
  the dock.
- **Floating** places supplementary windows inside the primary image. Drag
  them, or use arrow keys when focused; positions remain bounded on resize.
  A click promotes the feed; a drag does not. Positions are kept per camera
  for the current page session.

The workspace tabs occupy the left side of the main header directly; the former
brand label is removed. Dropdown chevrons use a right inset equal to their
centered vertical whitespace, including the compact payload and policy controls.

Camera FPS describes browser receipt, not source capture timing. No frame for
more than two seconds is labeled explicitly. Cameras and control use separate
WebSockets. Changing a layout does not open USB devices or restart capture. A dropped or silent
camera WebSocket reconnects automatically with bounded backoff. Video recovery
does not reopen the control socket, grant permission or resume a stopped operation.

Manual controls use named joint bounds and measured pickup. **Start manual stream**
requests permission when needed, then starts the stream in one explicit click.
No separate Enable click is required for manual mode. Without edited targets,
the controls refresh from the current measured pose when starting. Payload supports
DIRECT, TRAJECTORY and MODEL in INLINE/STREAM modes. The JSON editor contains
semantic intent; the transport adds current owner/session/window identities.
MODEL requires a registered profile. Existing configured Cartesian endpoints
are accepted only for TRAJECTORY INLINE. Unsupported fields reject rather than
being silently ignored. Starting a stream admits it once; subsequent **Send
updated payload** operations replace its intent. **End stream** and **Hold**
release permission. Normal INLINE completion retains the healthy permission
as before. Display updates are decimated independently of the 100-Hz browser
manual sender and existing owner loop.

## Structured payload editing

Payload contains **Blocks** and **JSON** subtabs for one shared draft. Blocks
provide editable joint targets, waypoints, model-task sequences and configured
Cartesian endpoints. Continuous DIRECT/MODEL inputs use a single target/task
card. Sequence cards can be added, removed and reordered; changing a card does
not transmit a command. Run/Send updated payload remain explicit actions.

Joint waypoint times are cumulative from the start. Reordering/removing carries
each segment duration with its target and recalculates later cumulative times.
Model and Cartesian durations are per-step; blank (or JSON zero for a model)
uses the existing model-profile duration behavior. Joint/gripper values retain
full numeric precision and never acquire invented targets.

Switching subtabs preserves the draft and expanded card. Unknown JSON fields
survive block edits; submission still rejects unsupported fields. Invalid JSON
shows an error and never falls back to an earlier valid payload. Validate uses
the same semantic normalizer as submission, with no networking or permission
change. Runtime planning/admission remains separate.

The pure editor model is `web/payload-model.js`; its row IDs are editor metadata
and never appear in submitted payloads. `tests/test_payload_model.mjs` covers
all operation shapes, timing/reordering, invalid drafts and lossless fields.

## Generic ROS graph

`web/graph.js` consumes schema-version-1 graph JSON through an injected request
function. It contains no arm names, camera names or permanent hardware-role
colors. The backend contract is documented separately with the graph observer.

Single-click selects and highlights without moving or removing blocks. The
selection is white on black; related publishers are red only while relevant.
Publish/subscribe paths use warm/cool hues. Double-click (or Shift+Enter) opens
an isolated neighborhood. **Exit focus** restores the overview and its prior
pan/zoom. Search and infrastructure filtering are explicit, with visible/total
counts. Each discovered node appears once; duplicate FQNs and unresolved
endpoint owners retain their backend-provided discovery metadata.

Node boxes retain their strong top rule and comment/metadata area. Topic boxes
show a shortened name/type, observed-rate label and sample-history sparkline;
the inspector retains the exact fully qualified name, type and endpoint QoS.
Comments are local to the current view. Actions/services use subdued dashed
links and can be inspected from the interface list. Crossings never join.
Both column gaps are at least 160% of the widest block width. With the current
230/238-unit blocks, each gap is 381 units; zoom and Fit preserve this ratio.

Sampling is an explicit, bounded observer operation, not a robot command.
Unsampled, unavailable and measured-zero states are different. Counts and
serialized bytes describe only that observer subscription, not guaranteed
network delivery or wire bandwidth. Message contents are not exported.

### Continuous graph-wide activity

The horizontal divider above the Topics/Actions/Services pane adjusts its
height. Drag upward for more list space or downward for more canvas space.
With the divider focused, Up/Down changes height, Shift makes larger steps,
Home resets and End maximizes it. The choice is stored locally and clamped to
the available screen height. Resizing preserves graph zoom, center and selection;
the list scrolls within its pane.

**Live flow** monitors the graph while its page/tab is visible. It covers topics
and action status/feedback regardless of which block is selected; selection
still controls the original warm publication / cool subscription highlights.
Moving spheres appear only on those highlighted connections of the current
selection. Unfocused interfaces keep their live rates and plots without spheres.
The number above each sphere is the observed message-count increase since the
previous graph update. Publisher labels use attributed counts where available;
subscriber labels show the topic count, not verified subscriber delivery.
Both publish and subscribe flow dots are solid, using their warm and cool route
colors. Marker fill does not encode delivery confirmation.
Action server labels combine its observed status/feedback contributions.
Every topic and action block retains its activity history plot. Unknown,
blocked, idle and observed-active states are distinct.

A renewable 12-second browser lease keeps shared observations alive, with
renewal approximately every four seconds. Renewing does not recreate
subscriptions, reset counters or interrupt observation. Turning Live flow off,
hiding the page or disconnecting releases only that browser's lease; expiry
handles closed/lost clients. Transient motor-status polling failures do not
permanently cancel read-only graph monitoring. The existing finite-sample API
is retained for external clients and older-backend diagnostics.

Moving dots have a stable repeating phase while fresh counters advance.
Publication and subscription directions animate concurrently; no one-shot
traversal is restarted on each HTTP poll. Frozen counters expire even if HTTP
requests keep succeeding. Animation remains local at up to 30 FPS / 128 dots,
with configurable graph polling and cached curve coordinates.
Hidden tabs and reduced-motion preferences stop animation. No per-message
browser event stream is used.

The **Update** selector offers 0.5, 1, 2, 5 and 10 seconds (default 2), saved
locally in the browser. This is a target display cadence; network and scheduling
delays can vary the actual interval. Requests do not overlap. A separate
two-second heartbeat maintains the approximately four-second lease renewal even
with ten-second display updates. Slower display rates extend only the permitted
freshness horizon; changing the setting cannot manufacture new activity.

Interval durations use differences between counter snapshots from the same
observer's monotonic clock, independently of browser arrival/render timing.
Clock or counter-epoch changes re-prime the interval instead of inventing a
duration. Only legacy snapshots without observer-clock fields use client timing.
Counter-only refreshes update plots, text and markers in place, preserving graph
buttons, selection and layout; topology/filter changes still rebuild the view.
This setting does not change ROS publication rates or the robot control loop.

Publisher GIDs from actual ROS MessageInfo attribute incoming topic traffic
when available, including multiple publishers. Unattributed single-source
routes and subscriber paths also use solid dots; subscriber delivery is not
confirmed by this observer. Attribution remains in the inspector and tooltips.
Action dots follow observed server status/feedback
back toward clients, with bidirectional dashed routes. They do not infer
unobserved goal requests or per-client receipt. Historical goal-state summaries
are labeled as last reported, rather than treated as perpetual traffic.

Existing dashboard subscriptions provide passive counts/history for their
exact topics, including compressed camera frames, without another image
subscription or payload serialization. Other eligible topics use bounded raw
observations, including raw images, point clouds and audio when ROS type support
is available. Internal buffer channels use exact publisher-GID projections from
logical-topic receipts instead of recursive adaptive subscriptions.

At most 64 additional raw topics are watched. Rolling five-second budgets
(default 10,000 messages per topic; global 50,000 messages),
20 history buckets and bounded publisher maps
limit work. Throttled/unavailable sources are reported, not shown as healthy.
Continuous counting has no media-type or byte-volume exclusion; the separate
finite-sample endpoint retains its documented sampling limits.
Graph metadata and traffic are read-only; no robot operation is submitted.

## Files and verification

- `web/app.js`: operations UI and camera interaction integration.
- `web/styles.css`, `web/index.html`: common Swiss visual system and markup.
- `web/camera-layout.js`: pure aspect-preserving packing functions.
- `web/transport.js`: authenticated typed transport, with no automatic arming,
  replay or operation reconnection.
- `web/graph.js`: generic graph normalization, rendering and inspection.

Run browser-independent checks with:

```sh
node --test tests/test_dashboard_transport.mjs tests/test_dashboard_graph.mjs tests/test_graph_flow.mjs tests/test_camera_layout.mjs tests/test_payload_model.mjs
```

For a hardware-free visual preview (requires aiohttp and Pillow):

```sh
python scripts/preview-dashboard.py --port 8767 --reference-root /path/to/so-arm
```

The preview is loopback-only, prominently marked as mock, and uses historical
camera references from the supplied project directory. Its controls affect
in-memory fixtures only. It never imports the robot runtime, opens a device or
connects to a production backend. The loopback preview also opens without a token.
See `design-qa.md` for browser evidence and intentional refinements to the
original visual targets.
