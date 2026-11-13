import test from "node:test";
import assert from "node:assert/strict";
import { StationClient } from "../web/transport.js";

const TOKEN = "station-test-token-12345678901234567890";
const copy = (value) => JSON.parse(JSON.stringify(value));
const flush = async () => {
  for (let i = 0; i < 35; i++) await Promise.resolve();
};
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((a, b) => {
    resolve = a;
    reject = b;
  });
  return { promise, resolve, reject };
};

class Clock {
  time = 0;
  next = 1;
  tasks = new Map();
  setTimeout = (fn, delay = 0) => {
    const id = this.next++;
    this.tasks.set(id, { at: this.time + delay, fn });
    return id;
  };
  clearTimeout = (id) => this.tasks.delete(id);
  now = () => this.time;
  async advance(ms) {
    const end = this.time + ms;
    await flush();
    for (let count = 0; count < 100000; count++) {
      const next = [...this.tasks]
        .filter(([, task]) => task.at <= end)
        .sort((a, b) => a[1].at - b[1].at)[0];
      if (!next) {
        this.time = end;
        await flush();
        return;
      }
      this.tasks.delete(next[0]);
      this.time = next[1].at;
      next[1].fn();
      await flush();
    }
    throw Error("Fake timer runaway");
  }
}

function fixture(options = {}) {
  const clock = new Clock(),
    calls = [],
    sockets = [],
    errors = [],
    events = [],
    states = [],
    connections = [],
    urls = new Set(),
    revoked = [];
  const status = {
    device_id: "mock-station",
    arms: {
      arm1: {
        arm_id: "arm1",
        runtime_boot_id: "boot1",
        controller_session: "session1",
        phase: "IDLE",
        permission: true,
        feedback_valid: true,
        feedback_age_ms: 3,
        gripper_percent: 40,
      },
    },
    joints: {
      arm1: { name: ["shoulder_pan", "elbow_flex"], position: [0, 0.1] },
    },
    cameras: ["overview"],
    upload: {},
    events: [],
  };
  const operations = new Map();
  let serial = 0,
    autoAck = true,
    route = null;
  const response = (body, code = 200) => ({
    ok: code >= 200 && code < 300,
    status: code,
    json: async () => copy(body),
    text: async () => (typeof body === "string" ? body : JSON.stringify(body)),
  });
  const grant = (op, extra = {}) => ({
    phase: "READY",
    stream_ready: true,
    arm_id: "arm1",
    runtime_boot_id: "boot1",
    controller_session: "session1",
    operation_id: op?.id || "op-manual",
    operation_epoch: 1,
    window_id: `window-${Math.floor(clock.time / 100)}`,
    window_valid_for_ms: 250,
    last_sequence: op?.lastSequence || 0,
    last_accepted_semantic_revision: op?.revision || 0,
    last_accepted_input_id: op?.inputId || "",
    last_applied_input_id: "",
    reason: op?.reason || "",
    ...extra,
  });
  class FakeWebSocket {
    readyState = 0;
    bufferedAmount = 0;
    sent = [];
    constructor(url, protocols) {
      this.url = url;
      this.protocols = protocols;
      sockets.push(this);
    }
    open() {
      this.readyState = 1;
      this.onopen?.({});
    }
    send(body) {
      assert.equal(this.readyState, 1);
      this.sent.push(JSON.parse(body));
    }
    message(value) {
      this.onmessage?.({
        data: value instanceof Blob ? value : JSON.stringify(value),
      });
    }
    close() {
      if (this.readyState === 3) return;
      this.readyState = 3;
      this.onclose?.({});
    }
  }
  const fetch = async (url, config) => {
    const call = {
      url,
      path: new URL(url).pathname,
      query: new URL(url).search,
      ...config,
      body: config.body === undefined ? undefined : JSON.parse(config.body),
    };
    calls.push(call);
    const dispatch = async () => {
      if (route) {
        const custom = route(call);
        if (custom !== undefined) return await custom;
      }
      if (call.path === "/api/status") return response(status);
      if (call.path === "/api/permission") {
        status.arms.arm1.permission = call.body.enable;
        return response({ accepted: true, permission: call.body.enable });
      }
      if (call.path === "/api/hold") {
        status.arms.arm1.permission = false;
        return response({ success: true, message: "held" });
      }
      if (call.path === "/api/operate") {
        const requestId = `request-${++serial}`;
        operations.set(requestId, {
          id: `operation-${serial}`,
          type: call.body.operation_type,
          mode: call.body.mode,
          done: false,
        });
        return response({ request_id: requestId });
      }
      if (call.path.startsWith("/api/operations/")) {
        const op = operations.get(
          decodeURIComponent(call.path.split("/").pop()),
        );
        return response({
          done: op.done,
          feedback: grant(op),
          result: op.done
            ? {
                result_code: "COMPLETED",
                execution_completed: true,
                task_outcome: "UNKNOWN",
                reason: "PATH_COMPLETE",
              }
            : null,
        });
      }
      if (call.path === "/api/stream") {
        const op = [...operations.values()].find(
          (x) => x.id === call.body.operation_id,
        );
        if (op && autoAck && call.body.frame_kind === "COMMAND") {
          op.revision = call.body.semantic_revision;
          op.inputId = call.body.input_id;
          op.lastSequence = call.body.sequence;
        }
        if (op && call.body.frame_kind === "END") op.done = true;
        return response({ forwarded: true });
      }
      return response({});
    };
    return new Promise((resolve, reject) => {
      if (config.signal.aborted) {
        reject(new DOMException("Aborted", "AbortError"));
        return;
      }
      config.signal.addEventListener(
        "abort",
        () => reject(new DOMException("Aborted", "AbortError")),
        { once: true },
      );
      dispatch().then(resolve, reject);
    });
  };
  const client = new StationClient({
    onState: (x) => states.push(copy(x)),
    onConnection: (x) => connections.push(x),
    onOperation: (x) => events.push(x),
    onError: (x) => errors.push(x),
    ...options,
    env: {
      fetch,
      WebSocket: FakeWebSocket,
      setTimeout: clock.setTimeout,
      clearTimeout: clock.clearTimeout,
      now: clock.now,
      origin: "https://station.test:8443",
      uuid: () => `uuid-${++serial}`,
      decodeImage: async () => {},
      createObjectURL: () => {
        const id = `blob:${++serial}`;
        urls.add(id);
        return id;
      },
      revokeObjectURL: (id) => {
        urls.delete(id);
        revoked.push(id);
      },
      afterPaint: (cb) => clock.setTimeout(cb, 0),
      ...options.env,
    },
  });
  return {
    client,
    clock,
    calls,
    sockets,
    errors,
    events,
    states,
    connections,
    status,
    operations,
    grant,
    response,
    urls,
    revoked,
    setRoute: (fn) => {
      route = fn;
    },
    setAutoAck: (value) => {
      autoAck = value;
    },
  };
}

