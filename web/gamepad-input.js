/** Pure standard-Gamepad interpretation. No DOM, device polling, clocks or I/O.
 * The caller owns permission, transport, physical limits and target-rate handling.
 */

export const GAMEPAD_BUTTON_ACTIONS = Object.freeze({
  0: "start", 1: "stop", 2: "griplock", 3: "home",
  4: "speeddown", 5: "speedup", 8: "help", 9: "nextcamera",
  10: "resetFKview", 11: "nextcameralayout",
});

const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
const opening = value => typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 100 ? value : null;

function setting(value, name, low, high, inclusiveLow = true) {
  if (typeof value !== "number" || !Number.isFinite(value) ||
      (inclusiveLow ? value < low : value <= low) || value > high) {
    throw new RangeError(`Invalid ${name}`);
  }
  return value;
}

function radial(x, y, deadzone) {
  const magnitude = Math.hypot(x, y);
  if (magnitude <= deadzone) return [0, 0];
  const fraction = Math.min(1, (magnitude - deadzone) / (1 - deadzone));
  return [x / magnitude * fraction, y / magnitude * fraction];
}

function trigger(value, deadzone) {
  return value <= deadzone ? 0 : (value - deadzone) / (1 - deadzone);
}

function inspect(raw, stickDeadzone, triggerDeadzone) {
  if (!raw || raw.connected === false) return { valid: false, connected: false, reason: "Gamepad disconnected" };
  if (raw.mapping !== "standard") return { valid: false, connected: true, reason: "Unsupported gamepad mapping; standard mapping required" };
  if (!raw.axes || raw.axes.length < 4 || !raw.buttons || raw.buttons.length < 16) {
    return { valid: false, connected: true, reason: "Incomplete standard gamepad sample" };
  }
  const axes = Array.from(raw.axes).slice(0, 4);
  if (axes.some(v => typeof v !== "number" || !Number.isFinite(v))) {
    return { valid: false, connected: true, reason: "Invalid gamepad axis value" };
  }
  const pressed = [];
  const values = [];
  for (let i = 0; i < 16; i += 1) {
    const button = raw.buttons[i];
    if (!button || typeof button.value !== "number" || !Number.isFinite(button.value)) {
      return { valid: false, connected: true, reason: "Invalid gamepad button value" };
    }
    values.push(clamp(button.value, 0, 1));
    pressed.push(button.pressed === true || button.value >= 0.5);
  }
  const left = radial(clamp(axes[0], -1, 1), clamp(axes[1], -1, 1), stickDeadzone);
  const right = radial(clamp(axes[2], -1, 1), clamp(axes[3], -1, 1), stickDeadzone);
  return {
    valid: true, connected: true, reason: "", pressed, left, right,
    lt: trigger(values[6], triggerDeadzone), rt: trigger(values[7], triggerDeadzone),
    identity: `${raw.index ?? ""}:${raw.id ?? ""}`,
  };
}

export class GamepadInputEngine {
  constructor({ stickDeadzone = 0.08, triggerDeadzone = 0.02, pickupMargin = 0.03, dpadRollScale = 0.2 } = {}) {
    this.stickDeadzone = setting(stickDeadzone, "stickDeadzone", 0, 0.99);
    this.triggerDeadzone = setting(triggerDeadzone, "triggerDeadzone", 0, 0.99);
    this.pickupMargin = setting(pickupMargin, "pickupMargin", 0, 1, false);
    this.dpadRollScale = setting(dpadRollScale, "dpadRollScale", 0, 1);
    this.gripperState = "pickup";
    this.heldClosing = null;
    this.pickupThreshold = null;
    this.liveLT = 0;
    this.liveRT = 0;
    this.resume_required = true;
    this._heldOpening = null;
    this._lastLT = 0;
    this._pickupArmed = false;
    this._buttons = null;
    this._identity = null;
    this._sourceValid = false;
    this._reason = "Explicit Start required";
  }

  _setHeld(value) {
    this._heldOpening = opening(value);
    this.heldClosing = this._heldOpening === null ? null : 1 - this._heldOpening / 100;
    this.pickupThreshold = this.heldClosing === null ? null : Math.min(1, this.heldClosing + this.pickupMargin);
  }

  _pickup(value, lt) {
    this._setHeld(value);
    this.gripperState = "pickup";
    this._lastLT = clamp(lt, 0, 1);
    this._pickupArmed = this.pickupThreshold !== null && this._lastLT < this.pickupThreshold;
  }

  /** Call after successful explicit Start, using the current admitted opening.
   * Supplying the current sample primes edges; held Start/X cannot auto-fire.
   * null opening is allowed for preview, but cannot produce robot intents.
   */
  reset(seedOpening, currentSample = null) {
    this._pickup(seedOpening, 0);
    this.liveLT = 0;
    this.liveRT = 0;
    this._buttons = null;
    this._identity = null;
    this._sourceValid = false;
    this.resume_required = false;
    this._reason = this._heldOpening === null ? "Admitted gripper opening unavailable" : "";
    if (currentSample !== null) this.resetButtonBaseline(currentSample);
    return this;
  }

