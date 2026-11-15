import { GamepadInputEngine } from './gamepad-input.js';

const copy = value => structuredClone(value);
const percent = value => Number.isFinite(value) ? Math.round(value * 100) : 0;
const activeRate = output => output && output.speed_scale > 0 &&
  (output.translation.some(value => value !== 0) || output.elevation !== 0 || output.roll !== 0);
const motionRates = output => [...output.translation, output.elevation, output.roll]
  .map(value => value * output.speed_scale);
const neutralOrReversed = (before, after, epsilon = 1e-7) =>
  Math.abs(before) > epsilon && (Math.abs(after) <= epsilon || Math.sign(before) !== Math.sign(after));

/** Browser device reader and view. All actuation uses the parent ManualPanel. */
export class GamepadPanel {
  constructor({ element, manual, onCameraCycle = () => {}, onCameraLayout = () => {}, env = {} }) {
    Object.assign(this, { element, manual, onCameraCycle, onCameraLayout });
    this.env = {
      read: () => globalThis.navigator?.getGamepads?.() || [],
      supported: () => typeof globalThis.navigator?.getGamepads === 'function',
      raf: fn => requestAnimationFrame(fn),
      now: () => performance.now(),
      focused: () => !document.hidden && document.hasFocus(),
      ...env,
    };
    this.engine = new GamepadInputEngine();
    this.selectedKey = ''; this.raw = null; this.output = null;
    this.active = false; this.sourceEpoch = 0; this.needsNeutral = true;
    this.lastSolveAt = null; this.solving = false; this.pendingMotion = null; this.inventoryKey = '';
    this.disconnected = false; this.renderAt = -Infinity; this.errorMessage = ''; this.deviceReadError = '';
    element.innerHTML = `<label class="field-label">Controller<select id="gamepad-device" aria-label="Game controller"><option value="">Press a controller button to detect it</option></select></label>
      <div class="gamepad-device-line"><span id="gamepad-device-state">Waiting for a controller</span><button type="button" id="gamepad-refresh" class="text-button">Refresh</button></div>
      <div class="gamepad-trigger-grid">
        <section><div class="small-heading">Grip trigger <span data-pad-key="lt">LT / L2</span></div><div class="gamepad-value"><strong id="gamepad-grip-value">—</strong><span id="gamepad-grip-state">Waiting</span></div><div class="gamepad-meter" role="meter" aria-label="Gripper trigger squeeze" aria-valuemin="0" aria-valuemax="100" id="gamepad-grip-meter"><i></i><b id="gamepad-pickup-marker" hidden></b></div><p id="gamepad-grip-note" class="detail-note">Opening stays held until trigger pickup.</p><button type="button" id="gamepad-grip-lock" class="manual-toggle" disabled>Lock gripper <kbd data-pad-key="lock">X / □</kbd></button></section>
        <section><div class="small-heading">Speed ceiling <span data-pad-key="rt">RT / R2</span></div><div class="gamepad-value"><strong id="gamepad-speed-value">—</strong><span id="gamepad-speed-cap">— mm/s</span></div><div class="gamepad-meter" role="meter" aria-label="Arm speed trigger" aria-valuemin="0" aria-valuemax="100" id="gamepad-speed-meter"><i></i></div><div class="segmented gamepad-speeds" role="group" aria-label="Gamepad maximum speed">${[1,2,3,4,5].map(value=>`<button type="button" data-gamepad-speed="${value}">${value}</button>`).join('')}</div><p class="detail-note">Trigger sets the range; sticks choose a fraction.</p></section>
      </div>
      <div class="gamepad-motion-readout"><span>Right stick <b>X / Y</b></span><span>Left stick <b>Z / roll</b></span><span>D-pad <b>Fine tilt / roll</b></span></div>
      <button type="button" class="text-button" id="gamepad-bindings-toggle" aria-expanded="false">Show controller bindings</button>
      <div id="gamepad-bindings" class="gamepad-bindings" hidden><dl>
        <div><dt>A / Cross</dt><dd>Start control</dd></div><div><dt>B / Circle</dt><dd>Stop / hold</dd></div><div><dt>X / Square</dt><dd>Lock / unlock gripper</dd></div><div><dt>Y / Triangle</dt><dd>Home</dd></div>
        <div><dt>LB / RB · L1 / R1</dt><dd>Speed profile − / +</dd></div><div><dt>Menu / Options</dt><dd>Next primary camera</dd></div><div><dt>View / Create</dt><dd>Show bindings</dd></div><div><dt>Left stick click / L3</dt><dd>Reset FK view</dd></div><div><dt>Right stick click / R3</dt><dd>Next camera layout</dd></div>
      </dl></div>`;
    this.el = id => element.querySelector('#' + id);
    this.el('gamepad-device').onchange = () => manual.safe(() => this.chooseDevice(this.el('gamepad-device').value));
    this.el('gamepad-refresh').onclick = () => { this.inventoryKey = ''; this.poll(); };
    this.el('gamepad-grip-lock').onclick = () => manual.safe(() => this.toggleGripperLock());
    this.el('gamepad-bindings-toggle').onclick = () => this.toggleBindings();
    for (const button of element.querySelectorAll('[data-gamepad-speed]'))
      button.onclick = () => manual.setSpeed(Number(button.dataset.gamepadSpeed));
    window.addEventListener('gamepadconnected', () => { this.inventoryKey = ''; });
    window.addEventListener('gamepaddisconnected', event => {
      this.inventoryKey = '';
      if (event.gamepad && this.raw?.index === event.gamepad.index) this.loseDevice();
    });
    window.addEventListener('blur', () => this.suspend());
    document.addEventListener('visibilitychange', () => { if (document.hidden) this.suspend(); });
    this.frame = () => { this.poll(); this.env.raf(this.frame); };
    this.env.raf(this.frame);
  }
  key(raw) { return `${raw.index}:${raw.id}`; }
  heldOpening() {
    const measured = this.manual.measured();
    const held = measured && typeof this.manual.preservedGripper === 'function'
      ? this.manual.preservedGripper(measured)
      : this.manual.target?.gripper ?? measured?.gripper;
    return Number.isFinite(held) && held >= 0 && held <= 100 ? held : null;
  }
  toggleGripperLock(alreadyToggled = false) {
    const m = this.manual;
    if (!m.isOwnStream() || m.mode !== 'gamepad' || m.busy || !this.output?.valid || !m.target) return false;
    const opening = this.heldOpening();
    if (opening === null) return false;
    // Physical X was toggled by sample(); an onscreen click uses this same path
    // but toggles once here. Never capture the trigger's new requested target.
    if (!alreadyToggled && !this.engine.toggleGripper(opening, this.engine.liveLT)) return false;
    this.sourceEpoch++; this.lastSolveAt = null; this.pendingMotion = null;
    const target = { ...copy(m.target), gripper: opening, gripper_unit: 'percent' };
    const reference = { ...copy(m.reference || m.target), gripper: opening, gripper_unit: 'percent' };
    if (m.client.setManualTarget(copy(target)) === false) return false;
    m.target = target; m.reference = reference;
    this.output = { ...this.output, gripper_target: opening };
    this.render();
    return true;
  }
  inputInvalidates(pending, current) {
    if (!current?.valid || current.resume_required) return true;
    const nextRates = motionRates(current);
    if (pending.rates.some((value, index) => neutralOrReversed(value, nextRates[index]))) return true;
    if (Number.isFinite(pending.gripperTarget)) {
      if (!Number.isFinite(current.gripper_target)) return true;
      if (neutralOrReversed(pending.gripperTarget - pending.referenceGripper,
          current.gripper_target - pending.referenceGripper, .0001)) return true;
    }
    return false;
  }
  gripperResultIsCurrent(pending, result) {
    const current = this.output?.gripper_target;
    const proposed = result?.target?.gripper;
    if (!Number.isFinite(current) || !Number.isFinite(proposed)) return true;
    // Same-direction LT changes need not starve inference, but an older result
    // may not overshoot the latest absolute opening requested by the operator.
    const low = Math.min(pending.referenceGripper, current) - .0001;
    const high = Math.max(pending.referenceGripper, current) + .0001;
    return proposed >= low && proposed <= high;
  }
  isFocused() { return this.active && this.env.focused(); }
  updateControls() {
    this.el('gamepad-device').disabled = this.manual.busy;
    this.el('gamepad-grip-lock').disabled = !this.manual.isOwnStream() || !this.raw || !this.output?.valid;
    for (const button of this.element.querySelectorAll('[data-gamepad-speed]'))
      button.classList.toggle('active', Number(button.dataset.gamepadSpeed) === this.manual.level);
  }
  canStart() { return !!this.raw && this.output?.valid === true && this.engine.liveRT === 0 && !this.disconnected; }
  startReason() {
    return this.deviceReadError ? `Controller read failed: ${this.deviceReadError}` : !this.raw ? 'Select a connected controller first.' : !this.output?.valid ? this.output?.reason || 'Controller mapping unavailable.' : this.engine.liveRT > 0 ? 'Release RT / R2 before starting.' : '';
  }
  setActive(active) {
    if (this.active === active) return;
    this.active = active;
    this.suspend();
    if (active) this.poll();
  }
  resetForArm() { this.sourceEpoch++; this.pendingMotion = null; this.output = null; this.needsNeutral = true; this.engine.reset(this.heldOpening(), this.raw); }
  suspend() {
    this.sourceEpoch++; this.lastSolveAt = null; this.pendingMotion = null; this.needsNeutral = true; this.output = null;
    const locked = this.engine.gripperState === 'locked';
    this.engine.reset(this.heldOpening(), this.raw);
    if (locked) this.engine.toggleGripper(this.heldOpening(), this.engine.liveLT);
  }
  operationStarted() { this.disconnected = false; this.suspend(); this.render(); }
  operationEnded() { this.suspend(); this.engine.resume_required = true; this.render(); }
  async chooseDevice(key) {
    if (key === this.selectedKey) return;
    this.sourceEpoch++;
    if (this.manual.mode === 'gamepad' && (this.manual.isOwnStream() || this.manual.busy)) await this.manual.stop();
    this.selectedKey = key; this.disconnected = false; this.raw = null;
    this.engine = new GamepadInputEngine(); this.output = null; this.needsNeutral = true;
    this.poll();
  }
  loseDevice() {
    if (this.disconnected) return;
    this.disconnected = true; this.sourceEpoch++; this.pendingMotion = null; this.raw = null; this.output = null;
    this.engine.sample(null); this.lastSolveAt = null;
    if (this.manual.mode === 'gamepad' && (this.manual.isOwnStream() || this.manual.busy))
      this.manual.safe(() => this.manual.stop());
    if (this.manual.mode === 'gamepad') this.manual.note('Controller disconnected. Select it and Start again to resume.');
    this.render();
  }
  poll() {
    if (this.manual.mode !== 'gamepad') return;
    let pads;
    try { pads = Array.from(this.env.read()).filter(pad => pad && pad.connected !== false); this.deviceReadError = ''; }
    catch (error) { pads = []; this.deviceReadError = String(error?.message || error || 'Controller access failed').slice(0, 512); }
    const signature = JSON.stringify(pads.map(pad => [pad.index,pad.id,pad.mapping]));
    if (signature !== this.inventoryKey) {
      this.inventoryKey = signature;
      const select = this.el('gamepad-device');
      select.replaceChildren(new Option(pads.length ? 'Select a controller' : 'Press a controller button to detect it', ''),
        ...pads.map(pad => new Option(pad.id, this.key(pad))));
      if (!this.selectedKey && pads.length === 1 && !this.disconnected) this.selectedKey = this.key(pads[0]);
      select.value = pads.some(pad => this.key(pad) === this.selectedKey) ? this.selectedKey : '';
    }
    const raw = pads.find(pad => this.key(pad) === this.selectedKey) || null;
    if (this.raw && !raw) this.loseDevice();
    const previous = this.output;
    if (raw && (!this.raw || this.key(this.raw) !== this.key(raw))) {
      this.raw = raw; this.sourceEpoch++; this.engine.resetButtonBaseline(raw);
      // Discovery/reconnection is display only. Start still has to be explicit.
      this.disconnected = false; this.needsNeutral = true;
    } else this.raw = raw;
    const focused = this.isFocused();
    if (this.wasFocused && !focused) this.suspend();
    this.wasFocused = focused;
    this.output = this.engine.sample(raw, {
      enabled: focused && this.manual.mode === 'gamepad' && this.manual.isOwnStream() && !this.manual.busy,
      heldOpening: this.heldOpening(),
    });
    if (this.output.source_lost) { this.loseDevice(); return; }
    if (focused && this.output.valid && this.needsNeutral && this.engine.liveRT === 0) this.needsNeutral = false;
    if (this.pendingMotion && this.inputInvalidates(this.pendingMotion, this.output)) {
      this.sourceEpoch++; this.lastSolveAt = null; this.pendingMotion = null;
    }
    if (activeRate(previous) && !activeRate(this.output)) { this.sourceEpoch++; this.lastSolveAt = null; }
    if (focused && this.output.valid) this.handleActions(this.output.actions);
    if (focused && !this.needsNeutral) void this.solve();
    if (this.env.now() - this.renderAt >= 50) { this.renderAt = this.env.now(); this.render(); }
  }
  handleActions(actions) {
    const m=this.manual;
    if (actions.includes('stop')) { this.sourceEpoch++; m.safe(() => m.stop()); return; }
    if (actions.includes('home')) { this.suspend(); m.safe(() => m.preset('home')); return; }
    if (actions.includes('start') && !m.isOwnStream() && this.canStart()) m.safe(() => m.start());
    if (actions.includes('griplock')) this.toggleGripperLock(true);
    if (actions.includes('speeddown')) m.setSpeed(Math.max(1,m.level-1));
    if (actions.includes('speedup')) m.setSpeed(Math.min(5,m.level+1));
    if (actions.includes('help')) this.toggleBindings();
    if (actions.includes('nextcamera')) this.onCameraCycle();
    if (actions.includes('resetFKview')) m.el('manual-fk').querySelector('[data-reset]').click();
    if (actions.includes('nextcameralayout')) this.onCameraLayout();
  }
  toggleBindings() {
    const box=this.el('gamepad-bindings'); box.hidden=!box.hidden;
    this.el('gamepad-bindings-toggle').textContent=box.hidden?'Show controller bindings':'Hide controller bindings';
    this.el('gamepad-bindings-toggle').setAttribute('aria-expanded',String(!box.hidden));
  }
  async solve() {
    const m=this.manual,o=this.output;
    if (!o?.valid || o.resume_required || !m.isOwnStream() || !m.client.currentOperation?.ready || m.mode!=='gamepad' || m.busy || this.solving || !m.target) return;
    const measured=m.measured(); if (!measured || !m.stateForArm().feedback_valid) return;
    const reference=m.reference || m.target;
    if (!activeRate(o) && (o.gripper_target===null || Math.abs(o.gripper_target-reference.gripper)<.0001)) { this.lastSolveAt=null; return; }
    this.solving=true;
    const sourceEpoch=this.sourceEpoch,epoch=m.epoch,context=m.intentContext(),key=this.selectedKey;
    const pending = { rates: motionRates(o), gripperTarget: o.gripper_target, referenceGripper: reference.gripper };
    this.pendingMotion = pending;
    const time=this.env.now(),dt=this.lastSolveAt===null?1/60:Math.min(.1,Math.max(0,(time-this.lastSolveAt)/1000));
    this.lastSolveAt=time;
    try {
      const result=await m.client.request(`/api/manual/${encodeURIComponent(context.arm)}/analog-step`,{
        ...measured,translation:o.translation,elevation:o.elevation,roll:o.roll,speed_scale:o.speed_scale,
        gripper_target:o.gripper_target,dt_s:dt,level:m.level,
        reference_positions:reference.positions,reference_gripper:reference.gripper,
      });
      if (sourceEpoch!==this.sourceEpoch || epoch!==m.epoch || key!==this.selectedKey || !m.currentIntent(context) || !m.isOwnStream() || !this.isFocused()) return;
      if (this.inputInvalidates(pending, this.output) || !this.gripperResultIsCurrent(pending, result)) return;
      if (result.target) { m.client.setManualTarget(copy(result.target)); m.target=copy(result.target); m.reference=copy(result.target); }
      m.note(result.status==='BLOCKED'?'Requested motion is unavailable at this pose.':result.step_fraction<1?'Gamepad active · smaller step near the reach boundary.':'Gamepad active · release RT / R2 to pause arm motion.');
      this.errorMessage='';
    } catch(error) {
      if (sourceEpoch!==this.sourceEpoch || !m.currentIntent(context)) return;
      if (error.status===409) return;
      this.suspend(); this.errorMessage=error.message; m.onError(error);
    } finally { this.solving=false; if (this.pendingMotion === pending) this.pendingMotion = null; }
  }
  render() {
    const valid=!!this.raw && this.output?.valid===true;
    this.el('gamepad-device-state').textContent=this.deviceReadError?`Controller read failed: ${this.deviceReadError}`:!this.env.supported()?'Gamepad input is unavailable in this browser.':
      !this.raw?'Waiting for a controller':!valid?this.output?.reason||'Mapping unavailable':this.needsNeutral?'Release RT / R2 to resume':this.manual.isOwnStream()?'Controller active':'Connected · ready to Start';
    const labels={lt:'LT / L2',rt:'RT / R2',lock:'X / □'};
    for(const node of this.element.querySelectorAll('[data-pad-key]'))node.textContent=labels[node.dataset.padKey];
    this.el('gamepad-grip-value').textContent=valid?`${percent(this.engine.liveLT)}%`:'—';
    this.el('gamepad-speed-value').textContent=valid?`${percent(this.engine.liveRT)}%`:'—';
    const level=this.manual.descriptor?.speed_levels?.find(value=>value.level===this.manual.level);
    this.el('gamepad-speed-cap').textContent=valid&&level?`${(level.translation_m_s*this.engine.liveRT*1000).toFixed(1)} mm/s`:'— mm/s';
    for(const [id,value]of[['gamepad-grip-meter',this.engine.liveLT],['gamepad-speed-meter',this.engine.liveRT]]){
      const meter=this.el(id);meter.querySelector('i').style.width=`${valid?percent(value):0}%`;meter.setAttribute('aria-valuenow',String(valid?percent(value):0));
    }
    const state=this.engine.gripperState;
    this.el('gamepad-grip-state').textContent=!this.manual.isOwnStream()?'Not started':state==='locked'?'Locked':state==='pickup'?'Waiting for pickup':'Following';
    const marker=this.el('gamepad-pickup-marker');marker.hidden=!valid||state!=='pickup'||this.engine.pickupThreshold===null;
    marker.style.left=`${percent(this.engine.pickupThreshold)}%`;
    this.el('gamepad-grip-note').textContent=!this.manual.isOwnStream()?'Start holds the current opening.':state==='locked'?`Opening held at ${Math.round(this.heldOpening()??0)}%.`:state==='pickup'?`Squeeze past ${percent(this.engine.pickupThreshold)}% to take over.`:`Opening target ${Math.round(this.output?.gripper_target??this.heldOpening()??0)}%`;
    const lock=this.el('gamepad-grip-lock');lock.firstChild.textContent=state==='locked'?'Unlock gripper ':'Lock gripper ';lock.setAttribute('aria-pressed',String(state==='locked'));
    this.updateControls();
    if(this.active)this.manual.updateControls();
  }
}