const positions = () => ({ shoulder_pan: 0.05, elbow_flex: 0.15 });
const trajectory = () => ({ points: [{ time_s: 1, positions: positions() }] });
const direct = () => ({
  points: [{ time_s: 1, positions: positions(), gripper: 43 }],
});
const model = () => ({ model_steps: [{ instruction: "", duration_s: 2 }] });

test("authentication is explicit, relative-only, never stored in URLs or serialized client state", async () => {
  const f = fixture();
  await f.client.connect(TOKEN);
  assert.equal(f.calls[0].headers.Authorization, `Bearer ${TOKEN}`);
  assert.ok(!JSON.stringify(f.client).includes(TOKEN));
  await assert.rejects(
    f.client.request("https://other.test/api/status"),
    /relative/,
  );
  assert.equal(f.calls.filter((x) => x.path === "/api/permission").length, 0);
  await f.client.enable("arm1");
  assert.equal(
    f.calls.find((x) => x.path === "/api/permission").body.operator_session_id,
    f.client.operatorSessionId,
  );
  await f.client.disconnect();
});

test("status polling does not overlap and late disconnected responses do not update state", async () => {
  const f = fixture();
  await f.client.connect(TOKEN);
  const pending = deferred();
  f.setRoute((call) =>
    call.path === "/api/status" ? pending.promise : undefined,
  );
  await f.clock.advance(700);
  assert.equal(f.calls.filter((x) => x.path === "/api/status").length, 2);
  const count = f.states.length;
  await f.client.disconnect();
  pending.resolve(f.response({ device_id: "late", arms: {} }));
  await flush();
  assert.equal(f.states.length, count);
  assert.equal(f.client.connected, false);
});

