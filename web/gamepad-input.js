/** Pure standard-Gamepad interpretation. No DOM, device polling, clocks or I/O.
 * The caller owns permission, transport, physical limits and target-rate handling.
 */

export const GAMEPAD_BUTTON_ACTIONS = Object.freeze({
  0: "start", 1: "stop", 2: "griplock", 3: "home",
  8: "help", 9: "nextcameralayout", 10: "nextcamera", 11: "nextspeed",
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
    this.activeTrigger = "left";
    this.bodyNeutral = false;
    this.resume_required = true;
    this._heldOpening = null;
    this._lastGrip = 0;
    this._pickupArmed = false;
    this._buttons = null;
    this._identity = null;
    this._sourceValid = false;
    this._reason = "Explicit Start required";
  }

  get liveGrip() {
    return this.activeTrigger === "right" ? this.liveRT : this.liveLT;
  }

  _readControls(source) {
    this.liveLT = source.lt;
    this.liveRT = source.rt;
    // Opposed D-pad buttons still mean a held body control, even if they cancel.
    this.bodyNeutral = source.left.every(value => value === 0) &&
      source.right.every(value => value === 0) && !source.pressed.slice(12, 16).some(Boolean);
  }

  /** Explicit side selection; same-side selection is idempotent. During an
   * active stream the caller supplies its admitted jaw target. The existing
   * seed is a fallback for idle UI setup, never a guessed midpoint.
   * Onscreen callers handle the returned change immediately. Only physical
   * shoulder selection emits trigger_changed in the sample that observes it.
   */
  selectTrigger(side, heldOpening = null) {
    if (side !== "left" && side !== "right") throw new RangeError("Trigger side must be left or right");
    if (side === this.activeTrigger) return false;
    const seed = opening(heldOpening) ?? this._heldOpening;
    this.activeTrigger = side;
    this._pickup(seed, this.liveGrip);
    this._reason = seed === null ? "Admitted gripper opening unavailable" : "";
    return true;
  }

  _setHeld(value) {
    this._heldOpening = opening(value);
    this.heldClosing = this._heldOpening === null ? null : 1 - this._heldOpening / 100;
    this.pickupThreshold = this.heldClosing === null ? null : Math.min(1, this.heldClosing + this.pickupMargin);
  }

  _pickup(value, selectedValue) {
    this._setHeld(value);
    this.gripperState = "pickup";
    this._lastGrip = clamp(selectedValue, 0, 1);
    this._pickupArmed = this.pickupThreshold !== null && this._lastGrip < this.pickupThreshold;
  }

  /** Call after successful explicit Start, using the current admitted opening.
   * Supplying the current sample primes edges; held Start/X cannot auto-fire.
   * null opening is allowed for preview, but cannot produce robot intents.
   * The selected trigger side is preserved across Start/reset.
   */
  reset(seedOpening, currentSample = null) {
    this._pickup(seedOpening, 0);
    this.liveLT = 0;
    this.liveRT = 0;
    this.bodyNeutral = false;
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
      this.bodyNeutral = false;
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
    this._readControls(source);
    this._lastGrip = this.liveGrip;
    if (this.gripperState === "pickup") {
      this._pickupArmed = this.pickupThreshold !== null && this.liveGrip < this.pickupThreshold;
    }
    return true;
  }

  /** Onscreen X entry. Caller supplies the admitted jaw target, not measured lag.
   * Returns false if that target is unknown; never invents a 50%/0% reference.
   * Physical button X calls this only during an enabled, resumed stream.
   */
  toggleGripper(currentOpening, selectedValue = this.liveGrip) {
    if (opening(currentOpening) === null || typeof selectedValue !== "number" || !Number.isFinite(selectedValue)) {
      this._reason = "Admitted gripper opening unavailable";
      return false;
    }
    if (this.gripperState === "locked") {
      this._pickup(currentOpening, selectedValue);
    } else {
      this._setHeld(currentOpening);
      this.gripperState = "locked";
      this._lastGrip = clamp(selectedValue, 0, 1);
      this._pickupArmed = false;
    }
    this._reason = "";
    return true;
  }

  _empty(source, actions = [], sourceLost = false, triggerChanged = false) {
    return {
      translation: [0, 0, 0], elevation: 0, roll: 0, speed_scale: 0,
      gripper_target: null, actions, valid: source.valid, connected: source.connected,
      source_lost: sourceLost, resume_required: this.resume_required,
      trigger_changed: triggerChanged,
      reason: source.reason || this._reason,
    };
  }

  sample(raw, { enabled = false, heldOpening = null, allowSelection = true } = {}) {
    const source = inspect(raw, this.stickDeadzone, this.triggerDeadzone);
    if (!source.valid) {
      const lost = this._sourceValid;
      this._sourceValid = false;
      this._buttons = null;
      this._identity = null;
      this.liveLT = 0;
      this.liveRT = 0;
      this.bodyNeutral = false;
      this.resume_required = true;
      this._reason = source.reason;
      return this._empty(source, [], lost);
    }
    this._readControls(source);
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
    const leftSelected = source.pressed[4] && !this._buttons[4];
    const rightSelected = source.pressed[5] && !this._buttons[5];
    this._buttons = [...source.pressed];
    if (actions.includes("stop")) {
      this.resume_required = true;
      this._reason = "Stopped; explicit Start required";
      this._lastGrip = this.liveGrip;
      return this._empty(source, ["stop"]);
    }
    if (actions.includes("home")) {
      this._lastGrip = this.liveGrip;
      return this._empty(source, actions);
    }
    let triggerChanged = false;
    if (allowSelection && !(source.pressed[4] && source.pressed[5])) {
      if (leftSelected) triggerChanged = this.selectTrigger("left", heldOpening);
      else if (rightSelected) triggerChanged = this.selectTrigger("right", heldOpening);
    }
    const active = enabled === true && !this.resume_required && this._heldOpening !== null;
    if (!active) {
      this._lastGrip = this.liveGrip;
      return this._empty(source, actions, false, triggerChanged);
    }
    // Selecting the trigger is an explicit pickup transition, even from locked.
    // A simultaneous X edge must not immediately relock the new selection.
    if (actions.includes("griplock") && !triggerChanged) this.toggleGripper(heldOpening);

    const selectedValue = this.liveGrip;

    if (this.gripperState === "pickup" && this.pickupThreshold !== null) {
      // If pickup began at/above its threshold, require a release/repress. A
      // small hysteresis avoids interpreting threshold noise as that gesture.
      const releaseLevel = Math.max(0, this.pickupThreshold - this.triggerDeadzone);
      if (!this._pickupArmed && selectedValue <= releaseLevel) this._pickupArmed = true;
      if (this._pickupArmed && this._lastGrip < this.pickupThreshold && selectedValue >= this.pickupThreshold && selectedValue > this._lastGrip) {
        this.gripperState = "following";
      }
    }
    this._lastGrip = selectedValue;
    const gripperTarget = this.gripperState === "following" ? 100 * (1 - selectedValue) : this._heldOpening;
    let translation = [-source.right[1], -source.right[0], -source.left[1]].map(value => value === 0 ? 0 : value);
    const norm = Math.hypot(...translation);
    if (norm > 1) translation = translation.map(value => value / norm);
    const elevation = (Number(source.pressed[12]) - Number(source.pressed[13])) * this.dpadRollScale;
    const dpadRoll = source.pressed[14] || source.pressed[15];
    const roll = dpadRoll ? (Number(source.pressed[15]) - Number(source.pressed[14])) * this.dpadRollScale : source.left[0];
    return {
      translation, elevation, roll, speed_scale: this.bodyNeutral ? 0 : 1,
      gripper_target: gripperTarget, actions, valid: true, connected: true,
      source_lost: false, resume_required: false, reason: this._reason,
      trigger_changed: triggerChanged,
    };
  }
}