  /** Prime on selection/connection only, never every animation frame.
   * This does not clear the reconnect/Stop latch; successful Start uses reset().
   */
  resetButtonBaseline(raw) {
    const source = inspect(raw, this.stickDeadzone, this.triggerDeadzone);
    if (!source.valid) {
      this._buttons = null;
      this._sourceValid = false;
      this.resume_required = true;
      this._reason = source.reason;
      return false;
    }
    if (this._identity !== null && this._identity !== source.identity) {
      this.resume_required = true;
      this._reason = "Controller changed; explicit Start required";
    }
    this._buttons = [...source.pressed];
    this._identity = source.identity;
    this._sourceValid = true;
    this.liveLT = source.lt;
    this.liveRT = source.rt;
    this._lastLT = source.lt;
    if (this.gripperState === "pickup") {
      this._pickupArmed = this.pickupThreshold !== null && source.lt < this.pickupThreshold;
    }
    return true;
  }

  /** Onscreen X entry. Caller supplies the admitted jaw target, not measured lag.
   * Returns false if that target is unknown; never invents a 50%/0% reference.
   * Physical button X calls this only during an enabled, resumed stream.
   */
  toggleGripper(currentOpening, LT = this.liveLT) {
    if (opening(currentOpening) === null || typeof LT !== "number" || !Number.isFinite(LT)) {
      this._reason = "Admitted gripper opening unavailable";
      return false;
    }
    if (this.gripperState === "locked") {
      this._pickup(currentOpening, LT);
    } else {
      this._setHeld(currentOpening);
      this.gripperState = "locked";
      this._lastLT = clamp(LT, 0, 1);
      this._pickupArmed = false;
    }
    this._reason = "";
    return true;
  }

  _empty(source, actions = [], sourceLost = false) {
    return {
      translation: [0, 0, 0], elevation: 0, roll: 0, speed_scale: 0,
      gripper_target: null, actions, valid: source.valid, connected: source.connected,
      source_lost: sourceLost, resume_required: this.resume_required,
      reason: source.reason || this._reason,
    };
  }

  sample(raw, { enabled = false, heldOpening = null } = {}) {
    const source = inspect(raw, this.stickDeadzone, this.triggerDeadzone);
    if (!source.valid) {
      const lost = this._sourceValid;
      this._sourceValid = false;
      this._buttons = null;
      this._identity = null;
      this.liveLT = 0;
      this.liveRT = 0;
      this.resume_required = true;
      this._reason = source.reason;
      return this._empty(source, [], lost);
    }
    this.liveLT = source.lt;
    this.liveRT = source.rt;
    const changed = this._identity !== null && this._identity !== source.identity;
    if (changed) {
      this.resume_required = true;
      this._reason = "Controller changed; explicit Start required";
      this._buttons = null;
    }
    if (this._buttons === null) {
      this.resetButtonBaseline(raw);
      return this._empty(source, [], changed);
    }
    this._sourceValid = true;
    const actions = Object.entries(GAMEPAD_BUTTON_ACTIONS)
      .filter(([index]) => source.pressed[index] && !this._buttons[index])
      .map(([, action]) => action);
    this._buttons = [...source.pressed];
    if (actions.includes("stop")) {
      this.resume_required = true;
      this._reason = "Stopped; explicit Start required";
      this._lastLT = source.lt;
      return this._empty(source, ["stop"]);
    }
    const active = enabled === true && !this.resume_required && this._heldOpening !== null;
    if (!active || actions.includes("home")) {
      this._lastLT = source.lt;
      return this._empty(source, actions);
    }
    if (actions.includes("griplock")) this.toggleGripper(heldOpening, source.lt);

    if (this.gripperState === "pickup" && this.pickupThreshold !== null) {
      // If pickup began at/above its threshold, require a release/repress. A
      // small hysteresis avoids interpreting threshold noise as that gesture.
      const releaseLevel = Math.max(0, this.pickupThreshold - this.triggerDeadzone);
      if (!this._pickupArmed && source.lt <= releaseLevel) this._pickupArmed = true;
      if (this._pickupArmed && this._lastLT < this.pickupThreshold && source.lt >= this.pickupThreshold && source.lt > this._lastLT) {
        this.gripperState = "following";
      }
    }
    this._lastLT = source.lt;
    const gripperTarget = this.gripperState === "following" ? 100 * (1 - source.lt) : this._heldOpening;
    let translation = [-source.right[1], -source.right[0], -source.left[1]].map(value => value === 0 ? 0 : value);
    const norm = Math.hypot(...translation);
    if (norm > 1) translation = translation.map(value => value / norm);
    const elevation = (Number(source.pressed[12]) - Number(source.pressed[13])) * this.dpadRollScale;
    const dpadRoll = source.pressed[14] || source.pressed[15];
    const roll = dpadRoll ? (Number(source.pressed[15]) - Number(source.pressed[14])) * this.dpadRollScale : source.left[0];
    return {
      translation, elevation, roll, speed_scale: source.rt,
      gripper_target: gripperTarget, actions, valid: true, connected: true,
      source_lost: false, resume_required: false, reason: this._reason,
    };
  }
}
