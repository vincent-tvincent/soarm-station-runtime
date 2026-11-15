import test from 'node:test';
import assert from 'node:assert/strict';
import { GamepadPanel } from '../web/gamepad-panel.js';

const copy = value => structuredClone(value);
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
const deferred = () => { let resolve; const promise = new Promise(done => resolve = done); return { promise, resolve }; };
const raw = ({ axes = [0, 0, 0, 0], rt = 0, lt = 0, buttons = [], connected = true } = {}) => ({
  index: 0, id: 'Test standard controller', mapping: 'standard', connected, axes: [...axes],
  buttons: Array.from({ length: 16 }, (_, index) => ({ value: index === 6 ? lt : index === 7 ? rt : buttons.includes(index) ? 1 : 0, pressed: buttons.includes(index) })),
});
const output = (engine, pad, held) => engine.sample(pad, { enabled: true, heldOpening: held });

class Element {
  constructor() { this.nodes = new Map(); this.style = {}; this.dataset = {}; this.firstChild = { textContent: '' }; this.hidden = false; this.value = ''; this.attributes = {}; this.classList = { toggle() {} }; }
  querySelector(selector) { if (!this.nodes.has(selector)) this.nodes.set(selector, new Element()); return this.nodes.get(selector); }
  querySelectorAll() { return []; }
  setAttribute(name, value) { this.attributes[name] = value; }
  replaceChildren(...children) { this.children = children; }
  click() { return this.onclick?.(); }
}

function fixture(t, { own = true, pad = raw(), requested = 50, admitted = 50, measured = 50, reference = requested } = {}) {
  const originals = { window: globalThis.window, document: globalThis.document, Option: globalThis.Option };
  const listeners = new Map();
  let focused = true, current = pad, time = 0, generation = 0, requestHandler = null, readerError = null;
  const eventTarget = { addEventListener(name, callback) { const list = listeners.get(name) || []; list.push(callback); listeners.set(name, list); } };
  globalThis.window = eventTarget;
  globalThis.document = { ...eventTarget, hidden: false, hasFocus: () => focused };
  globalThis.Option = class { constructor(label, value) { this.label = label; this.value = value; } };
  t.after(() => { for (const [key, value] of Object.entries(originals)) { if (value === undefined) delete globalThis[key]; else globalThis[key] = value; } });
  const calls = { targets: [], requests: [], errors: [], starts: 0, stops: 0, homes: 0, cameras: 0, layouts: 0, raf: [] };
  const manualElements = new Element();
  let panel;
  const manual = {
    arm: 'arm1', mode: 'gamepad', busy: false, own, epoch: 0, level: 2,
    target: { positions: { joint: 1 }, gripper: requested, gripper_unit: 'percent' },
    reference: { positions: { joint: .5 }, gripper: reference, gripper_unit: 'percent' },
    descriptor: { speed_levels: [{ level: 2, translation_m_s: .04 }] },
    isOwnStream() { return this.own; }, measured: () => ({ positions: { joint: .4 }, gripper: measured }),
    preservedGripper: () => admitted, stateForArm: () => ({ feedback_valid: true }),
    intentContext() { return { arm: this.arm, mode: this.mode, generation }; },
    currentIntent(context) { return context.arm === this.arm && context.mode === this.mode && context.generation === generation; },
    updateControls() {}, note() {}, el: id => manualElements.querySelector('#' + id),
    setSpeed(value) { this.level = value; },
    onError(error) { calls.errors.push(error); },
    async safe(fn) { try { return await fn(); } catch (error) { calls.errors.push(error); } },
    async start() { calls.starts++; this.own = true; this.client.currentOperation = { ready: true }; panel.operationStarted(); },
    async stop() { calls.stops++; this.own = false; this.client.currentOperation = null; generation++; this.epoch++; panel.operationEnded(); },
    async preset(name) { assert.equal(name, 'home'); calls.homes++; generation++; this.epoch++; panel.suspend(); },
    client: {
      currentOperation: own ? { ready: true } : null,
      enable() { throw Error('The gamepad panel must not grant permission directly'); },
      request(path, body) { calls.requests.push({ path, body: copy(body) }); return requestHandler ? requestHandler(path, body) : Promise.resolve({ status: 'OK', step_fraction: 1, target: { positions: copy(body.reference_positions), gripper: body.gripper_target ?? body.reference_gripper, gripper_unit: 'percent' } }); },
      setManualTarget(target) { calls.targets.push(copy(target)); return true; },
    },
  };
  const element = new Element();
  panel = new GamepadPanel({ element, manual, onCameraCycle: () => calls.cameras++, onCameraLayout: () => calls.layouts++,
    env: { read: () => { if (readerError !== null) throw readerError; return current ? [current] : []; }, supported: () => true, focused: () => focused,
      now: () => time, raf: callback => calls.raf.push(callback) } });
  panel.active = true; panel.wasFocused = true; panel.selectedKey = panel.key(pad); panel.raw = pad;
  panel.inventoryKey = JSON.stringify([[pad.index, pad.id, pad.mapping]]); panel.needsNeutral = false;
  if (own) panel.engine.reset(admitted, pad); else panel.engine.resetButtonBaseline(pad);
  panel.output = panel.engine.sample(pad, { enabled: own, heldOpening: admitted });
  return { panel, manual, calls, element,
    poll(next = current) { current = next; time += 16; panel.poll(); },
    focus(value) { focused = value; },
    advance(value) { time += value; },
    handler(fn) { requestHandler = fn; },
    readError(error) { readerError = error; },
    emit(name, event = {}) { for (const callback of listeners.get(name) || []) callback(event); },
  };
}