for (const [type, payload, profile] of [
  ["DIRECT", direct, ""],
  ["TRAJECTORY", trajectory, ""],
  ["MODEL", model, "registered-test-profile"],
]) {
  test(`${type} INLINE preserves typed inputs and completes without another enable or hold`, async () => {
    const f = fixture();
    await f.client.connect(TOKEN);
    await f.client.enable("arm1");
    const op = await f.client.submit(
      "arm1",
      type,
      "INLINE",
      payload(),
      profile,
    );
    const sent = f.calls.find((x) => x.path === "/api/operate").body;
    assert.equal(sent.runtime_boot_id, "boot1");
    assert.equal(sent.controller_session, "session1");
    assert.equal(sent.operator_session_id, f.client.operatorSessionId);
    assert.equal(sent.mode, "INLINE");
    assert.equal(sent.operation_type, type);
    if (type === "DIRECT") assert.equal(sent.points[0].gripper, 43);
    if (type === "MODEL")
      assert.deepEqual(sent.model_steps, [{ instruction: "", duration_s: 2 }]);
    f.operations.get(op.requestId).done = true;
    await f.clock.advance(60);
    assert.equal(f.client.currentOperation, null);
    assert.equal(f.events.at(-1).state, "done");
    assert.equal(f.calls.filter((x) => x.path === "/api/hold").length, 0);
    await f.client.submit("arm1", type, "INLINE", payload(), profile);
    assert.equal(f.calls.filter((x) => x.path === "/api/permission").length, 1);
    await f.client.disconnect();
  });
}

for (const [type, payload, profile] of [
  ["MODEL", () => ({ instruction: "move the cup" }), "registered-test-profile"],
  ["TRAJECTORY", trajectory, ""],
]) {
  test(`${type} STREAM opens without an inline body, waits for READY, sends one intent and ACKs its revision`, async () => {
    const f = fixture();
    await f.client.connect(TOKEN);
    const wait = deferred();
    let waiting = true;
    f.setRoute((call) =>
      call.path.startsWith("/api/operations/") && waiting
        ? wait.promise
        : undefined,
    );
    const op = await f.client.submit(
      "arm1",
      type,
      "STREAM",
      payload(),
      profile,
    );
    await f.clock.advance(120);
    assert.equal(f.calls.filter((x) => x.path === "/api/stream").length, 0);
    const start = f.calls.find((x) => x.path === "/api/operate").body;
    assert.equal(start.points, undefined);
    assert.equal(start.instruction, undefined);
    assert.equal(start.duration_s, undefined);
    waiting = false;
    wait.resolve(
      f.response({
        done: false,
        feedback: f.grant(f.operations.get(op.requestId)),
      }),
    );
    await f.clock.advance(150);
    const commands = f.calls.filter(
      (x) => x.path === "/api/stream" && x.body.frame_kind === "COMMAND",
    );
    assert.equal(commands.length, 1);
    assert.equal(commands[0].body.semantic_revision, 1);
    assert.ok(
      f.events.some(
        (e) => e.event === "input-accepted" && e.lastAcceptedRevision === 1,
      ),
    );
    assert.ok(
      f.events
        .filter((e) => e.feedback)
        .every((e) => !("window_id" in e.feedback)),
    );
    const nextPayload =
      type === "MODEL"
        ? { instruction: "place the cup" }
        : { points: [{ time_s: 2, positions: positions() }] };
    const next = f.client.updateStream(nextPayload);
    await f.clock.advance(120);
    assert.equal((await next).semanticRevision, 2);
    assert.ok(
      f.calls.some(
        (x) =>
          x.path === "/api/stream" &&
          x.body.frame_kind === "HEARTBEAT" &&
          x.body.semantic_revision === undefined,
      ),
    );
    const ended = f.client.endOperation();
    await f.clock.advance(100);
    await ended;
    assert.equal(f.client.currentOperation, null);
    assert.ok(
      f.calls.some(
        (x) => x.path === "/api/stream" && x.body.frame_kind === "END",
      ),
    );
    await f.client.disconnect();
  });
}

