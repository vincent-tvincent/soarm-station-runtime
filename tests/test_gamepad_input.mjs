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
  let out = active(e, pad({ axes: [.03, -.03, .04, -.04], rt: 1 }));
  assert.deepEqual(out.translation, [0, 0, 0]); close(out.roll, 0);
  out = active(e, pad({ axes: [0, -.54, 0, 0], rt: 1 }));
  close(out.translation[0], .5); close(out.translation[1], 0); close(out.translation[2], 0);
  out = active(e, pad({ axes: [.54, -.54, .54, -.54], rt: 1 }));
  assert.ok(out.translation[0] > 0 && out.translation[1] < 0 && out.translation[2] > 0 && out.roll > 0);
});

test("left stick independently controls XY and right stick independently controls Z and roll", () => {
  const e = new GamepadInputEngine(); e.reset(40, pad());
  const cases = [
    [[0, -.54, 0, 0], [.5, 0, 0], 0],
    [[0, .54, 0, 0], [-.5, 0, 0], 0],
    [[-.54, 0, 0, 0], [0, .5, 0], 0],
    [[.54, 0, 0, 0], [0, -.5, 0], 0],
    [[0, 0, 0, -.54], [0, 0, .5], 0],
    [[0, 0, 0, .54], [0, 0, -.5], 0],
    [[0, 0, -.54, 0], [0, 0, 0], -.5],
    [[0, 0, .54, 0], [0, 0, 0], .5],
  ];
  for (const [axes, translation, roll] of cases) {
    const result = active(e, pad({axes}));
    result.translation.forEach((value, index) => close(value, translation[index]));
    close(result.roll, roll); close(result.elevation, 0);
  }
});

test("sticks set body fractions independently of either trigger and translation norm is capped", () => {
  const e = new GamepadInputEngine(); e.reset(40, pad());
  const out = active(e, pad({ axes: [1, -1, 1, -1], rt: rawTrigger(.35) }));
  close(Math.hypot(...out.translation), 1);
  close(out.speed_scale, 1);
  assert.ok(Math.abs(out.roll) <= 1);
  const neutralRT = active(e, pad({ axes: [0, -1, 0, -1], rt: .015 }));
  close(neutralRT.speed_scale, 1);
  close(Math.hypot(...neutralRT.translation), 1);
  const axes = [0, -.54, 0, 0];
  for (const [lt, rt] of [[0, 0], [1, 0], [0, 1], [.2, .9]]) {
    const body = active(e, pad({ axes, lt, rt }));
    close(body.translation[0], .5); close(body.speed_scale, 1);
  }
  close(active(e, pad({ lt: 1, rt: 1 })).speed_scale, 0);
});

test("D-pad elevation and fine roll override without extra yaw", () => {
  const e = new GamepadInputEngine(); e.reset(40, pad());
  let out = active(e, pad({ axes: [0, 0, 1, 0], pressed: [12, 14], rt: 1 }));
  close(out.elevation, .2); close(out.roll, -.2); assert.ok(!("yaw" in out));
  out = active(e, pad({ axes: [0, 0, 1, 0], pressed: [13, 15], rt: 1 }));
  close(out.elevation, -.2); close(out.roll, .2);
  out = active(e, pad({ axes: [0, 0, 1, 0], pressed: [12, 13, 14, 15], rt: 1 }));
  assert.equal(out.elevation, 0); close(out.roll, 0);
  close(active(e, pad({ axes: [0, 0, 1, 0], rt: 1 })).roll, 1);
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
  active(e, pad({ axes: [0, -1, 0, 0], rt: 1 }));
  let out = active(e, null); assert.equal(out.source_lost, true); assert.equal(out.resume_required, true);
  out = active(e, null); assert.equal(out.source_lost, false); assert.deepEqual(out.actions, []);
  out = active(e, pad({ axes: [0, -1, 0, 0], rt: 1, pressed: [0] }));
  assert.deepEqual(out.actions, []); assert.deepEqual(out.translation, [0, 0, 0]);
  e.sample(pad());
  out = e.sample(pad({ pressed: [0] })); assert.deepEqual(out.actions, ["start"]);
  assert.equal(e.resume_required, true);
  e.reset(40, pad({ pressed: [0] }));
  out = active(e, pad({ axes: [0, -1, 0, 0], rt: 1, pressed: [0] }));
  close(out.translation[0], 1); assert.deepEqual(out.actions, []);
});

test("stable held controls and unchanged browser timestamp have no inactivity timeout", () => {
  const e = new GamepadInputEngine(); e.reset(40, pad());
  const held = pad({ axes: [0, -.7, 0, 0], rt: rawTrigger(.3), timestamp: 123 });
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
  assert.deepEqual(active(e, pad({ id: "new selection", axes: [0, -1, 0, 0], rt: 1 })).translation, [0, 0, 0]);
  e.reset(40, pad());
  const bad = pad(); bad.axes[2] = NaN;
  assert.equal(active(e, bad).valid, false);
  const unknown = new GamepadInputEngine(); unknown.reset(null, pad());
  assert.equal(active(unknown, pad({ lt: 1, rt: 1 })).gripper_target, null);
});

