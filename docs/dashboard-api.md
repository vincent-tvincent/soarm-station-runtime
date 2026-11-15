# SO-Arm Station dashboard backend API v1

JSON contracts for resource descriptors, cached motor telemetry and generic ROS graph discovery. Access uses the trusted-peer/bearer policy below. Existing control authority stays with the runtime owner. Null denotes an unavailable value. For the complete route inventory and external control examples, see the [README API tables](../README.md#web-api-tables) and [integration guide](web-api.md).


## Static assets

Public GET `/assets/{file}` serves only the explicit `ASSET_NAMES` allowlist in `dashboard_node.py`: dashboard, graph, transport, camera, payload-editor, Manual/FK and Policies JavaScript/CSS, the local YAML bundle and its license text. No directory traversal or generic filesystem exposure. `/` remains public. APIs, media and WebSockets use the trusted-peer/bearer access policy.

## GET /api/status (additive)

Existing `device_id`, `arms`, `inference`, `joints`, `cameras`, `upload`, `events`, `workflows`, `decisions` stay unchanged. Added fields:

```json
{
  "schema_version": 1,
  "resources": {
    "arms": [{"id":"arm1","label":"arm1","backend":"so101","read_only":false,"base_frame":"arm1_base","tcp_frame":"gripper_frame_link",
      "joints":[{"name":"shoulder_pan","label":"shoulder_pan","unit":"rad","min":-2.8,"max":2.8,"velocity_limit":1.5}],
      "gripper":{"name":"gripper","label":"gripper","unit":"percent","min":0,"max":100,"velocity_limit":100},
      "capabilities":{"operation_types":["DIRECT","TRAJECTORY","MODEL"],"input_modes":["INLINE","STREAM"],"writable":true,"cartesian_endpoints":true},
      "profile_ids":[]}],
    "cameras":[{"id":"forward","label":"forward","width":640,"height":480,"requested_fps":30,"frame_id":"forward_optical","calibrated":false}],
    "profiles":[{"id":"policy1","label":"policy1","arm_id":"arm1","backend":"local","language_conditioned":true,"action_fps":30}],
    "operation_contract":{"schema_version":1,"control_websocket":{"operation_type":"DIRECT","mode":"STREAM"},"stream_endpoint":"/api/stream","operate_endpoint":"/api/operate","feedback_endpoint":"/api/operations/{request_id}"}
  },
  "motor_status": {
    "arm1": {"schema_version":1,"arm_id":"arm1","state":"measured","health_time_ns":123,"health_age_ms":20,
      "motors":[{"id":1,"name":"shoulder_pan","label":"shoulder_pan","role":"joint","unit":"rad",
        "position":0.1,"position_time_ns":123,"position_age_ms":10,
        "goal_position":0.12,"reference_position":0.11,"torque_enabled":true,
        "temperature_c":34,"voltage_v":12.0,"current_a":null,
        "source_state":"measured","unavailable_fields":["current_a"]}]
    }
  }
}
```

GET `/api/resources` returns the `resources` object with top-level `schema_version:1`. Labels are optional configuration metadata, falling back to IDs/names. Native motor IDs come from the actual calibration/bus; mock IDs can be specified with `joints[].motor_id` / `gripper.motor_id`, otherwise null. Body units are `rad`; gripper is `percent`. No paths, tokens or raw deployment profiles are exposed. A `MODEL` capability means the API supports it; only `profile_ids` actually registered for that arm are selectable. Cartesian endpoints require a configured URDF; `tcp_frame` uses the existing solver default when omitted. They remain subject to existing model/frame/FK validation. Policy `profile_ids` require exact configured arm bindings.

`motor_status` state/source_state: `measured`, `simulated`, `stale`, `unavailable`. Goal is an actual motor goal register when available; reference is the owner's admitted command reference, never a fabricated copy of position. Read-only adapters may have no goal/reference. Missing values are null and listed in `unavailable_fields`. Native health uses the same existing four batched reads, at the unchanged 1-Hz cadence; positions reuse ordinary feedback reads. `current_a` is nullable and populated only by a real supported source; the current SO101 adapters supply null. No new current/load inference or register reads.

## GET /api/graph (also GET /api/v1/ros/graph)

Generic ROS graph metadata, no SO-Arm-specific discovery filter or assumed node roles:

```json
{
  "schema_version":1,"sequence":1,"captured_at_ns":123,"age_ms":10,
  "state":"ready","scope":"observer_visible_ros_domain","consistency":"best_effort_discovery",
  "nodes":[{"id":"node:/ns/node","name":"node","namespace":"/ns","fully_qualified_name":"/ns/node","instance_count":1}],
  "topics":[{"id":"topic:/data","name":"/data","types":["std_msgs/msg/String"],
    "publishers":[{"id":"publisher:gid","node_id":"node:/ns/node","type":"std_msgs/msg/String","gid":"gid","qos":{"reliability":"RELIABLE","durability":"VOLATILE","history":"KEEP_LAST","depth":10,"deadline_ns":"0","lifespan_ns":"0","liveliness":"AUTOMATIC","liveliness_lease_duration_ns":"0"}}],
    "subscriptions":[],
    "traffic":{"state":"not_sampled","sample_id":null,"messages":null,"bytes":null,"rate_hz":null,"bytes_per_second":null,"last_message_age_ms":null,"history":[],"reason":"No sampling requested"}}],
  "services":[{"id":"service:/get","name":"/get","types":["example_interfaces/srv/Trigger"],"servers":["node:/ns/node"],"clients":[]}],
  "actions":[{"id":"action:/run","name":"/run","types":["example_interfaces/action/Fibonacci"],"servers":["node:/ns/node"],"clients":[]}],
  "edges":[{"id":"publish:gid","kind":"publish","from":"node:/ns/node","to":"topic:/data","endpoint_id":"publisher:gid"}],
  "coverage":{"nodes":"complete","topics":"complete","services":"complete","actions":"complete"},
  "errors":[],
  "sampling":{"enabled":true,"max_topics":2,"max_duration_s":10,"payload_export":false}
}
```

Edges: `publish` node→topic, `subscribe` topic→node, `service_client` node→service, `service_server` service→node, `action_client` node→action, `action_server` action→node. Service/action QoS is not exposed by these discovery APIs; do not infer it. Endpoint QoS fields may be null/UNKNOWN. FQN collisions aggregate `instance_count`; endpoints retain discovered GIDs. Metadata/edges show discovered relationships, not proven liveness, receipt, execution or a healthy connection. Partial API discovery yields coverage `partial` and structured errors rather than a falsely complete graph.

Discovery refresh defaults to 2 seconds. Topic types/hidden action infrastructure are retained. Frontend may filter its view without losing discovery metadata.

## POST /api/graph/sample (also POST /api/v1/ros/sample)

Request `{"topic":"/data","duration_s":5}`. Response `{"schema_version":1,"accepted":true,"topic":"/data","sample_id":"...","state":"sampling"}`. Stop with `{"topic":"/data","stop":true}`. This controls only observer subscriptions, never robot operations.

## Local telemetry storage

The dashboard status response also enriches `upload` with `delivery_mode`
(`local` or `remote`), `storage_bytes` (database + WAL + SHM; null if unavailable),
`sample_period_s`, `retention_days` and `storage_limit_mb`. Local-only mode has
`queued: 0`, since no destination is configured; `remote_committed` retains its
actual acknowledgment count. File paths and collector credentials are omitted.
Storage and queue counters are omitted from the operator dashboard; this
metadata remains available to API consumers and does not change the ROS UploadStatus type.

## Continuous live watch

`POST /api/v1/ros/watch` uses the same gateway access policy. It adds continuous,
graph-wide observation alongside the unchanged finite-sample routes:

| Operation | JSON body | Meaning |
| --- | --- | --- |
| Acquire | `{"operation":"acquire","client_id":"UUID"}` | Acquire a browser-owned lease; same-client retries are idempotent. |
| Renew | `{"operation":"renew","lease_id":"OPAQUE_ID"}` | Extend this lease without replacing subscriptions/counter identities. |
| Release | `{"operation":"release","lease_id":"OPAQUE_ID"}` | Release only this lease; other clients retain their observations. |

Responses contain `schema_version`, `accepted`, `lease_id`, `expires_in_ms` and
`active` when successful. Rejections include `reason`/`code`; malformed requests
return JSON HTTP 400. Default lifetime is 12 seconds, with at most eight clients.
Browser renewal is approximately every four seconds, using local elapsed time.
No credentials or lease identifiers belong in URLs.

Graph snapshots add `watch` coverage/limits and `topics[].activity`, independent
of historical `traffic` finite samples. Activity includes `state`, `sample_id`,
`source`, cumulative message counts, nullable bytes/rates/ages, bounded `history`
and `publisher_counts` keyed by actual publisher GID. Unknown attribution is
reported separately. States are `watching`, `inactive`, `blocked`, `unavailable`,
`limited`, `throttled`, or `cleanup_pending`; watching with zero receipts is not
proof of a dead publisher. Passive existing subscriptions can supply data,
including compressed images, without duplicate subscriptions or serialization.

For an auxiliary `/_buf_<transport>` topic with a same-type base topic, existing
logical receipts may be attributed using an exact, globally unique publisher
GID. These records use `source: transport_gid_projection` and include
`projection.source_topic`, `source_sample_id`, `publisher_gids` and
`complete_source_coverage`. They count logical deliveries observed locally,
not transport packets or confirmed delivery to each subscriber. Bytes remain
unknown; rates/history are reused only when the matching GIDs cover every
source message. Direct observations take precedence. `watch.projected_topics`
counts these records; `direct_coverage` preserves pre-projection coverage.

The default graph view hides auxiliary buffer interfaces and their unresolved
endpoint placeholders under **Infrastructure**. Ordinary unresolved endpoints
remain visible; discovery JSON retains every original name, GID and edge.
Transport endpoints use descriptive display labels without guessing an owner.
Selecting a node highlights its complete path through each adjacent interface:
upstream publications for its subscriptions, downstream subscriptions for its
publications, and opposing action/service bindings. Live markers follow these
same highlighted paths only when advancing observations support them.

Counter snapshots also include `activity_clock_id` (stable observer-instance
UUID), `activity_clock_type: "observer_monotonic"` and
`activity_captured_at_ns` (decimal string). The capture is recorded immediately
after the live counters are copied. To measure an update interval, subtract two
captures only when observer ID and counter epoch match. Do not compare this
clock with a client's monotonic clock or treat it as packet time/network
latency. Existing `captured_at_ns` and `sequence` still describe topology
discovery; they do not determine the counter refresh interval.

`actions[].activity` references its standard hidden status/feedback topics and
their observations. GoalStatusArray may provide bounded counts by status, never
goal payloads or IDs. Feedback content is not decoded/exported. Goal requests,
results and receipt by individual clients are explicitly unobserved.

Additional observations are capped at 64 topics. Continuous monitoring admits
raw images, point clouds and audio when ROS type support is available. Each
source defaults to a rolling five-second 10,000-message budget, with a global
50,000-message limit. Message size and byte volume do not exclude a source.
Counters use received length, time and publisher identity without an additional
payload copy or serialization; middleware reception can still allocate data.
Histories retain 20 half-second buckets. Existing passive receipts are reused.
Auxiliary buffer channels use exact-GID projection from their logical source
instead of creating recursive adaptive subscriptions on internal channels.
Missing type support or unobservable sources remain explicitly unavailable.

Configure `graph_observer.watch` with `enabled`, `max_topics`, `max_clients`,
`lease_s` and `max_messages_per_window`. The legacy `max_bytes_per_window`
configuration is accepted for compatibility but no longer limits continuous
counting. Watch metadata reports byte ceilings as null and
`byte_limits_enforced: false`. Fixed global
ceilings are advertised in the snapshot. These bounds limit observer work;
they are not guarantees of transport delivery or zero publisher-side cost.

### Finite-sample bounds

Only explicitly selected topics are sampled, best-effort/volatile/depth 1, at most 2 concurrent topics and 10 seconds. Known high-bandwidth image/pointcloud/audio types are blocked; unknown/unavailable message types report unavailable. Serialized bytes are counted, never decoded/exported. Message/byte budgets terminate sampling early. No payload contents or secrets/command payloads are returned.

`traffic.state`: `not_sampled`, `sampling`, `complete`, `blocked`, `unavailable`. Active/completed samples include `sample_id`, `started_at_ns`, `ended_at_ns`, `messages`, `bytes`, `rate_hz`, `bytes_per_second`, `last_message_age_ms`, `reason` and at most 20 history buckets `{"at_ns":123,"messages":3,"bytes":200}`. Rates refer only to this local observer subscription, not global transport/delivery. Zero messages is observed zero during this sample, not proof the topic is dead. Default raw payload export is always false, including on command-bearing topics.

Optional configuration: `graph_observer: {refresh_s:2, sampling:{enabled:true, max_topics:2, max_duration_s:10}}`. Hard ceilings remain bounded. Motor labeling fields `label`, `joints[].label`, `gripper.label`, and simulated `motor_id` are additive metadata. No actuation scope is expanded.

## Telemetry validation and source provenance

Gripper position keeps its own `position_time_ns` and `position_age_ms`; newer body `JointState` timestamps do not refresh the gripper. A malformed diagnostic record does not replace a valid cache entry. `motor_status[arm_id].error` reports rejected telemetry and marks retained values stale; valid subsequent telemetry clears the error. Both root/nested shapes and numeric values are bounded/validated.

A native RMW may expose endpoint GIDs and QoS without a ROS node name/namespace. Those owners appear as `node:unresolved:<gid>` placeholders, with `namespace`, `fully_qualified_name`, `instance_count` null and `discovery_state:"unresolved_endpoint_owner"`. They are owner placeholders, not a count of known physical processes. Endpoint bindings remain present; topic coverage is partial. Service/action queries run only for identified node names. Metadata state can be initializing, ready, partial or stale.

Sampling counters include the terminating message; a custom oversized message can arrive before cancellation. Samples terminate at 2000 messages, 1 MiB observed bytes or a message over 64 KiB, in addition to duration/concurrency limits. These limits bound observer sampling/retained state, not global wire bandwidth. Failed subscription destruction remains tracked, consumes the concurrency budget and is retried; `sampling.cleanup_pending`, `sampling.cleanup_errors` and per-topic `traffic.cleanup_error` expose that state. Observer-loop exceptions are isolated/reported instead of silently killing discovery.

## Existing Cartesian INLINE HTTP payload

`POST /api/operate` supports the existing TRAJECTORY/INLINE Cartesian backend through:

```json
{"arm_id":"arm1","runtime_boot_id":"BOOT","controller_session":"SESSION","operator_session_id":"OPERATOR","operation_type":"TRAJECTORY","mode":"INLINE","payload_kind":"CARTESIAN_TARGET","cartesian":[{"frame_id":"arm1_base","position":[0.1,0.2,0.3],"orientation_mode":"POSITION_ONLY","orientation":[0,0,0,1],"duration_s":1.25}]}
```

There are 1..16 endpoints. Position is metres; quaternion order is XYZW. POSITION_ONLY and STRICT_POSE are the existing supported orientation modes. STRICT_POSE requires an orientation. Mixed joint/Cartesian bodies, malformed/nonfinite vectors and Cartesian STREAM reject. Registered-model/frame/IK/path limits and permission/operation checks remain authoritative. For incremental keyboard IK, use the separate [Manual calculation routes](manual-control-backend.md) and send their joint targets through DIRECT control. The INLINE endpoint does not provide a continuous Cartesian-path guarantee.

## Reloading the dashboard process

Static JS/CSS changes are read from disk on each request; reload the browser to load a new module. A change to the Python asset allowlist needs the dashboard process to load the new code. In a service launch using `respawn:=true`, gracefully terminate only the `dashboard_node` child after confirming no active operation/control authority; launch recreates it after the three-second respawn delay. This does not replace `lerobot_node` or its native worker. Verify their PID/runtime boot and permission/write count before and after.

Use the actual dashboard child in the service cgroup, identified by its full executable path; do not use a stale PID. Dashboard HTTP/WebSocket sessions reconnect. A full service restart is unnecessary for a dashboard-only route/module update. If foreground launch has `respawn:=false`, it does not recreate the node automatically.

## Automatic access for the owner

`web.trusted_network_access` defaults to true. Actual socket peers on loopback,
Tailscale IPv4 (`100.64.0.0/10`) or Tailscale IPv6 (`fd7a:115c:a1e0::/48`) access
the APIs/media and WebSockets without an operator token. Forwarded headers are
not identity. The browser connects automatically, with no credential form or
bootstrap secret; WebSockets use only the `soarm_station` subprotocol.

A valid bearer remains accepted for other clients. Setting the flag false
keeps bearer-only API access; a token is required for that configuration.
Trusted mode can start without a token. Empty bearer values never grant access.
Browser mutations/WebSockets keep same-origin checks. This change only affects
opening the dashboard; it does not enable motion, change actuator ownership or
resume an earlier operation.