test("DIRECT STREAM uses measured pickup, then latest user intent; no queued burst or reconnection", async () => {
  const f = fixture();
  await f.client.connect(TOKEN);
  const pending = f.client.submit("arm1", "DIRECT", "STREAM", {
    positions: positions(),
    gripper: 43,
  });
  const ws = f.sockets[0];
  ws.open();
  await pending;
  assert.deepEqual(ws.protocols, ["soarm_station", TOKEN]);
  assert.ok(!ws.url.includes(TOKEN));
  assert.equal(ws.sent.length, 1);
  assert.equal(ws.sent[0].duration_s, undefined);
  await f.clock.advance(30);
  assert.equal(ws.sent.length, 1);
  ws.message({ type: "grant", grant: f.grant() });
  await f.clock.advance(10);
  const pickup = ws.sent.at(-1);
  assert.deepEqual(pickup.body.positions, { shoulder_pan: 0, elbow_flex: 0.1 });
  ws.message({
    type: "grant",
    grant: f.grant(null, {
      phase: "ACTIVE",
      last_accepted_input_id: pickup.input_id,
      last_sequence: pickup.sequence,
    }),
  });
  await f.clock.advance(10);
  assert.deepEqual(ws.sent.at(-1).body.positions, positions());
  const changed = {
    positions: { shoulder_pan: 0.07, elbow_flex: 0.16 },
    gripper: 41,
  };
  await f.client.updateStream(changed);
  changed.positions.shoulder_pan = 999;
  ws.bufferedAmount = 10;
  const count = ws.sent.length;
  await f.clock.advance(50);
  assert.equal(ws.sent.length, count);
  ws.bufferedAmount = 0;
  await f.clock.advance(10);
  assert.equal(ws.sent.at(-1).body.positions.shoulder_pan, 0.07);
  const lastCount = ws.sent.length;
  await f.clock.advance(500);
  assert.ok(ws.sent.length - lastCount < 25);
  const stoppedAt = ws.sent.length;
  await f.clock.advance(100);
  assert.equal(ws.sent.length, stoppedAt);
  ws.close();
  await f.clock.advance(1000);
  assert.equal(f.sockets.length, 1);
  assert.equal(f.client.currentOperation, null);
  assert.equal(f.calls.filter((x) => x.path === "/api/permission").length, 0);
  await f.client.disconnect();
});

