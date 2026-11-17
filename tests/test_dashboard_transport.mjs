import test from "node:test";
import assert from "node:assert/strict";
import { StationClient, StationError } from "../web/transport.js";

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
        const requestId = call.body.request_id;
        operations.set(requestId, {
          id: `operation-${++serial}`,
          type: call.body.operation_type,
          mode: call.body.mode,
          principal:call.body.operator_session_id,
          done: false,
        });
        return response({ request_id: requestId });
      }
      if(call.path.startsWith('/api/operations/') && call.path.endsWith('/cancel')) {
        const id=decodeURIComponent(call.path.split('/').at(-2)),op=operations.get(id);
        if(op && op.principal!==call.body.operator_session_id)return response({accepted:false,reason:'Owner mismatch'},403);
        if(op)op.done=true;
        return response({accepted:true,done:true});
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

test('disconnect releases only its captured idle grant with keepalive for page exit',async()=>{
  const f=fixture();await f.client.connect(TOKEN);await f.client.enable('arm1');
  const principal=f.client.operatorSessionId;
  await f.client.disconnect();
  const hold=f.calls.find(call=>call.path==='/api/hold');
  assert.equal(hold.keepalive,true);
  assert.deepEqual(hold.body,{arm_id:'arm1',controller_session:'session1',operator_session_id:principal,operation_id:null});
  assert.equal(f.status.arms.arm1.permission,false);assert.equal(f.client._ownedGrants.size,0);
  assert(!f.calls.filter(call=>call.path==='/api/status').some(call=>call.keepalive));
});

test('public permission alone is not proof that this browser owns the current grant',async()=>{
  const f=fixture();await f.client.connect(TOKEN);
  assert.equal(f.client.state.arms.arm1.permission,true);assert.equal(f.client.ownsPermission('arm1'),false);
  await f.client.enable('arm1');assert.equal(f.client.ownsPermission('arm1'),true);
  f.status.arms.arm1.controller_session='session2';await f.client.refresh();
  assert.equal(f.client.ownsPermission('arm1'),false);assert.equal(f.client._ownedGrants.size,0);
  await f.client.disconnect();assert(!f.calls.some(call=>call.path==='/api/hold'));
});

test('disconnect closes its old WS before idle cleanup and cannot stop a newer active same-principal operation',async()=>{
  const f=fixture();await f.client.connect(TOKEN);await f.client.enable('arm1');
  const opening=f.client.startManual('arm1'),ws=f.sockets[0];ws.open();await opening;
  const principal=f.client.operatorSessionId,native={permission:true,operation_id:'replacement'};
  f.setRoute(call=>{
    if(call.path!=='/api/hold')return;
    assert.equal(ws.readyState,3);assert.equal(call.body.operator_session_id,principal);
    const matches=Object.hasOwn(call.body,'operation_id') && call.body.operation_id===native.operation_id;
    if(!Object.hasOwn(call.body,'operation_id') || matches)native.permission=false;
    return f.response({success:matches,message:'Operation changed'});
  });
  await f.client.disconnect();assert.equal(native.permission,true);
  assert.equal(f.calls.filter(call=>call.path==='/api/hold').length,1);
  assert.equal(f.calls.find(call=>call.path==='/api/hold').body.operation_id,null);
});

test('a delayed disconnect cleanup keeps its original identity across a reconnect and new active operation',async()=>{
  const f=fixture();await f.client.connect(TOKEN);await f.client.enable('arm1');
  const original=f.client.operatorSessionId,gate=deferred();let originalBody;
  f.setRoute(call=>{
    if(call.path==='/api/hold'&&!originalBody){originalBody=call.body;return gate.promise;}
  });
  const retiring=f.client.disconnect();await flush();
  await f.client.connect(TOKEN);await f.client.enable('arm1');
  const opening=f.client.startManual('arm1'),ws=f.sockets[0];ws.open();await opening;
  const replacement=f.client.currentOperation.localId,newPrincipal=f.client.operatorSessionId;
  gate.resolve(f.response({success:false,message:'Owner changed'}));await retiring;
  assert.notEqual(newPrincipal,original);assert.equal(originalBody.operator_session_id,original);
  assert.equal(originalBody.controller_session,'session1');assert.equal(originalBody.operation_id,null);
  assert.equal(f.client.currentOperation.localId,replacement);assert.equal(ws.readyState,1);
  assert.equal(f.client._ownedGrants.get('arm1').operator_session_id,newPrincipal);
  assert.equal(f.client.ownsPermission('arm1'),true);assert.equal(f.client.connected,true);
  await f.client.disconnect();
});

test('disconnect cancels the exact HTTP request before attempting captured idle-grant cleanup',async()=>{
  const f=fixture();await f.client.connect(TOKEN);await f.client.enable('arm1');
  const op=await f.client.submit('arm1','DIRECT','INLINE',direct()),gate=deferred();let canceled=false;
  f.setRoute(call=>{
    if(call.path.endsWith('/cancel'))return gate.promise.then(()=>{canceled=true;return f.response({accepted:true,done:false});});
    if(call.path==='/api/hold'){assert.equal(canceled,true);assert.equal(call.body.operation_id,null);}
  });
  const stopping=f.client.disconnect();await flush();
  assert.equal(f.client.currentOperation,null);assert(!f.calls.some(call=>call.path==='/api/hold'));
  const cancel=f.calls.find(call=>call.path.endsWith('/cancel'));
  assert.equal(cancel.path,`/api/operations/${op.requestId}/cancel`);
  gate.resolve();await stopping;assert.equal(f.calls.filter(call=>call.path==='/api/hold').length,1);
});

test('retiring an old local operation handle never closes a replacement socket',async()=>{
  const f=fixture();await f.client.connect(TOKEN);
  const first=f.client.startManual('arm1'),old=f.sockets[0];old.open();const oldHandle=(await first).localId;
  assert.ok(oldHandle);await f.client.retireOperation(oldHandle);assert.equal(old.readyState,3);
  const second=f.client.startManual('arm1'),current=f.sockets[1];current.open();const next=await second;
  assert.equal(await f.client.retireOperation(oldHandle),false);
  assert.equal(current.readyState,1);assert.equal(f.client.currentOperation.localId,next.localId);
  assert(!f.calls.some(call=>call.path==='/api/hold'));
  await f.client.disconnect();
});

test('successful finite completion retains its healthy grant until explicit disconnect',async()=>{
  const f=fixture();await f.client.connect(TOKEN);await f.client.enable('arm1');
  const op=await f.client.submit('arm1','DIRECT','INLINE',direct());
  f.operations.get(op.requestId).done=true;await f.clock.advance(60);
  assert.equal(f.client.currentOperation,null);assert.equal(f.client.ownsPermission('arm1'),true);
  assert(!f.calls.some(call=>call.path==='/api/hold'||call.path.endsWith('/cancel')));
  await f.client.disconnect();assert.equal(f.status.arms.arm1.permission,false);
  assert.equal(f.calls.find(call=>call.path==='/api/hold').body.operation_id,null);
});

test('a delayed idle-release acknowledgment cannot overwrite newer grant or operation state',async()=>{
  const f=fixture();await f.client.connect(TOKEN);await f.client.enable('arm1');
  const scope=f.client._ownedGrants.get('arm1'),gate=deferred();
  f.setRoute(call=>call.path==='/api/hold'?gate.promise:undefined);
  const release=f.client.releaseIdlePermission('arm1',scope);
  await f.client.enable('arm1');const opening=f.client.startManual('arm1'),ws=f.sockets[0];ws.open();const op=await opening;
  gate.resolve(f.response({success:true}));await release;
  assert.equal(f.client.currentOperation.localId,op.localId);assert.equal(f.client.ownsPermission('arm1'),true);
  assert.equal(f.client.state.arms.arm1.permission,true);assert.equal(ws.readyState,1);
  f.setRoute(null);await f.client.disconnect();
});

for (const accepted of [true,false]) test(`scoped handoff stops local targets before Hold and closes socket after response (${accepted})`,async()=>{
  const f=fixture();await f.client.connect(TOKEN);await f.client.enable('arm1');
  const opening=f.client.startManual('arm1'),ws=f.sockets[0];ws.open();await opening;
  ws.message({type:'grant',grant:f.grant(null)});await flush();
  const gate=deferred();let arrived=false;
  f.setRoute(call=>{
    if(call.path==='/api/hold'){
      arrived=true;assert.equal(ws.readyState,1,'EOF must not clear owner before conditional hold');
      assert.equal(call.body.operation_id,'op-manual');return gate.promise;
    }
  });
  const holding=f.client.hold('arm1',{controller_session:'session1',operator_session_id:f.client.operatorSessionId,operation_id:'op-manual'});
  await flush();assert(arrived);assert.equal(f.client.currentOperation,null);
  const sent=ws.sent.length;await f.clock.advance(100);assert.equal(ws.sent.length,sent);
  gate.resolve(f.response({success:accepted,message:'Owner changed'}));
  if(accepted)await holding;else await assert.rejects(holding,/Owner changed/);
  assert.equal(ws.readyState,3,'failed requests also close only the captured old socket');
});

test('a late scoped Hold never closes a replacement local stream on the same arm',async()=>{
  const f=fixture();await f.client.connect(TOKEN);await f.client.enable('arm1');
  const opening=f.client.startManual('arm1'),ws=f.sockets[0];ws.open();await opening;
  ws.message({type:'grant',grant:f.grant(null,{operation_id:'new-op'})});await flush();
  f.setRoute(call=>call.path==='/api/hold'?f.response({success:false,message:'Operation changed'}):undefined);
  await assert.rejects(f.client.hold('arm1',{controller_session:'session1',operator_session_id:f.client.operatorSessionId,operation_id:'old-op'}),/Operation changed/);
  assert.equal(ws.readyState,1);assert.equal(f.client.currentOperation.operationId,'new-op');
});

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
  assert.equal(ws.sent[0].input_semantics, "persistent_target");
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

test("browser manual intent remains connected across an idle grant gap without replaying expired frames", async () => {
  const f = fixture();
  await f.client.connect(TOKEN);
  const pending = f.client.startManual("arm1");
  const ws = f.sockets[0];
  ws.open();
  await pending;
  assert.equal(ws.sent[0].input_semantics, "persistent_target");
  ws.message({ type: "grant", grant: f.grant() });
  await f.clock.advance(10);
  const pickup = ws.sent.at(-1);
  ws.message({ type: "grant", grant: f.grant(null, {
    phase: "ACTIVE", last_accepted_input_id: pickup.input_id, last_sequence: pickup.sequence,
  }) });
  f.client.setManualTarget({ positions: positions(), gripper: 42 });
  await f.clock.advance(500);
  const stoppedSendingAt = ws.sent.length;
  await f.clock.advance(1000);
  assert.equal(ws.sent.length, stoppedSendingAt, "an expired grant must not authorize more frames");
  assert.equal(f.client.currentOperation?.transport, "websocket");
  assert.equal(ws.readyState, 1, "an idle persistent target does not close its control session");
  ws.message({ type: "grant", grant: f.grant(null, { phase: "ACTIVE" }) });
  await f.clock.advance(10);
  const fresh = ws.sent.at(-1);
  assert.deepEqual(fresh.body.positions, positions());
  assert.notEqual(fresh.window_id, pickup.window_id);
  assert.ok(fresh.sequence > pickup.sequence);
  assert.equal(f.calls.filter(x => x.path === "/api/permission").length, 0);
  ws.close();
  await f.clock.advance(1000);
  assert.equal(f.client.currentOperation, null);
  assert.equal(f.sockets.length, 1, "closed sockets must not reconnect or rearm");
});

test("status poll degradation reports established control connectivity without closing its socket", async () => {
  const f = fixture();
  await f.client.connect(TOKEN);
  const pending=f.client.startManual("arm1"),ws=f.sockets[0];
  ws.open();await pending;
  f.setRoute(call=>call.path==="/api/status"?f.response("status request delayed",503):undefined);
  await f.clock.advance(110);
  assert.deepEqual(f.connections.at(-1),{
    state:"degraded",reason:"status request delayed",connected:true,controlSocketOpen:true,
  });
  assert.equal(f.client.currentOperation.socketOpen,true);
  assert.equal(ws.readyState,1);
  ws.close();
  assert.equal(f.client.currentOperation,null);
  await f.client.disconnect();
  assert.equal(f.connections.at(-1).connected,false);
  assert.equal(f.connections.at(-1).controlSocketOpen,false);
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

test("missing sparse ACK retires only the unconfirmed update while scoped status and the operation continue", async () => {
  const f = fixture({ ackTimeoutMs: 300 });
  await f.client.connect(TOKEN);
  f.setAutoAck(false);
  const operation=await f.client.submit("arm1", "TRAJECTORY", "STREAM");
  await f.clock.advance(60);
  const update = f.client.updateStream(trajectory());
  const outcome = assert.rejects(
    update,
    (error) => error.code === "ACK_TIMEOUT",
  );
  await f.clock.advance(400);
  await outcome;
  assert.equal(f.client.currentOperation.requestId,operation.requestId);
  assert.equal(f.client.currentOperation.pendingInputId,null);
  const commands=f.calls.filter(c=>c.path==='/api/stream'&&c.body.frame_kind==='COMMAND');
  await f.clock.advance(450);
  assert.equal(f.calls.filter(c=>c.path==='/api/stream'&&c.body.frame_kind==='COMMAND').length,commands.length);
  assert.ok(f.calls.some(c=>c.path==='/api/stream'&&c.body.frame_kind==='HEARTBEAT'));
  assert(!f.calls.some(c=>c.path==='/api/hold'||c.path.endsWith('/cancel')));
  const last=commands.at(-1).body,server=f.operations.get(operation.requestId);
  Object.assign(server,{inputId:last.input_id,revision:last.semantic_revision});
  await f.clock.advance(60);
  assert.equal(f.client.currentOperation.lastAcceptedRevision,last.semantic_revision,'late ACK remains observable');
  await f.client.disconnect();
});

test("lost operate response cancels its known request ID and never replays or holds the arm", async () => {
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
  const sent=f.calls.find(c=>c.path==='/api/operate').body;
  const canceled=f.calls.find(c=>c.path===`/api/operations/${sent.request_id}/cancel`);
  assert.equal(canceled.body.operator_session_id,sent.operator_session_id);
  assert.equal(canceled.keepalive,true);
  assert.equal(f.calls.filter((c) => c.path === "/api/hold").length, 0);
  assert.equal(f.client.currentOperation, null);
  await f.client.disconnect();
});

test("unconfirmed scoped cancellation is reported; admission rejection never stops another operation", async () => {
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
      : c.path.endsWith('/cancel')
        ? f.response({ accepted: false, reason:'Cancellation unavailable' })
        : undefined,
  );
  const p = f.client.submit("arm1", "DIRECT", "INLINE", direct());
  const rejected = assert.rejects(p);
  await f.clock.advance(250);
  await rejected;
  assert.ok(f.errors.some((e) => e.code === "CANCEL_UNCONFIRMED"));
  assert.equal(f.calls.filter(c=>c.path==='/api/hold').length,0);
  await f.client.disconnect();
});

test('temporary operation polling failures retain ownership and retry the same request ID',async()=>{
  const f=fixture();await f.client.connect(TOKEN);let polls=0;
  f.setRoute(c=>c.path.startsWith('/api/operations/')&&c.method==='GET'&&++polls===1?f.response('Temporary failure',503):undefined);
  const op=await f.client.submit('arm1','MODEL','STREAM',{},'model-a');
  await f.clock.advance(180);
  assert.equal(f.client.currentOperation.requestId,op.requestId);assert.equal(f.client.connected,true);
  assert.ok(polls>=2);assert.ok(f.events.some(e=>e.event==='transport-delayed'));
  assert(!f.calls.some(c=>c.path==='/api/hold'||c.path.endsWith('/cancel')));
  f.operations.get(op.requestId).done=true;await f.clock.advance(60);
  assert.equal(f.client.currentOperation,null,'an actual terminal result still retires the operation');
  await f.client.disconnect();
});

test('a transient sparse POST failure retries the existing input identity before ACK without restarting the operation',async()=>{
  const f=fixture();await f.client.connect(TOKEN);
  const op=await f.client.submit('arm1','TRAJECTORY','STREAM');await f.clock.advance(60);
  let first=true;
  f.setRoute(c=>{if(c.path==='/api/stream'&&c.body.frame_kind==='COMMAND'&&first){first=false;return Promise.reject(new TypeError('Temporary network loss'));}});
  const update=f.client.updateStream(trajectory());await f.clock.advance(400);await update;
  const commands=f.calls.filter(c=>c.path==='/api/stream'&&c.body.frame_kind==='COMMAND');
  assert.equal(commands.length,2);assert.equal(commands[0].body.input_id,commands[1].body.input_id);
  assert.equal(commands[0].body.semantic_revision,commands[1].body.semantic_revision);
  assert.ok(commands[1].body.sequence>commands[0].body.sequence);
  assert.equal(f.client.currentOperation.requestId,op.requestId);
  assert(!f.calls.some(c=>c.path==='/api/hold'||c.path.endsWith('/cancel')));
  await f.client.disconnect();
});

test('late HTTP admission after replacement cancels only the original request',async()=>{
  const f=fixture();await f.client.connect(TOKEN);const gate=deferred();let oldBody;
  f.setRoute(c=>{if(c.path==='/api/operate'&&!oldBody){oldBody=c.body;return gate.promise;}});
  const first=f.client.submit('arm1','DIRECT','INLINE',direct());
  const retired=assert.rejects(first,e=>e.code==='CANCELED');
  f.client._stopLocalOperation('stopped','Retired test intent');
  const next=await f.client.submit('arm1','DIRECT','INLINE',direct());
  gate.resolve(f.response({request_id:oldBody.request_id}));await retired;await flush();
  assert.equal(f.client.currentOperation.requestId,next.requestId);
  const cancels=f.calls.filter(c=>c.path.endsWith('/cancel'));
  assert.equal(cancels.length,1);assert.equal(cancels[0].path,`/api/operations/${oldBody.request_id}/cancel`);
  assert(!f.calls.some(c=>c.path==='/api/hold'));
  await f.client.disconnect();
});

test('late cancellation of a failed HTTP operation cannot retire replacement permission or execution',async()=>{
  const f=fixture();await f.client.connect(TOKEN);await f.client.enable('arm1');
  const first=await f.client.submit('arm1','DIRECT','INLINE',direct()),old=f.client._active,gate=deferred();
  f.setRoute(c=>c.path===`/api/operations/${first.requestId}/cancel`?gate.promise:undefined);
  f.client._failOperation(old,new StationError('Definitive old error','CONTROL_ERROR'));
  await f.client.enable('arm1');const next=await f.client.submit('arm1','DIRECT','INLINE',direct());
  gate.resolve(f.response({accepted:true,done:true}));await flush();
  assert.equal(f.client.currentOperation.requestId,next.requestId);assert.equal(f.client._ownedArms.has('arm1'),true);
  f.client._failOperation(old,new StationError('Late duplicate failure','CONTROL_ERROR'));await flush();
  assert.equal(f.client.currentOperation.requestId,next.requestId);
  assert.equal(f.calls.filter(c=>c.path.endsWith('/cancel')).length,1);
  assert(!f.calls.some(c=>c.path==='/api/hold'));
  await f.client.disconnect();
});

test('persistent manual pickup waits through missing measurements and PAUSED resumes with a fresh sample',async()=>{
  const f=fixture();await f.client.connect(TOKEN);
  const pending=f.client.startManual('arm1');const ws=f.sockets[0];ws.open();await pending;
  f.status.arms.arm1.feedback_valid=false;f.client.state.arms.arm1.feedback_valid=false;
  ws.message({type:'grant',grant:f.grant(null,{phase:'PAUSED'})});
  await f.clock.advance(30);
  assert.equal(ws.sent.filter(message=>message.frame_kind==='COMMAND').length,0);
  assert.equal(f.client.currentOperation.state,'paused');assert.equal(ws.readyState,1);
  f.status.arms.arm1.feedback_valid=true;f.client.state.arms.arm1.feedback_valid=true;
  ws.message({type:'grant',grant:f.grant(null,{phase:'PAUSED'})});await f.clock.advance(20);
  assert.ok(ws.sent.some(message=>message.frame_kind==='COMMAND'));
  assert(!f.calls.some(c=>c.path==='/api/hold'||c.path.endsWith('/cancel')));
  await f.client.disconnect();
});

test('per-packet rejection retains manual connection; actual terminal error closes only that owned socket',async()=>{
  const f=fixture();await f.client.connect(TOKEN);
  const opening=f.client.startManual('arm1'),ws=f.sockets[0];ws.open();await opening;
  ws.message({type:'grant',grant:f.grant()});await f.clock.advance(20);
  const pickup=ws.sent.find(message=>message.frame_kind==='COMMAND');
  ws.message({type:'grant',grant:f.grant(null,{last_accepted_input_id:pickup.input_id})});
  ws.message({type:'input_rejected',reason:'Malformed previous frame',sequence:pickup.sequence});
  f.client.setManualTarget({positions:positions(.7),gripper:35});await f.clock.advance(20);
  const next=ws.sent.filter(message=>message.frame_kind==='COMMAND').at(-1);
  assert.equal(ws.readyState,1);assert.notEqual(next.input_id,pickup.input_id);assert.ok(next.sequence>pickup.sequence);
  assert.deepEqual(next.body.positions,positions(.7));
  assert.ok(f.events.some(e=>e.event==='input-rejected'));assert.ok(f.client.currentOperation);
  ws.message({type:'error',reason:'Native owner failed'});await flush();
  assert.equal(ws.readyState,3);assert.equal(f.client.currentOperation,null);
  await f.clock.advance(500);assert.equal(f.sockets.length,1,'actual disconnect never auto-reopens');
  assert(!f.calls.some(c=>c.path==='/api/hold'||c.path.endsWith('/cancel')));
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

test("an enable response arriving after explicit disconnect is compensated by captured idle-only cleanup", async () => {
  const f = fixture();
  await f.client.connect(TOKEN);
  const wait = deferred();
  f.setRoute((c) => (c.path === "/api/permission" ? wait.promise : undefined));
  const permission = f.client.enable("arm1");
  const principal=f.client.operatorSessionId;
  const rejected = assert.rejects(permission, (e) => e.code === "CANCELED");
  await f.client.disconnect();
  wait.resolve(f.response({ accepted: true, permission: true }));
  await flush();
  await rejected;
  assert.ok(f.calls.some((c) => c.path === "/api/hold"));
  assert.deepEqual(f.calls.find(c=>c.path==='/api/hold').body,
    {arm_id:'arm1',controller_session:'session1',operator_session_id:principal,operation_id:null});
  assert.equal(f.status.arms.arm1.permission,false);
  assert.equal(f.client.connected, false);
});

test('late accepted permission cleanup uses the original principal and cannot stop a replacement after reconnect',async()=>{
  const f=fixture();await f.client.connect(TOKEN);const gate=deferred();let firstPermission=true;
  const original=f.client.operatorSessionId;
  f.setRoute(call=>{
    if(call.path==='/api/permission'&&firstPermission){firstPermission=false;return gate.promise;}
    if(call.path==='/api/hold'&&call.body.operator_session_id===original) {
      assert.equal(call.body.operation_id,null);return f.response({success:false,message:'Owner changed'});
    }
  });
  const enabling=f.client.enable('arm1'),canceled=assert.rejects(enabling,e=>e.code==='CANCELED');
  await f.client.disconnect();await f.client.connect(TOKEN);await f.client.enable('arm1');
  const newPrincipal=f.client.operatorSessionId,opening=f.client.startManual('arm1'),ws=f.sockets[0];ws.open();const op=await opening;
  gate.resolve(f.response({accepted:true,permission:true}));await canceled;
  const hold=f.calls.find(call=>call.path==='/api/hold');
  assert.equal(hold.body.operator_session_id,original);assert.notEqual(hold.body.operator_session_id,newPrincipal);
  assert.equal(f.client.currentOperation.localId,op.localId);assert.equal(ws.readyState,1);
  assert.equal(f.client.ownsPermission('arm1'),true);
  await f.client.disconnect();
});

test('a controller-session change during enable neither installs stale grant metadata nor cleans up the new session',async()=>{
  const f=fixture();await f.client.connect(TOKEN);const gate=deferred();
  f.setRoute(call=>call.path==='/api/permission'?gate.promise:call.path==='/api/hold'?f.response({success:false,message:'Owner changed'}):undefined);
  const enabling=f.client.enable('arm1'),canceled=assert.rejects(enabling,e=>e.code==='CANCELED');
  f.status.arms.arm1.controller_session='replacement-session';f.status.arms.arm1.runtime_boot_id='replacement-boot';
  await f.client.refresh();gate.resolve(f.response({accepted:true,permission:true}));await canceled;
  assert.equal(f.client.state.arms.arm1.controller_session,'replacement-session');
  assert.equal(f.client._ownedGrants.size,0);
  assert.equal(f.calls.find(call=>call.path==='/api/hold').body.controller_session,'session1');
  await f.client.disconnect();
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
  f.client.openCamera("overview", (frame) => {
    if (frame.state === "frame") frames.push(frame);
  });
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

test("camera connection drops recover without restarting or holding manual control", async () => {
  const f = fixture();
  await f.client.connect();
  const opening = f.client.startManual("arm1");
  const control = f.sockets[0];
  control.open();
  await opening;
  const states = [];
  f.client.openCamera("overview", (x) => states.push(x));
  const camera = f.sockets[1];
  camera.open();
  const before = f.client.currentOperation;
  camera.close();
  assert.equal(states.at(-1).state, "reconnecting");
  await f.clock.advance(500);
  assert.equal(f.sockets.length, 3);
  const replacement = f.sockets[2];
  replacement.open();
  replacement.message(new Blob(["new-frame"]));
  await flush();
  assert.equal(states.at(-1).state, "frame");
  assert.equal(f.client.currentOperation.operationId, before.operationId);
  assert.equal(control.readyState, 1);
  assert.equal(
    f.calls.some((c) => c.path === "/api/hold" || c.path === "/api/permission"),
    false,
  );
  await f.client.disconnect();
});

test("a silent camera retries and explicit close cancels all further retries", async () => {
  const f = fixture();
  await f.client.connect();
  const states = [];
  const stop = f.client.openCamera("overview", (x) => states.push(x));
  f.sockets[0].open();
  await f.clock.advance(5000);
  assert.equal(states.at(-1).state, "reconnecting");
  await f.clock.advance(500);
  assert.equal(f.sockets.length, 2);
  stop();
  await f.clock.advance(15000);
  assert.equal(f.sockets.length, 2);
  await f.client.disconnect();
});

test("camera retry stops on dashboard disconnect and never reopens controls", async () => {
  const f = fixture();
  await f.client.connect();
  f.client.openCamera("overview");
  f.sockets[0].close();
  await f.client.disconnect();
  await f.clock.advance(15000);
  assert.equal(f.sockets.length, 1);
  assert.equal(f.urls.size, 0);
});

test("a late camera decode from a dropped socket cannot replace its new stream", async () => {
  const pending = deferred();
  let decodeCount = 0;
  const f = fixture({
    env: {
      decodeImage: () =>
        ++decodeCount === 1 ? pending.promise : Promise.resolve(),
    },
  });
  await f.client.connect();
  const frames = [];
  f.client.openCamera("overview", (x) => {
    if (x.state === "frame") frames.push(x);
  });
  f.sockets[0].message(new Blob(["old-frame"]));
  await flush();
  const oldUrl = [...f.urls][0];
  f.sockets[0].close();
  await f.clock.advance(500);
  f.sockets[1].message(new Blob(["new-frame"]));
  pending.resolve();
  await flush();
  assert.equal(frames.length, 1);
  assert.notEqual(frames[0].url, oldUrl);
  assert.ok(f.revoked.includes(oldUrl));
  await f.client.disconnect();
});

test("an obsolete camera stop handle does not close a replacement subscription", async () => {
  const f = fixture();
  await f.client.connect();
  const oldStop = f.client.openCamera("overview");
  f.client.openCamera("overview");
  oldStop();
  assert.equal(f.sockets[1].readyState, 0);
  await f.client.disconnect();
});


test('torque command is typed, exact-arm scoped and does not grant control',async()=>{
 const f=fixture();await f.client.connect(TOKEN);
 f.setRoute(call=>call.path==='/api/torque'?f.response({success:true,measured_torque:call.body.enabled?'ON':'OFF',message:'Applied'}):undefined);
 const result=await f.client.setTorque('arm1',false);
 assert.equal(result.measured_torque,'OFF');
 assert.deepEqual(f.calls.find(c=>c.path==='/api/torque').body,{arm_id:'arm1',controller_session:'session1',enabled:false});
 assert.equal(f.client.state.arms.arm1.permission,false);
 assert(!f.calls.some(c=>c.path==='/api/permission'||c.path==='/api/hold'));
 await assert.rejects(f.client.setTorque('arm1','false'),/boolean/);
});

test('failed torque requests preserve last measured state and propagate actual reason',async()=>{
 const f=fixture();await f.client.connect(TOKEN);const before=f.client.state.arms.arm1.measured_torque;
 f.setRoute(call=>call.path==='/api/torque'?f.response({success:false,measured_torque:'UNKNOWN',message:'Readback failed'}):undefined);
 await assert.rejects(f.client.setTorque('arm1',false),/Readback failed/);
 assert.equal(f.client.state.arms.arm1.measured_torque,before);
});


test("native jog releases to a zero rate and a named goal supersedes jog without replay", async () => {
  const f = fixture();
  await f.client.connect(TOKEN);
  const opened = f.client.startManual("arm1");
  const ws = f.sockets[0]; ws.open(); await opened;
  ws.message({type:"grant", grant:f.grant(null)});
  await f.clock.advance(20);
  const pickup = ws.sent.filter(m=>m.frame_kind==="COMMAND").at(-1);
  ws.message({type:"grant", grant:f.grant(null,{last_accepted_input_id:pickup.input_id})});
  const jog = {translation:[0,1,0], elevation:0, roll:0, gripper_target:null, gripper_rate:0, level:2, position_only:false};
  let intent = jog;
  f.client.setJogSource(()=>intent);
  await f.clock.advance(20);
  assert.deepEqual(ws.sent.at(-1).body,jog);
  assert.equal(ws.sent.at(-1).payload_kind,"MANUAL_JOG");
  intent=null;
  await f.clock.advance(20);
  assert.equal(ws.sent.at(-1).payload_kind,"MANUAL_JOG");
  assert.deepEqual(ws.sent.at(-1).body.translation,[0,0,0]);
  f.client.setManualTarget({positions:{shoulder_pan:0,elbow_flex:.1},gripper:40});
  await f.clock.advance(20);
  assert.equal(ws.sent.at(-1).payload_kind,"DIRECT_JOINT_TARGET");
  await f.client.disconnect();
});