test("revised shoulder/stick/menu actions have no old speed or FK-reset mapping", () => {
  assert.equal(GAMEPAD_BUTTON_ACTIONS[4], undefined);
  assert.equal(GAMEPAD_BUTTON_ACTIONS[5], undefined);
  assert.equal(GAMEPAD_BUTTON_ACTIONS[9], "nextcameralayout");
  assert.equal(GAMEPAD_BUTTON_ACTIONS[10], "nextcamera");
  assert.equal(GAMEPAD_BUTTON_ACTIONS[11], "nextspeed");
  assert.ok(!Object.values(GAMEPAD_BUTTON_ACTIONS).some(a => ["speeddown", "speedup", "resetFKview"].includes(a)));
});

test("bodyNeutral reflects processed sticks and any D-pad control during inactive preview", () => {
  const e = new GamepadInputEngine();
  e.reset(40, pad({ axes: [.04, -.04, .04, -.04], lt: 1, rt: 1 }));
  assert.equal(e.bodyNeutral, true);
  e.sample(pad({ axes: [0, -.2, 0, 0] }), { enabled: false });
  assert.equal(e.bodyNeutral, false);
  let out = e.sample(pad({ pressed: [12, 13] }), { enabled: false });
  assert.equal(e.bodyNeutral, false); assert.equal(out.elevation, 0); assert.equal(out.gripper_target, null);
  out = e.sample(pad({ lt: 1, rt: 1 }), { enabled: false });
  assert.equal(e.bodyNeutral, true); assert.equal(out.speed_scale, 0);
  e.sample(null); assert.equal(e.bodyNeutral, false);
});

test("partial trigger switches both directions capture admitted opening and ignore inactive trigger", () => {
  const e = new GamepadInputEngine(); e.reset(40, pad());
  assert.equal(e.activeTrigger, "left");
  active(e, pad({ lt: rawTrigger(.8), rt: rawTrigger(.2) }));
  assert.equal(e.gripperState, "following");
  let out = active(e, pad({ pressed: [5], lt: rawTrigger(.8), rt: rawTrigger(.2), axes: [0, -.54, 0, 0] }), 35);
  assert.equal(e.activeTrigger, "right"); assert.equal(out.trigger_changed, true);
  assert.equal(e.gripperState, "pickup"); close(e.liveGrip, .2); close(out.gripper_target, 35);
  close(out.translation[0], .5); close(out.speed_scale, 1);
  out = active(e, pad({ lt: 0, rt: rawTrigger(.2) }), 35);
  close(out.gripper_target, 35); assert.equal(out.trigger_changed, false);
  out = active(e, pad({ lt: 1, rt: rawTrigger(.7) }), 35);
  assert.equal(e.gripperState, "following"); close(out.gripper_target, 30);
  out = active(e, pad({ pressed: [4], lt: rawTrigger(.9), rt: rawTrigger(.7) }), 27.3);
  assert.equal(e.activeTrigger, "left"); assert.equal(out.trigger_changed, true);
  assert.equal(e.gripperState, "pickup"); close(out.gripper_target, 27.3);
  out = active(e, pad({ lt: rawTrigger(.9), rt: 0 }), 27.3);
  close(out.gripper_target, 27.3); // an already squeezed newly selected trigger cannot capture
  active(e, pad({ lt: rawTrigger(.7), rt: 1 }), 27.3);
  out = active(e, pad({ lt: rawTrigger(.8), rt: 0 }), 27.3);
  assert.equal(e.gripperState, "following"); close(out.gripper_target, 20);
});

test("right-trigger pickup and X locking use RT while LT remains irrelevant", () => {
  const e = new GamepadInputEngine(); e.reset(40, pad());
  active(e, pad({ lt: 0, rt: rawTrigger(.9) }));
  let out = active(e, pad({ pressed: [5], lt: 1, rt: rawTrigger(.9) }), 40);
  assert.equal(e.gripperState, "pickup"); close(out.gripper_target, 40);
  out = active(e, pad({ lt: 0, rt: rawTrigger(.9) }), 40); close(out.gripper_target, 40);
  active(e, pad({ lt: 1, rt: rawTrigger(.5) }), 40);
  out = active(e, pad({ lt: 0, rt: rawTrigger(.7) }), 40);
  assert.equal(e.gripperState, "following"); close(out.gripper_target, 30);
  out = active(e, pad({ pressed: [2], lt: 1, rt: rawTrigger(.7) }), 33.4);
  assert.equal(e.gripperState, "locked"); close(out.gripper_target, 33.4);
  out = active(e, pad({ lt: 0, rt: 0 }), 33.4); close(out.gripper_target, 33.4);
  active(e, pad({ rt: 1 }), 33.4);
  out = active(e, pad({ pressed: [2], lt: 0, rt: 1 }), 33.4);
  assert.equal(e.gripperState, "pickup"); close(out.gripper_target, 33.4);
  out = active(e, pad({ lt: 1, rt: 1 }), 33.4); close(out.gripper_target, 33.4);
});