test('an invalid current sample stops once without null dereference and polling can continue', async t => {
  const f = fixture(t); const bad = raw(); bad.axes[0] = NaN;
  assert.doesNotThrow(() => f.poll(bad)); await flush();
  assert.equal(f.calls.stops, 1); assert.equal(f.manual.own, false);
  assert.doesNotThrow(() => f.poll(bad)); assert.doesNotThrow(() => f.poll(raw()));
  await flush(); assert.equal(f.calls.stops, 1); assert.equal(f.calls.starts, 0);
  assert.equal(f.calls.targets.length, 0);
});

test('controller read errors are visible, stop a lost source once, and clear without rearming after a successful read', async t => {
  const f = fixture(t); f.panel.errorMessage = 'Earlier calculation error';
  f.readError(new Error('Browser policy blocked controller access'));
  assert.doesNotThrow(() => f.poll()); await flush();
  const status = f.element.querySelector('#gamepad-device-state');
  assert.match(status.textContent, /Controller read failed: Browser policy blocked controller access/);
  assert.match(f.panel.startReason(), /Browser policy blocked/);
  assert.equal(f.panel.deviceReadError, 'Browser policy blocked controller access');
  assert.equal(f.panel.errorMessage, 'Earlier calculation error');
  assert.equal(f.calls.stops, 1); assert.equal(f.manual.own, false); assert.equal(f.panel.canStart(), false);
  f.poll(); await flush(); assert.equal(f.calls.stops, 1);
  f.readError(null); f.advance(60); f.poll(null); await flush();
  assert.equal(f.panel.deviceReadError, ''); assert.equal(status.textContent, 'Waiting for a controller');
  assert.equal(f.panel.errorMessage, 'Earlier calculation error');
  f.poll(raw({ buttons: [0] })); await flush();
  assert.equal(f.calls.starts, 0); assert.equal(f.manual.own, false); assert.equal(f.calls.targets.length, 0);
});