test("sparse retry keeps canonical input/revision, uses fresh transport identity, and stops after rejection", async () => {
  const f = fixture();
  await f.client.connect(TOKEN);
  f.setAutoAck(false);
  const op = await f.client.submit(
    "arm1",
    "MODEL",
    "STREAM",
    {},
    "registered-test-profile",
  );
  await f.clock.advance(60);
  const update = f.client.updateStream({ instruction: "same task" });
  const outcome = assert.rejects(update, /Unsupported task/);
  await f.clock.advance(350);
  const commands = f.calls.filter(
    (c) => c.path === "/api/stream" && c.body.frame_kind === "COMMAND",
  );
  assert.ok(commands.length >= 2);
  assert.equal(new Set(commands.map((c) => c.body.input_id)).size, 1);
  assert.equal(new Set(commands.map((c) => c.body.semantic_revision)).size, 1);
  assert.ok(commands[1].body.sequence > commands[0].body.sequence);
  assert.notEqual(commands[1].body.window_id, commands[0].body.window_id);
  f.operations.get(op.requestId).reason = "Unsupported task";
  await f.clock.advance(60);
  await outcome;
  const count = f.calls.filter(
    (c) => c.path === "/api/stream" && c.body.frame_kind === "COMMAND",
  ).length;
  await f.clock.advance(400);
  assert.equal(
    f.calls.filter(
      (c) => c.path === "/api/stream" && c.body.frame_kind === "COMMAND",
    ).length,
    count,
  );
  assert.ok(f.client.currentOperation); // Known rejected replacement does not cancel the previous accepted intent.
  await f.client.disconnect();
});

test("missing sparse ACK stops uncertain task with explicit hold instead of retaining/replaying it", async () => {
  const f = fixture({ ackTimeoutMs: 300 });
  await f.client.connect(TOKEN);
  f.setAutoAck(false);
  await f.client.submit("arm1", "TRAJECTORY", "STREAM");
  await f.clock.advance(60);
  const update = f.client.updateStream(trajectory());
  const outcome = assert.rejects(
    update,
    (error) => error.code === "ACK_TIMEOUT",
  );
  await f.clock.advance(400);
  await outcome;
  assert.equal(f.client.currentOperation, null);
  assert.ok(f.calls.some((c) => c.path === "/api/hold"));
  await f.client.disconnect();
});

test("lost operate response requests hold and never retries a possibly accepted INLINE action", async () => {
  const f = fixture({ requestTimeoutMs: 300 });
  await f.client.connect(TOKEN);
  const lost = deferred();
  f.setRoute((c) => (c.path === "/api/operate" ? lost.promise : undefined));
  const submission = f.client.submit("arm1", "DIRECT", "INLINE", direct());
  const outcome = assert.rejects(
    submission,
    (error) => error.code === "TIMEOUT",
  );
  await f.clock.advance(350);
  await outcome;
  assert.equal(f.calls.filter((c) => c.path === "/api/operate").length, 1);
  assert.equal(f.calls.filter((c) => c.path === "/api/hold").length, 1);
  assert.equal(f.client.currentOperation, null);
  await f.client.disconnect();
});

test("unconfirmed compensating hold is reported; definitive admission rejection does not stop another operation", async () => {
  const f = fixture({ requestTimeoutMs: 200 });
  await f.client.connect(TOKEN);
  f.setRoute((c) =>
    c.path === "/api/operate" ? f.response("BUSY", 400) : undefined,
  );
  await assert.rejects(
    f.client.submit("arm1", "DIRECT", "INLINE", direct()),
    /BUSY/,
  );
  assert.equal(f.calls.filter((c) => c.path === "/api/hold").length, 0);
  f.setRoute((c) =>
    c.path === "/api/operate"
      ? new Promise(() => {})
      : c.path === "/api/hold"
        ? f.response({ success: false })
        : undefined,
  );
  const p = f.client.submit("arm1", "DIRECT", "INLINE", direct());
  const rejected = assert.rejects(p);
  await f.clock.advance(250);
  await rejected;
  assert.ok(f.errors.some((e) => e.code === "HOLD_UNCONFIRMED"));
  await f.client.disconnect();
});

