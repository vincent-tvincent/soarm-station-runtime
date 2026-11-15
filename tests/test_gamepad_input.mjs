import assert from "node:assert/strict";
import test from "node:test";
import { GamepadInputEngine, GAMEPAD_BUTTON_ACTIONS } from "../web/gamepad-input.js";

function pad({ axes = [0, 0, 0, 0], pressed = [], lt = 0, rt = 0, mapping = "standard", connected = true, id = "test standard pad", timestamp = 1 } = {}) {
  const buttons = Array.from({ length: 17 }, (_, i) => ({ pressed: pressed.includes(i), value: pressed.includes(i) ? 1 : 0 }));
  buttons[6] = { pressed: lt >= 0.5, value: lt };
  buttons[7] = { pressed: rt >= 0.5, value: rt };
  return { axes, buttons, mapping, connected, id, index: 0, timestamp };
}
const rawTrigger = normalized => normalized === 0 ? 0 : .02 + normalized * .98;
const close = (actual, expected) => assert.ok(Math.abs(actual - expected) < 1e-9, `${actual} != ${expected}`);
const active = (engine, raw, heldOpening = 40) => engine.sample(raw, { enabled: true, heldOpening });

test("standard stick signs, radial deadzone and remapped residual", () => {
  const e = new GamepadInputEngine(); e.reset(40, pad());
  let out = active(e, pad({ axes: [.04, -.04, .03, -.03], rt: 1 }));
  assert.deepEqual(out.translation, [0, 0, 0]); close(out.roll, 0);
  out = active(e, pad({ axes: [0, 0, 0, -.54], rt: 1 }));
  close(out.translation[0], .5); close(out.translation[1], 0); close(out.translation[2], 0);
  out = active(e, pad({ axes: [.54, -.54, .54, -.54], rt: 1 }));
  assert.ok(out.translation[0] > 0 && out.translation[1] < 0 && out.translation[2] > 0 && out.roll > 0);
});

test("RT is separate normalized speed bound, translation norm is capped", () => {
  const e = new GamepadInputEngine(); e.reset(40, pad());
  const out = active(e, pad({ axes: [1, -1, 1, -1], rt: rawTrigger(.35) }));
  close(Math.hypot(...out.translation), 1);
  close(out.speed_scale, .35);
  assert.ok(Math.hypot(...out.translation) > out.speed_scale); // axes are not pre-scaled twice
  assert.ok(Math.abs(out.roll) <= 1);
  const neutralRT = active(e, pad({ axes: [0, -1, 0, -1], rt: .015 }));
  close(neutralRT.speed_scale, 0);
  close(Math.hypot(...neutralRT.translation), 1);
});

test("D-pad elevation and fine roll override without extra yaw", () => {
  const e = new GamepadInputEngine(); e.reset(40, pad());
  let out = active(e, pad({ axes: [1, 0, 0, 0], pressed: [12, 14], rt: 1 }));
  close(out.elevation, .2); close(out.roll, -.2); assert.ok(!("yaw" in out));
  out = active(e, pad({ axes: [1, 0, 0, 0], pressed: [13, 15], rt: 1 }));
  close(out.elevation, -.2); close(out.roll, .2);
  out = active(e, pad({ axes: [1, 0, 0, 0], pressed: [12, 13, 14, 15], rt: 1 }));
  assert.equal(out.elevation, 0); close(out.roll, 0);
  close(active(e, pad({ axes: [1, 0, 0, 0], rt: 1 })).roll, 1);
});

test("first-sample/selection baseline prevents held Start and standard validation never guesses", () => {
  const e = new GamepadInputEngine();
  assert.deepEqual(e.sample(pad({ pressed: [0] })).actions, []);
  assert.deepEqual(e.sample(pad({ pressed: [0] })).actions, []);
  e.sample(pad());
  assert.deepEqual(e.sample(pad({ pressed: [0] })).actions, ["start"]);
  assert.equal(e.resume_required, true); // a button edge is not a successful grant
  const invalid = e.sample(pad({ mapping: "", axes: [1, 1, 1, 1], rt: 1 }), { enabled: true });
  assert.equal(invalid.valid, false); assert.deepEqual(invalid.translation, [0, 0, 0]);
  assert.deepEqual(invalid.actions, []); assert.equal(invalid.gripper_target, null);
});

test("all action edges fire once; Stop suppresses simultaneous actions/intents", () => {
  for (const [index, action] of Object.entries(GAMEPAD_BUTTON_ACTIONS)) {
    const e = new GamepadInputEngine(); e.reset(40, pad());
    assert.deepEqual(e.sample(pad({ pressed: [+index] })).actions, [action]);
    assert.deepEqual(e.sample(pad({ pressed: [+index] })).actions, []);
  }
  const e = new GamepadInputEngine(); e.reset(40, pad());
  const out = active(e, pad({ axes: [1, -1, 1, -1], pressed: [0, 1, 2, 3, 4, 5], lt: 1, rt: 1 }));
  assert.deepEqual(out.actions, ["stop"]); assert.deepEqual(out.translation, [0, 0, 0]);
  assert.equal(out.gripper_target, null); assert.equal(e.gripperState, "pickup");
  assert.equal(e.resume_required, true);
  assert.deepEqual(e.sample(pad({ pressed: [16] })).actions, []);
});

test("disabled preview emits Start/X notifications but no robot intents or staged jaw lock", () => {
  const e = new GamepadInputEngine(); e.reset(40, pad());
  const out = e.sample(pad({ axes: [1, -1, 1, -1], pressed: [0, 2, 12], lt: 1, rt: 1 }), { enabled: false, heldOpening: 27 });
  assert.deepEqual(out.actions, ["start", "griplock"]);
  assert.deepEqual(out.translation, [0, 0, 0]); assert.equal(out.speed_scale, 0); assert.equal(out.elevation, 0);
  assert.equal(out.gripper_target, null); assert.equal(e.gripperState, "pickup");
  close(e.heldClosing, .6); close(e.liveLT, 1); close(e.liveRT, 1);
});

