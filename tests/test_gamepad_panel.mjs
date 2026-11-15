import test from 'node:test';
import assert from 'node:assert/strict';
import { GamepadPanel } from '../web/gamepad-panel.js';
import { ManualPanel } from '../web/manual-panel.js';

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
  querySelectorAll(selector) {
    if (selector === '[data-grip-trigger]') return ['left', 'right'].map(side => {
      const node = this.querySelector(`[data-grip-trigger="${side}"]`); node.dataset.gripTrigger = side; return node;
    });
    return [];
  }
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
    updateControls() {}, updateKeys() {}, note() {}, el: id => manualElements.querySelector('#' + id),
    setSpeed(value) { ManualPanel.prototype.setSpeed.call(this, value); },
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
  manual.gamepad = panel; manual.element = element;
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

test('motion speed meter follows normalized stick commands independently of gripper triggers', t => {
  const f=fixture(t);
  for(const [lt,rt] of [[0,0],[1,0],[0,1],[1,1]]) {
    f.panel.output=output(f.panel.engine,raw({axes:[0,0,0,-.54],lt,rt}),50);
    assert.ok(Math.abs(f.panel.motionSpeedFraction()-.5)<1e-9);
    f.panel.render();
    assert.equal(f.element.querySelector('#gamepad-motion-speed-value').textContent,'50%');
    assert.equal(f.element.querySelector('#gamepad-speed-cap').textContent,'20.0 mm/s');
    const meter=f.element.querySelector('#gamepad-motion-speed-meter');
    assert.equal(meter.attributes['aria-valuenow'],'50');
    assert.equal(meter.querySelector('i').style.width,'50%');
  }
  f.panel.output=output(f.panel.engine,raw({lt:1,rt:1}),50);
  assert.equal(f.panel.motionSpeedFraction(),0,'gripper-only motion is not body speed');
  f.panel.render();
  assert.equal(f.element.querySelector('#gamepad-speed-cap').textContent,'0.0 mm/s');
});

test('motion rate readout scales with profile and keeps angular units separate',t=>{
  const f=fixture(t);
  f.manual.descriptor.speed_levels.push({level:3,translation_m_s:.08,orientation_rad_s:Math.PI/4});
  f.manual.setSpeed(3);
  f.panel.output=output(f.panel.engine,raw({axes:[0,0,0,-.54]}),50);
  f.panel.render();
  assert.equal(f.element.querySelector('#gamepad-motion-speed-value').textContent,'50%');
  assert.equal(f.element.querySelector('#gamepad-speed-cap').textContent,'40.0 mm/s');
  f.panel.output=output(f.panel.engine,raw({axes:[.54,0,0,0]}),50);
  f.panel.render();
  assert.equal(f.element.querySelector('#gamepad-speed-cap').textContent,'0.0 mm/s · 22.5°/s');
});

test('motion speed includes angular-only and fine D-pad requests and caps combined translation', t => {
  const f=fixture(t);
  for(const [pad,expected] of [
    [raw({axes:[.54,0,0,0]}),.5],
    [raw({buttons:[12]}),.2],
    [raw({axes:[0,-1,1,-1]}),1],
  ]) {
    f.panel.output=output(f.panel.engine,pad,50);
    assert.ok(Math.abs(f.panel.motionSpeedFraction()-expected)<1e-9);
  }
});

test('motion meter clears inactive or suspended requests instead of retaining a previous speed', t => {
  const f=fixture(t);
  f.panel.output=output(f.panel.engine,raw({axes:[0,0,0,-1]}),50);
  assert.equal(f.panel.motionSpeedFraction(),1);
  f.panel.needsNeutral=true; assert.equal(f.panel.motionSpeedFraction(),0); f.panel.needsNeutral=false;
  f.focus(false); assert.equal(f.panel.motionSpeedFraction(),0); f.focus(true);
  f.manual.busy=true; assert.equal(f.panel.motionSpeedFraction(),0); f.manual.busy=false;
  f.manual.own=false; f.panel.render();
  assert.equal(f.element.querySelector('#gamepad-motion-speed-value').textContent,'0%');
  assert.equal(f.element.querySelector('#gamepad-motion-speed-meter').querySelector('i').style.width,'0%');
});

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

test('only focused neutral sticks and released D-pad satisfy resume; triggers are not a speed gate', async t => {
  const f = fixture(t); f.focus(false); f.emit('blur');
  f.poll(raw({ rt: 1, lt: 1 })); assert.equal(f.panel.needsNeutral, true);
  f.poll(raw({ rt: 1, axes: [0, 0, 0, -.6] }));
  f.focus(true); f.poll(raw({ rt: 1, axes: [0, 0, 0, -.6] })); await flush();
  assert.equal(f.panel.needsNeutral, true); assert.equal(f.calls.requests.length, 0);
  f.poll(raw({ rt: 1, buttons: [12] })); await flush();
  assert.equal(f.panel.needsNeutral, true); assert.equal(f.calls.requests.length, 0);
  f.panel.setActive(false); f.poll(raw({ rt: 1, lt: 1 })); assert.equal(f.panel.needsNeutral, true);
  f.panel.setActive(true); // Focused body-neutral input, despite both triggers being squeezed.
  assert.equal(f.panel.needsNeutral, false);
  f.poll(raw({ rt: 0, lt: 1, axes: [0, 0, 0, -.6] })); await flush();
  assert.equal(f.calls.requests.length, 1); assert.equal(f.calls.stops, 0);
  assert.equal(f.calls.requests[0].body.speed_scale, 1);
});