test("onscreen selection is idempotent, enters pickup, and never queues a second notification", () => {
  const e = new GamepadInputEngine(); e.reset(40, pad());
  active(e, pad({ lt: rawTrigger(.8), rt: rawTrigger(.9) }));
  assert.equal(e.selectTrigger("left", 99), false);
  assert.equal(e.gripperState, "following"); close(e.heldClosing, .6);
  let out = active(e, pad({ pressed: [4], lt: rawTrigger(.8), rt: rawTrigger(.9) }), 99);
  assert.equal(out.trigger_changed, false); assert.equal(e.gripperState, "following"); close(out.gripper_target, 20);
  assert.equal(e.toggleGripper(35.7), true); assert.equal(e.gripperState, "locked");
  assert.equal(e.selectTrigger("right", 35.7), true); assert.equal(e.gripperState, "pickup");
  out = active(e, pad({ rt: rawTrigger(.9) }), 35.7);
  assert.equal(out.trigger_changed, false); close(out.gripper_target, 35.7);
  assert.equal(e.selectTrigger("right", 90), false);
  out = active(e, pad({ rt: rawTrigger(.9) }), 35.7);
  assert.equal(out.trigger_changed, false); close(out.gripper_target, 35.7);
});

test("both shoulders are ignored and held shoulder release cannot pick a winner", () => {
  const e = new GamepadInputEngine(); e.reset(40, pad());
  let out = active(e, pad({ pressed: [4, 5], lt: 1, rt: 1 }));
  assert.equal(e.activeTrigger, "left"); assert.equal(out.trigger_changed, false);
  out = active(e, pad({ pressed: [5], rt: 1 }));
  assert.equal(e.activeTrigger, "left"); assert.equal(out.trigger_changed, false);
  active(e, pad());
  out = active(e, pad({ pressed: [5] }));
  assert.equal(e.activeTrigger, "right"); assert.equal(out.trigger_changed, true);
  out = active(e, pad({ pressed: [5] })); assert.equal(out.trigger_changed, false);
});

test("background shoulders only baseline edges; focused inactive setup may select without motion", () => {
  const e = new GamepadInputEngine(); e.reset(40, pad());
  let out = e.sample(pad({ pressed: [5], rt: 1 }), { enabled: false, heldOpening: 35, allowSelection: false });
  assert.equal(e.activeTrigger, "left"); assert.equal(out.trigger_changed, false);
  out = e.sample(pad({ pressed: [5], rt: 1 }), { enabled: false, heldOpening: 35, allowSelection: true });
  assert.equal(e.activeTrigger, "left"); assert.equal(out.trigger_changed, false);
  e.sample(pad(), { enabled: false, allowSelection: true });
  out = e.sample(pad({ pressed: [5], rt: 1, axes: [0, -1, 0, 0] }), { enabled: false, heldOpening: 35, allowSelection: true });
  assert.equal(e.activeTrigger, "right"); assert.equal(out.trigger_changed, true);
  assert.equal(out.gripper_target, null); assert.deepEqual(out.translation, [0, 0, 0]);
  assert.equal(e.gripperState, "pickup"); close(e.heldClosing, .65);
  e.reset(35, pad({ rt: 1 }));
  assert.equal(e.activeTrigger, "right"); close(active(e, pad({ rt: 1 }), 35).gripper_target, 35);
});

test("trigger switch outranks X, but Stop/Home outrank trigger selection", () => {
  const e = new GamepadInputEngine(); e.reset(40, pad());
  let out = active(e, pad({ pressed: [2, 5], rt: 1 }), 32);
  assert.equal(out.trigger_changed, true); assert.equal(e.activeTrigger, "right");
  assert.equal(e.gripperState, "pickup"); close(out.gripper_target, 32);
  assert.deepEqual(out.actions, ["griplock"]);
  active(e, pad());
  out = active(e, pad({ pressed: [3, 4], lt: 1 }), 32);
  assert.equal(e.activeTrigger, "right"); assert.equal(out.trigger_changed, false);
  assert.equal(out.gripper_target, null); assert.deepEqual(out.translation, [0, 0, 0]);
  active(e, pad());
  out = active(e, pad({ pressed: [1, 4], lt: 1 }), 32);
  assert.equal(e.activeTrigger, "right"); assert.equal(out.trigger_changed, false);
  assert.deepEqual(out.actions, ["stop"]);
});