test('RT neutral in a background or inactive pane cannot satisfy focused resume', async t => {
  const f = fixture(t); f.focus(false); f.emit('blur');
  f.poll(raw({ rt: 0 })); assert.equal(f.panel.needsNeutral, true);
  f.poll(raw({ rt: 1, axes: [0, 0, 0, -.6] }));
  f.focus(true); f.poll(raw({ rt: 1, axes: [0, 0, 0, -.6] })); await flush();
  assert.equal(f.panel.needsNeutral, true); assert.equal(f.calls.requests.length, 0);
  f.panel.setActive(false); f.poll(raw()); assert.equal(f.panel.needsNeutral, true);
  f.panel.setActive(true); // Current source is neutral and this pane is focused.
  assert.equal(f.panel.needsNeutral, false);
  f.poll(raw({ rt: 1, axes: [0, 0, 0, -.6] })); await flush();
  assert.equal(f.calls.requests.length, 1); assert.equal(f.calls.stops, 0);
});

for (const [label, nextAxes] of [['neutral', [0, 0, -.5, 0]], ['reverse', [0, 0, -.5, .5]]]) {
  test(`an individual body-axis ${label} retires a pending solve while another axis stays active`, async t => {
    const f = fixture(t, { pad: raw({ rt: 1, axes: [0, 0, -.5, -.5] }) });
    const gate = deferred(); f.handler(() => gate.promise);
    const pending = f.panel.solve(); const epoch = f.panel.sourceEpoch;
    f.poll(raw({ rt: 1, axes: nextAxes })); assert.ok(f.panel.sourceEpoch > epoch);
    gate.resolve({ status: 'OK', step_fraction: 1, target: { positions: { joint: .6 }, gripper: 50 } });
    await pending; assert.equal(f.calls.targets.length, 0);
  });
}

test('same-direction stick jitter does not starve a valid pending solve', async t => {
  const f = fixture(t, { pad: raw({ rt: 1, axes: [0, 0, 0, -.5] }) });
  const gate = deferred(); f.handler(() => gate.promise);
  const pending = f.panel.solve(); const epoch = f.panel.sourceEpoch;
  f.poll(raw({ rt: 1, axes: [0, 0, 0, -.51] }));
  f.poll(raw({ rt: 1, axes: [0, 0, 0, -.49] }));
  assert.equal(f.panel.sourceEpoch, epoch);
  gate.resolve({ status: 'OK', step_fraction: 1, target: { positions: { joint: .6 }, gripper: 50 } });
  await pending; assert.equal(f.calls.targets.length, 1);
});

test('LT reversal invalidates an older closing result even with RT and body axes idle', async t => {
  const f = fixture(t, { pad: raw({ lt: .9 }) });
  f.panel.engine.gripperState = 'following'; f.panel.output = output(f.panel.engine, raw({ lt: .9 }), 50);
  const gate = deferred(); f.handler(() => gate.promise);
  const pending = f.panel.solve(); assert.ok(f.calls.requests[0].body.gripper_target < 20);
  f.poll(raw({ lt: .1 })); assert.ok(f.panel.output.gripper_target > 80);
  gate.resolve({ status: 'OK', step_fraction: 1, target: { positions: { joint: .5 }, gripper: 49 } });
  await pending; assert.equal(f.calls.targets.length, 0);
});

test('same-direction LT easing rejects only a result that would overshoot the newer absolute target', async t => {
  const f = fixture(t, { pad: raw({ lt: .9 }) });
  f.panel.engine.gripperState = 'following'; f.panel.output = output(f.panel.engine, raw({ lt: .9 }), 50);
  const gate = deferred(); f.handler(() => gate.promise);
  const pending = f.panel.solve(); const epoch = f.panel.sourceEpoch;
  f.poll(raw({ lt: .02 + .505 * .98 })); // 49.5% opening, still below the 50% reference.
  assert.equal(f.panel.sourceEpoch, epoch);
  gate.resolve({ status: 'OK', step_fraction: 1, target: { positions: { joint: .5 }, gripper: 49 } });
  await pending; assert.equal(f.calls.targets.length, 0);
});