test("invalid payloads never silently invent gripper values or send unsupported trajectory fields", async () => {
  const f = fixture();
  await f.client.connect(TOKEN);
  await assert.rejects(
    f.client.submit("arm1", "DIRECT", "INLINE", trajectory()),
    /Gripper/,
  );
  await assert.rejects(
    f.client.submit("arm1", "TRAJECTORY", "INLINE", direct()),
    /Use DIRECT/,
  );
  await assert.rejects(
    f.client.submit("arm1", "DIRECT", "INLINE", {
      points: [{ time_s: 1, positions: { shoulder_pan: NaN }, gripper: 40 }],
    }),
    /joint-name|finite/,
  );
  await assert.rejects(
    f.client.submit("arm1", "TRAJECTORY", "STREAM", { cartesian: [] }),
    /Cartesian/,
  );
  await assert.rejects(
    f.client.submit(
      "arm1",
      "MODEL",
      "INLINE",
      { ...model(), points: trajectory().points },
      "registered-test-profile",
    ),
    /does not support/,
  );
  assert.equal(f.calls.filter((c) => c.path === "/api/operate").length, 0);
  await f.client.disconnect();
});

test("Cartesian INLINE carries exact finite endpoints only when the arm advertises the capability", async () => {
  const f = fixture();
  await f.client.connect(TOKEN);
  const endpoint = {
    frame_id: "arm1_base",
    position: [0.2, 0.01, 0.1],
    orientation_mode: "STRICT_POSE",
    orientation: [0, 0, 0, 1],
    duration_s: 2,
  };
  const payload = { payload_kind: "CARTESIAN_TARGET", cartesian: [endpoint] };
  await assert.rejects(
    f.client.submit("arm1", "TRAJECTORY", "INLINE", payload),
    (e) => e.code === "UNSUPPORTED_CAPABILITY",
  );
  assert.equal(f.calls.filter((c) => c.path === "/api/operate").length, 0);
  f.client.state.resources = {
    arms: [
      {
        id: "arm1",
        base_frame: "arm1_base",
        tcp_frame: "gripper_frame_link",
        capabilities: { cartesian_endpoints: true },
      },
    ],
  };
  await f.client.submit("arm1", "TRAJECTORY", "INLINE", payload);
  const sent = f.calls.find((c) => c.path === "/api/operate").body;
  assert.equal(sent.payload_kind, "CARTESIAN_TARGET");
  assert.equal(sent.points, undefined);
  assert.deepEqual(sent.cartesian, [endpoint]);
  endpoint.position[0] = 999;
  assert.equal(sent.cartesian[0].position[0], 0.2);
  await f.client.disconnect();
});

test("Cartesian validation rejects STREAM, mixed bodies, wrong frames, unsupported constraints and malformed vectors", async () => {
  const f = fixture();
  await f.client.connect(TOKEN);
  f.client.state.resources = {
    arms: [
      {
        id: "arm1",
        base_frame: "arm1_base",
        capabilities: { cartesian_endpoints: true },
      },
    ],
  };
  const target = () => ({
    frame_id: "arm1_base",
    position: [0.2, 0, 0.1],
    orientation_mode: "POSITION_ONLY",
    orientation: [0, 0, 0, 1],
    duration_s: 1,
  });
  const cases = [
    { mode: "STREAM", payload: { cartesian: [target()] } },
    { payload: { cartesian: [target()], points: [] } },
    { payload: { payload_kind: "JOINT_TRAJECTORY", cartesian: [target()] } },
    { payload: { cartesian: [{ ...target(), frame_id: "wrong_frame" }] } },
    { payload: { cartesian: [{ ...target(), position: [0.1, NaN, 0.2] }] } },
    { payload: { cartesian: [{ ...target(), position: [0.1, 0.2] }] } },
    {
      payload: {
        cartesian: [
          {
            ...target(),
            orientation_mode: "STRICT_POSE",
            orientation: [0, 0, 0, 0],
          },
        ],
      },
    },
    {
      payload: { cartesian: [{ ...target(), orientation_mode: "TOOL_AXIS" }] },
    },
    { payload: { cartesian: [{ ...target(), duration_s: 0 }] } },
    { payload: { cartesian: Array.from({ length: 17 }, target) } },
  ];
  for (const item of cases)
    await assert.rejects(
      f.client.submit(
        "arm1",
        "TRAJECTORY",
        item.mode || "INLINE",
        item.payload,
      ),
    );
  assert.equal(f.calls.filter((c) => c.path === "/api/operate").length, 0);
  await f.client.submit("arm1", "TRAJECTORY", "INLINE", {
    cartesian: [
      { frame_id: "arm1_base", position: [0.2, 0, 0.1], duration_s: 1 },
    ],
  });
  const sent = f.calls.find((c) => c.path === "/api/operate").body;
  assert.equal(sent.cartesian[0].orientation_mode, "POSITION_ONLY");
  assert.deepEqual(sent.cartesian[0].orientation, [0, 0, 0, 1]);
  await f.client.disconnect();
});