test("pickup requires a new upward crossing, then LT release opens only while following", () => {
  const e = new GamepadInputEngine(); e.reset(40, pad());
  close(e.pickupThreshold, .63);
  close(active(e, pad({ lt: rawTrigger(.6) })).gripper_target, 40);
  let out = active(e, pad({ lt: rawTrigger(.64) }));
  assert.equal(e.gripperState, "following"); close(out.gripper_target, 36);
  out = active(e, pad()); close(out.gripper_target, 100);
});

test("X captures admitted target, unlocks into pickup and ignores an already-held high LT", () => {
  const e = new GamepadInputEngine(); e.reset(40, pad());
  active(e, pad({ lt: 1 })); assert.equal(e.gripperState, "following");
  let out = active(e, pad({ lt: 1, pressed: [2] }), 27.3);
  assert.equal(e.gripperState, "locked"); close(out.gripper_target, 27.3);
  out = active(e, pad()); close(out.gripper_target, 27.3); // LT release cannot open a locked jaw
  active(e, pad({ lt: 1 }));
  out = active(e, pad({ lt: 1, pressed: [2] }), 27.3);
  assert.equal(e.gripperState, "pickup"); close(out.gripper_target, 27.3);
  out = active(e, pad({ lt: 1 })); close(out.gripper_target, 27.3);
  active(e, pad({ lt: rawTrigger(.70) }));
  out = active(e, pad({ lt: rawTrigger(.80) }));
  assert.equal(e.gripperState, "following"); close(out.gripper_target, 20);
});

test("closed endpoint threshold caps at one and reset with squeezed LT cannot auto-capture", () => {
  const e = new GamepadInputEngine(); e.reset(0, pad({ lt: 1, pressed: [0] }));
  assert.equal(e.pickupThreshold, 1);
  let out = active(e, pad({ lt: 1, pressed: [0] }), 0);
  assert.deepEqual(out.actions, []); assert.equal(e.gripperState, "pickup"); close(out.gripper_target, 0);
  active(e, pad({ lt: rawTrigger(.9) }), 0);
  out = active(e, pad({ lt: 1 }), 0); assert.equal(e.gripperState, "following"); close(out.gripper_target, 0);
  out = active(e, pad(), 0); close(out.gripper_target, 100);
});

test("onscreen toggle uses current admitted opening and refuses unknown references", () => {
  const e = new GamepadInputEngine(); e.reset(52, pad());
  assert.equal(e.toggleGripper(null), false); assert.equal(e.gripperState, "pickup");
  assert.equal(e.toggleGripper(35.7, .8), true); assert.equal(e.gripperState, "locked");
  close(active(e, pad()).gripper_target, 35.7);
  assert.equal(e.toggleGripper(35.7, .8), true); assert.equal(e.gripperState, "pickup");
  close(active(e, pad({ lt: rawTrigger(.8) })).gripper_target, 35.7);
});

test("disconnect/invalid input latch resume and emit source loss once", () => {
  const e = new GamepadInputEngine(); e.reset(40, pad());
  active(e, pad({ axes: [0, 0, 0, -1], rt: 1 }));
  let out = active(e, null); assert.equal(out.source_lost, true); assert.equal(out.resume_required, true);
  out = active(e, null); assert.equal(out.source_lost, false); assert.deepEqual(out.actions, []);
  out = active(e, pad({ axes: [0, 0, 0, -1], rt: 1, pressed: [0] }));
  assert.deepEqual(out.actions, []); assert.deepEqual(out.translation, [0, 0, 0]);
  e.sample(pad());
  out = e.sample(pad({ pressed: [0] })); assert.deepEqual(out.actions, ["start"]);
  assert.equal(e.resume_required, true);
  e.reset(40, pad({ pressed: [0] }));
  out = active(e, pad({ axes: [0, 0, 0, -1], rt: 1, pressed: [0] }));
  close(out.translation[0], 1); assert.deepEqual(out.actions, []);
});

test("stable held controls and unchanged browser timestamp have no inactivity timeout", () => {
  const e = new GamepadInputEngine(); e.reset(40, pad());
  const held = pad({ axes: [0, 0, 0, -.7], rt: rawTrigger(.3), timestamp: 123 });
  const initial = active(e, held);
  for (let i = 0; i < 10000; i += 1) assert.deepEqual(active(e, held), initial);
});

test("controller replacement and malformed analog values cannot resume or emit motion", () => {
  const e = new GamepadInputEngine(); e.reset(40, pad());
  const changed = active(e, pad({ id: "different standard pad", pressed: [0], rt: 1 }));
  assert.equal(changed.source_lost, true); assert.equal(changed.resume_required, true); assert.deepEqual(changed.actions, []);
  e.reset(40, pad());
  e.resetButtonBaseline(pad({ id: "new selection", pressed: [0] }));
  assert.equal(e.resume_required, true);
  assert.deepEqual(active(e, pad({ id: "new selection", axes: [0, 0, 0, -1], rt: 1 })).translation, [0, 0, 0]);
  e.reset(40, pad());
  const bad = pad(); bad.axes[2] = NaN;
  assert.equal(active(e, bad).valid, false);
  const unknown = new GamepadInputEngine(); unknown.reset(null, pad());
  assert.equal(active(unknown, pad({ lt: 1, rt: 1 })).gripper_target, null);
});