for (const entry of ['onscreen', 'physical']) {
  test(`${entry} X locks the admitted 35% reference, replaces requested 20%, and preserves body targets`, async t => {
    const f = fixture(t, { requested: 20, reference: 20, admitted: 35, measured: 50, pad: raw({ lt: .9 }) });
    f.panel.engine.gripperState = 'following'; f.panel.output = output(f.panel.engine, raw({ lt: .9 }), 35);
    const gate = deferred(); f.handler(() => gate.promise);
    const pending = f.panel.solve();
    if (entry === 'onscreen') await f.element.querySelector('#gamepad-grip-lock').click();
    else f.poll(raw({ lt: .9, buttons: [2] }));
    assert.equal(f.panel.engine.gripperState, 'locked');
    assert.equal(f.manual.target.gripper, 35); assert.equal(f.manual.reference.gripper, 35);
    assert.deepEqual(f.manual.target.positions, { joint: 1 }); assert.deepEqual(f.manual.reference.positions, { joint: .5 });
    assert.equal(f.calls.targets.length, 1); assert.equal(f.calls.targets[0].gripper, 35);
    assert.deepEqual(f.calls.targets[0].positions, { joint: 1 });
    gate.resolve({ status: 'OK', step_fraction: 1, target: { positions: { joint: .7 }, gripper: 19 } });
    await pending; assert.equal(f.calls.targets.length, 1, 'late pre-lock solve must not restore the closing request');
  });
}

test('unlock keeps the captured grip until LT crosses pickup; it does not fabricate a new target', async t => {
  const f = fixture(t, { requested: 20, reference: 20, admitted: 35, measured: 50 });
  await f.element.querySelector('#gamepad-grip-lock').click();
  assert.equal(f.panel.engine.gripperState, 'locked');
  await f.element.querySelector('#gamepad-grip-lock').click();
  assert.equal(f.panel.engine.gripperState, 'pickup');
  assert.equal(f.manual.target.gripper, 35);
  f.poll(raw({ lt: .5 })); await flush(); assert.equal(f.panel.engine.gripperState, 'pickup');
  assert.equal(f.calls.requests.length, 0);
  f.poll(raw({ lt: .8 })); await flush();
  assert.equal(f.panel.engine.gripperState, 'following'); assert.equal(f.calls.requests.length, 1);
});

test('discovery, reconnect and held Start cannot auto-start; idle active sessions have no timeout', async t => {
  const f = fixture(t, { own: false, pad: raw({ buttons: [0] }) });
  f.poll(raw({ buttons: [0] })); await flush(); assert.equal(f.calls.starts, 0);
  f.poll(raw()); f.poll(raw({ buttons: [0] })); await flush(); assert.equal(f.calls.starts, 1);
  f.poll(raw());
  for (let i = 0; i < 20; i++) { f.advance(10000); f.poll(raw()); }
  await flush(); assert.equal(f.calls.stops, 0); assert.equal(f.manual.own, true);
  f.poll(null); await flush(); assert.equal(f.calls.stops, 1);
  f.emit('gamepaddisconnected', { gamepad: { index: 0 } }); f.poll(null); await flush(); assert.equal(f.calls.stops, 1);
  f.poll(raw({ rt: 1, buttons: [0] })); f.poll(raw()); await flush();
  assert.equal(f.calls.starts, 1); assert.equal(f.manual.own, false);
});

for (const action of ['stop', 'home', 'blur', 'mode']) {
  test(`${action} invalidates an outstanding analog response`, async t => {
    const f = fixture(t, { pad: raw({ rt: 1, axes: [0, 0, 0, -.5] }) });
    const gate = deferred(); f.handler(() => gate.promise);
    const pending = f.panel.solve();
    if (action === 'stop') f.poll(raw({ rt: 1, buttons: [1] }));
    if (action === 'home') f.poll(raw({ rt: 1, buttons: [3] }));
    if (action === 'blur') { f.focus(false); f.emit('blur'); }
    if (action === 'mode') { f.manual.mode = 'joint'; f.panel.setActive(false); }
    gate.resolve({ status: 'OK', step_fraction: 1, target: { positions: { joint: .6 }, gripper: 50 } });
    await pending; await flush(); assert.equal(f.calls.targets.length, 0);
    if (action === 'stop') assert.equal(f.calls.stops, 1);
    if (action === 'home') assert.equal(f.calls.homes, 1);
  });
}