test("manual feedback stays current internally while UI state callbacks are decimated", async () => {
  const f = fixture();
  await f.client.connect(TOKEN);
  const opening = f.client.startManual("arm1");
  const ws = f.sockets[0];
  ws.open();
  await opening;
  const before = f.states.length;
  for (let i = 1; i <= 10; i++)
    ws.message({
      type: "state",
      state: { ...f.status.arms.arm1, sample_sequence: i },
      joints: {
        name: ["shoulder_pan", "elbow_flex"],
        position: [i / 1000, 0.1],
      },
    });
  assert.equal(f.client.state.arms.arm1.sample_sequence, 10);
  assert.equal(f.client.state.joints.arm1.position[0], 0.01);
  assert.equal(f.states.length, before);
  await f.clock.advance(99);
  assert.equal(f.states.length, before);
  await f.clock.advance(1);
  assert.equal(f.states.length, before + 1);
  await f.client.disconnect();
});

test("an enable response arriving after explicit disconnect is compensated by hold", async () => {
  const f = fixture();
  await f.client.connect(TOKEN);
  const wait = deferred();
  f.setRoute((c) => (c.path === "/api/permission" ? wait.promise : undefined));
  const permission = f.client.enable("arm1");
  const rejected = assert.rejects(permission, (e) => e.code === "CANCELED");
  await f.client.disconnect();
  wait.resolve(f.response({ accepted: true, permission: true }));
  await flush();
  await rejected;
  assert.ok(f.calls.some((c) => c.path === "/api/hold"));
  assert.equal(f.client.connected, false);
});

test("runtime boot change invalidates local operation and cannot regrant or resend it", async () => {
  const f = fixture();
  await f.client.connect(TOKEN);
  await f.client.submit(
    "arm1",
    "MODEL",
    "STREAM",
    { instruction: "" },
    "registered-test-profile",
  );
  await f.clock.advance(100);
  f.status.arms.arm1.runtime_boot_id = "boot2";
  await f.clock.advance(110);
  const count = f.calls.filter((c) => c.path === "/api/stream").length;
  assert.equal(f.client.currentOperation, null);
  await f.clock.advance(300);
  assert.equal(f.calls.filter((c) => c.path === "/api/stream").length, count);
  assert.equal(f.calls.filter((c) => c.path === "/api/operate").length, 1);
  await f.client.disconnect();
});

test("camera URLs decode before publication, retire after display boundary, and all release on close", async () => {
  const decoding = [];
  const f = fixture({
    env: {
      decodeImage: (url) => {
        const d = deferred();
        decoding.push({ url, ...d });
        return d.promise;
      },
    },
  });
  await f.client.connect(TOKEN);
  const frames = [];
  f.client.openCamera("overview", (frame) => frames.push(frame));
  const ws = f.sockets[0];
  ws.open();
  ws.message(new Blob(["one"]));
  await flush();
  assert.equal(frames.length, 0);
  decoding[0].resolve();
  await flush();
  assert.equal(frames.length, 1);
  const first = frames[0].url;
  ws.message(new Blob(["two"]));
  await flush();
  assert.ok(f.urls.has(first));
  decoding[1].resolve();
  await flush();
  assert.equal(frames.length, 2);
  assert.ok(f.urls.has(first));
  await f.clock.advance(0);
  assert.ok(!f.urls.has(first));
  ws.message(new Blob(["pending"]));
  await flush();
  f.client.closeCamera("overview");
  decoding[2].resolve();
  await flush();
  assert.equal(frames.length, 2);
  assert.equal(f.urls.size, 0);
  assert.equal(new Set(f.revoked).size, f.revoked.length);
  await f.client.disconnect();
});