test('Start uses raw body neutrality even when inactive preview has zero motion output', async t => {
  const f = fixture(t, { own: false, pad: raw({ axes: [0, 0, 0, -.5] }) });
  assert.deepEqual(f.panel.output.translation, [0, 0, 0]);
  assert.equal(f.panel.canStart(), false);
  f.poll(raw({ axes: [0, 0, 0, -.5], buttons: [0] })); await flush(); assert.equal(f.calls.starts, 0);
  f.poll(raw({ buttons: [12] })); assert.equal(f.panel.canStart(), false);
  f.poll(raw({ rt: 1, lt: 1 })); assert.equal(f.panel.canStart(), true);
  f.poll(raw({ rt: 1, lt: 1, buttons: [0] })); await flush(); assert.equal(f.calls.starts, 1);
});

test('sticks request the same body speed with RT released or squeezed', async t => {
  const f = fixture(t, { pad: raw({ axes: [0, 0, 0, -.5], rt: 0 }) });
  await f.panel.solve();
  const released = f.calls.requests.at(-1).body;
  assert.equal(released.speed_scale, 1); assert.ok(released.translation[0] > 0);
  f.poll(raw({ axes: [0, 0, 0, -.5], rt: 1 })); await flush();
  const squeezed = f.calls.requests.at(-1).body;
  assert.equal(squeezed.speed_scale, 1); assert.deepEqual(squeezed.translation, released.translation);
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

for (const [from, to, button] of [['left', 'right', 5], ['right', 'left', 4]]) {
  test(`${from}-to-${to} trigger swap freezes admitted grip, retires the pending old-source reply, and waits for pickup`, async t => {
    const pad = values => raw({ lt: values.left ?? .9, rt: values.right ?? .95, buttons: values.buttons || [] });
    const initial = pad({ [from]: .9, [to]: .95 });
    const f = fixture(t, { pad: initial, requested: 20, reference: 20, admitted: 35, measured: 50 });
    if (from === 'right') f.panel.engine.selectTrigger('right', 35);
    f.panel.engine.gripperState = 'following'; f.panel.output = output(f.panel.engine, initial, 35);
    const gate = deferred(); f.handler(() => gate.promise);
    const pending = f.panel.solve(); assert.ok(f.calls.requests[0].body.gripper_target < 20);
    f.poll(pad({ [from]: .9, [to]: .95, buttons: [button] }));
    assert.equal(f.panel.engine.activeTrigger, to); assert.equal(f.panel.engine.gripperState, 'pickup');
    assert.equal(f.manual.target.gripper, 35); assert.equal(f.manual.reference.gripper, 35);
    assert.deepEqual(f.manual.target.positions, { joint: 1 }); assert.deepEqual(f.manual.reference.positions, { joint: .5 });
    assert.equal(f.calls.targets.length, 1); assert.equal(f.calls.targets[0].gripper, 35);
    assert.equal(f.calls.stops, 0); assert.equal(f.calls.starts, 0); assert.equal(f.manual.own, true);
    gate.resolve({ status: 'OK', step_fraction: 1, target: { positions: { joint: .8 }, gripper: 19 } });
    await pending; assert.equal(f.calls.targets.length, 1);
    f.handler(null);
    f.poll(pad({ [from]: 0, [to]: .95 })); await flush();
    assert.equal(f.panel.engine.gripperState, 'pickup'); assert.equal(f.calls.targets.length, 1);
    assert.ok(Math.abs(f.panel.engine.pickupThreshold - .68) < 1e-10);
    f.poll(pad({ [from]: 1, [to]: .5 })); await flush(); assert.equal(f.panel.engine.gripperState, 'pickup');
    f.poll(pad({ [from]: 1, [to]: .8 })); await flush();
    assert.equal(f.panel.engine.gripperState, 'following'); assert.ok(f.calls.targets.at(-1).gripper < 35);
    assert.equal(f.calls.stops, 0);
  });
}

test('a locked gripper can swap trigger ownership and stays held until the new trigger picks up', async t => {
  const f = fixture(t, { pad: raw({ lt: .9, rt: .95 }), requested: 20, reference: 20, admitted: 35, measured: 50 });
  await f.element.querySelector('#gamepad-grip-lock').click(); assert.equal(f.panel.engine.gripperState, 'locked');
  await f.element.querySelector('[data-grip-trigger="right"]').click();
  assert.equal(f.panel.engine.activeTrigger, 'right'); assert.equal(f.panel.engine.gripperState, 'pickup');
  assert.equal(f.manual.target.gripper, 35); assert.equal(f.manual.reference.gripper, 35);
  assert.equal(f.calls.targets.length, 2); const epoch = f.panel.sourceEpoch;
  f.poll(raw({ lt: 0, rt: .95 })); await flush(); assert.equal(f.panel.engine.gripperState, 'pickup');
  assert.equal(f.calls.targets.length, 2, 'the onscreen selection must not queue a second freeze on the next sample');
  assert.equal(f.panel.sourceEpoch, epoch);
  assert.ok(f.calls.targets.every(value => value.gripper === 35)); assert.equal(f.calls.stops, 0);
});

test('reselecting the active trigger is idempotent and does not reset following or freeze another target', async t => {
  const lt = .02 + .65 * .98;
  const f = fixture(t, { pad: raw({ lt }), requested: 35, reference: 35, admitted: 35 });
  f.panel.engine.gripperState = 'following'; f.panel.output = output(f.panel.engine, raw({ lt }), 35);
  const epoch = f.panel.sourceEpoch;
  assert.equal(f.panel.selectGripperTrigger('left'), false);
  f.poll(raw({ lt, buttons: [4] })); await flush();
  assert.equal(f.panel.engine.gripperState, 'following'); assert.equal(f.panel.sourceEpoch, epoch);
  assert.equal(f.calls.targets.length, 0); assert.equal(f.calls.requests.length, 0); assert.equal(f.calls.stops, 0);
});

for (const selected of ['left', 'right']) {
  test(`the inactive trigger cannot change a following ${selected} gripper target`, async t => {
    const activeValue = .02 + .6 * .98;
    const sample = inactive => raw(selected === 'left' ? { lt: activeValue, rt: inactive } : { rt: activeValue, lt: inactive });
    const f = fixture(t, { pad: sample(0), requested: 40, reference: 40, admitted: 40 });
    if (selected === 'right') f.panel.engine.selectTrigger('right', 40);
    f.panel.engine.gripperState = 'following'; f.panel.output = output(f.panel.engine, sample(0), 40);
    const epoch = f.panel.sourceEpoch;
    f.poll(sample(1)); f.poll(sample(0)); await flush();
    assert.equal(f.panel.engine.activeTrigger, selected); assert.equal(f.panel.engine.gripperState, 'following');
    assert.ok(Math.abs(f.panel.output.gripper_target - 40) < .0001);
    assert.equal(f.panel.sourceEpoch, epoch); assert.equal(f.calls.targets.length, 0); assert.equal(f.calls.requests.length, 0);
  });
}

test('preview trigger selection does not grant permission, start, stop, or send a target', async t => {
  const f = fixture(t, { own: false, pad: raw({ rt: .95 }), requested: 20, reference: 20, admitted: 35 });
  await f.element.querySelector('[data-grip-trigger="right"]').click();
  assert.equal(f.panel.engine.activeTrigger, 'right'); assert.equal(f.manual.own, false);
  assert.equal(f.calls.targets.length, 0); assert.equal(f.calls.starts, 0); assert.equal(f.calls.stops, 0);
  assert.equal(f.manual.target.gripper, 20);
});

test('R3 wraps speed 5 to 1 once per press and rejects an outstanding old-profile response', async t => {
  const source = buttons => raw({ axes: [0, 0, 0, -.5], buttons });
  const f = fixture(t, { pad: source([]) }); f.manual.level = 5;
  const gate = deferred(); f.handler(() => gate.promise);
  const pending = f.panel.solve(); assert.equal(f.calls.requests[0].body.level, 5);
  const epoch = f.manual.epoch;
  f.poll(source([11])); assert.equal(f.manual.level, 1); assert.ok(f.manual.epoch > epoch);
  assert.equal(f.panel.lastSolveAt, null);
  f.poll(source([11])); assert.equal(f.manual.level, 1);
  gate.resolve({ status: 'OK', step_fraction: 1, target: { positions: { joint: .9 }, gripper: 50 } });
  await pending; assert.equal(f.calls.targets.length, 0);
  f.handler(null); f.poll(source([11])); await flush(); assert.equal(f.calls.requests.at(-1).body.level, 1);
  assert.equal(f.calls.requests.at(-1).body.dt_s, 1 / 60, 'new profile starts a fresh integration interval rather than catching up');
  assert.equal(f.panel.engine.activeTrigger, 'left'); assert.equal(f.calls.stops, 0);
});

test('L3 cycles primary camera and Menu cycles layout without changing speed or operation authority', async t => {
  const f = fixture(t, { own: false }); const level = f.manual.level;
  f.poll(raw({ buttons: [10] })); f.poll(raw({ buttons: [10] })); await flush();
  assert.equal(f.calls.cameras, 1); assert.equal(f.calls.layouts, 0);
  f.poll(raw()); f.poll(raw({ buttons: [9] })); f.poll(raw({ buttons: [9] })); await flush();
  assert.equal(f.calls.layouts, 1); assert.equal(f.calls.cameras, 1); assert.equal(f.manual.level, level);
  assert.equal(f.calls.starts, 0); assert.equal(f.calls.stops, 0); assert.equal(f.calls.targets.length, 0);
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