test("draft validation shares submission checks without sending or acquiring permission", async () => {
  const f = fixture();
  await f.client.connect(TOKEN);
  const before = f.calls.length;
  const body = {
    points: [
      {
        time_s: 1,
        positions: { shoulder_pan: 0, elbow_flex: 0.1 },
        gripper: 40,
      },
    ],
  };
  assert.deepEqual(
    f.client.validatePayload("arm1", "DIRECT", "INLINE", body),
    body,
  );
  assert.throws(() =>
    f.client.validatePayload("arm1", "MODEL", "INLINE", {
      model_steps: [{ instruction: "task", duration_s: -1 }],
    }),
  );
  const target = {
    positions: { shoulder_pan: 0, elbow_flex: 0.1 },
    gripper: 40,
  };
  assert.equal(
    f.client.validatePayload("arm1", "DIRECT", "STREAM", target).gripper_unit,
    "percent",
  );
  for (const mode of ["INLINE", "STREAM"]) {
    assert.equal(
      f.client.validatePayload("arm1", "TRAJECTORY", mode, {
        points: [{ time_s: 1, positions: target.positions }],
      }).points.length,
      1,
    );
    const model =
      mode === "INLINE"
        ? { model_steps: [{ instruction: "task" }] }
        : { instruction: "task" };
    assert.deepEqual(
      f.client.validatePayload("arm1", "MODEL", mode, model),
      model,
    );
  }
  assert.equal(f.calls.length, before);
  assert.equal(f.sockets.length, 0);
  assert.equal(f.client.currentOperation, null);
  await f.client.disconnect();
});

test("a late rejected enable does not hold another controller's arm", async () => {
  const f = fixture();
  await f.client.connect(TOKEN);
  const wait = deferred();
  f.setRoute((c) => (c.path === "/api/permission" ? wait.promise : undefined));
  const request = f.client.enable("arm1");
  const canceled = assert.rejects(request, (e) => e.code === "CANCELED");
  await f.client.disconnect();
  wait.resolve(
    f.response({
      accepted: false,
      permission: true,
      reason: "Another controller owns permission",
    }),
  );
  await flush();
  await canceled;
  assert.equal(
    f.calls.some((c) => c.path === "/api/hold"),
    false,
  );
});

test("private station opens without a token and does not enable control", async () => {
  const f = fixture();
  await f.client.connect();
  assert.equal(f.client.connected, true);
  assert.equal(f.calls[0].headers.Authorization, undefined);
  assert.equal(
    f.calls.some(
      (c) => c.path === "/api/permission" || c.path === "/api/operate",
    ),
    false,
  );
  f.client.openCamera("overview");
  assert.deepEqual(f.sockets[0].protocols, ["soarm_station"]);
  await f.client.enable("arm1");
  const opening = f.client.startManual("arm1");
  assert.deepEqual(f.sockets[1].protocols, ["soarm_station"]);
  f.sockets[1].open();
  await opening;
  await f.client.disconnect();
  assert.ok(f.calls.some((c) => c.path === "/api/hold"));
  assert.ok(f.calls.every((c) => c.headers.Authorization === undefined));
});

test("tokenless error text is retained without empty-string redaction", async () => {
  const f = fixture();
  await f.client.connect();
  f.setRoute((c) =>
    c.path === "/api/history"
      ? f.response("History unavailable", 400)
      : undefined,
  );
  await assert.rejects(
    f.client.request("/api/history"),
    (e) => e.message === "History unavailable",
  );
  await f.client.disconnect();
});
