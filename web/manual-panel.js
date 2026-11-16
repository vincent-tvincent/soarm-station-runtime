import { FKView } from "./fk-view.js";
import { GamepadPanel } from "./gamepad-panel.js";
const AXES = [
  ["X", "KeyK", "K", "KeyJ", "J"],
  ["Y", "KeyL", "L", "KeyH", "H"],
  ["Z", "KeyF", "F", "KeyD", "D"],
  ["Elevation", "KeyI", "I", "KeyU", "U"],
  ["Wrist roll", "KeyG", "G", "KeyS", "S"],
  ["Gripper", "BracketLeft", "[", "BracketRight", "]"],
];
const MOVE_KEYS = new Set(AXES.flatMap((a) => [a[1], a[3]]));
const finite = Number.isFinite;
const copy = (value) => structuredClone(value);
export function jacobianRankPresentation(value, stale = false) {
  if (stale || !value || !Number.isInteger(value.rank) || !Number.isInteger(value.max_rank) ||
      value.max_rank <= 0 || value.rank < 0 || value.rank > value.max_rank)
    return { rank: '—', color: '#666', detail: stale ? 'Waiting for fresh pose' : 'Unavailable' };
  const missing = value.max_rank - value.rank;
  const redness = missing === 0 ? 0 : Math.min(1,.45 + .55 * missing / value.max_rank);
  const rgb = [25,25,25].map((component,i)=>Math.round(component+([217,35,27][i]-component)*redness));
  return { rank: `${value.rank} / ${value.max_rank}`, color: `rgb(${rgb.join(', ')})`,
    detail: `${value.rows || 6} × ${value.columns || value.max_rank} geometric Jacobian · measured pose` };
}
export class ManualPanel {
  constructor({ element, client, onError, onNotice, onCameraCycle, onCameraLayout, gamepadEnv }) {
    Object.assign(this, { element, client, onError, onNotice });
    this.arm = "";
    this.mode = "joint";
    this.followGripper = true;
    this.level = 2;
    this.keys = new Set();
    this.epoch = 0;
    this.intentGeneration = 0;
    this.pendingActions = 0;
    this.expectedTermination = 0;
    this.lastSolveAt = null;
    this.busy = false;
    this.visible = false;
    this.target = null;
    this.dirty = false;
    this.descriptor = null;
    this.hosts = [];
    element.innerHTML = `<label class="field-label manual-mode-label">Control mode<select id="manual-mode" aria-label="Control mode"><option value="joint">Joint targets</option><option value="ik">IK · keyboard jog</option><option value="leader">Leader arm</option><option value="gamepad">Gamepad · Xbox / DualSense</option></select></label>
    <div class="manual-control-area">
      <div data-manual-view="joint"><div class="small-heading">Joint targets <span>radians / gripper %</span></div><div id="manual-joints"></div><button id="reset-targets" class="text-button">Use measured pose</button></div>
      <div data-manual-view="ik" hidden><div class="small-heading">Cartesian jog <span>base frame</span></div><div class="ik-jog-grid">${AXES.map(([name, minus, mKey, plus, pKey]) => `<div class="ik-jog-row"><span>${name}<output data-axis="${name}"></output></span><button data-jog="${minus}" aria-label="${name} ${name === "Gripper" ? "close" : "decrease"}">${name === "Gripper" ? "Close" : "−"} <kbd>${mKey}</kbd></button><button data-jog="${plus}" aria-label="${name} ${name === "Gripper" ? "open" : "increase"}">${name === "Gripper" ? "Open" : "+"} <kbd>${pKey}</kbd></button></div>`).join("")}</div><div class="manual-speed"><div class="small-heading">Speed <span id="ik-speed-label">Normal</span></div><div class="segmented speed-levels" aria-label="IK speed">${[1, 2, 3, 4, 5].map((n) => `<button data-speed="${n}" class="${n === 2 ? "active" : ""}">${n}</button>`).join("")}</div><p class="detail-note">Fine → maximum · hold <kbd>Shift</kbd> for maximum</p></div><button id="ik-position-only" class="manual-toggle" aria-pressed="false">Position only <kbd>Tab</kbd></button><div class="manual-wrist-row"><button data-roll="minus_90">−90° <kbd>R</kbd></button><button data-roll="reference">Reference <kbd>T</kbd></button><button data-roll="plus_90">+90° <kbd>E</kbd></button></div><button id="partial-home" class="text-button">Home · preserve wrist <kbd>Space</kbd></button></div>
      <div data-manual-view="gamepad" id="gamepad-panel" hidden></div>
      <div data-manual-view="leader" hidden><label class="field-label">Input host<select id="leader-host"><option value="">No input hosts connected</option></select></label><label class="field-label">Leader device<select id="leader-device"><option value="">Select an input host</option></select></label><div class="panel-subheading"><span id="leader-status" class="detail-note">Connect an input host to discover devices.</span><button id="refresh-leaders" class="text-button">Refresh</button></div><button id="leader-gripper-follow" class="manual-toggle" aria-pressed="true">Gripper follow <kbd>O</kbd></button><p class="detail-note">The selected leader follows through the station. Home stops following.</p></div>
    </div>
    <div class="manual-actions"><button id="start-manual" class="primary" disabled>Start</button><button id="stop-manual" disabled>Stop <kbd>Esc</kbd></button><button id="home-manual" disabled>Home <kbd>N</kbd></button><button id="fold-manual" disabled>Fold <kbd>M</kbd></button></div><p id="manual-note" class="detail-note" role="status">Targets stay local until you start.</p><div id="manual-fk"></div>`;
    this.el = (id) => element.querySelector("#" + id);
    this.fk = new FKView(this.el("manual-fk"));
    this.gamepad = new GamepadPanel({element:this.el('gamepad-panel'),manual:this,onCameraCycle,onCameraLayout,env:gamepadEnv});
    this.el("manual-mode").onchange = () =>
      this.safe(() => this.changeMode(this.el("manual-mode").value));
    this.el("reset-targets").onclick = () => {
      this.resetTargets();
      if (this.isOwnStream()) this.client.setManualTarget(this.target);
    };
    this.el("start-manual").onclick = () => this.safe(() => this.start());
    this.el("stop-manual").onclick = () => this.safe(() => this.stop());
    this.el("home-manual").onclick = () => this.safe(() => this.preset("home"));
    this.el("fold-manual").onclick = () => this.safe(() => this.preset("fold"));
    this.el("partial-home").onclick = () =>
      this.safe(() => this.preset("home", true));
    this.el("ik-position-only").onclick = () => {
      this.positionOnly = !this.positionOnly;
      this.el("ik-position-only").setAttribute(
        "aria-pressed",
        String(this.positionOnly),
      );
      this.clearKeys();
    };
    for (const b of element.querySelectorAll("[data-speed]"))
      b.onclick = () => this.setSpeed(Number(b.dataset.speed));
    for (const b of element.querySelectorAll("[data-roll]"))
      b.onclick = () => this.safe(() => this.roll(b.dataset.roll));
    for (const b of element.querySelectorAll("[data-jog]")) {
      b.onpointerdown = (e) => {
        if (e.button !== 0 || this.busy || !this.isOwnStream()) return;
        e.preventDefault();
        b.setPointerCapture(e.pointerId);
        if (!this.keys.has(b.dataset.jog)) this.epoch++;
        this.keys.add(b.dataset.jog);
        this.updateKeys();
      };
      const release = () => {
        this.keys.delete(b.dataset.jog);
        this.epoch++;
        this.updateKeys();
      };
      b.onpointerup = b.onpointercancel = b.onlostpointercapture = release;
    }
    this.el("leader-gripper-follow").onclick = () =>
      this.safe(() => this.setGripperFollow(!this.followGripper));
    this.el("refresh-leaders").onclick = () =>
      this.safe(() => this.refreshHosts());
    this.el("leader-host").onchange = () => this.renderDevices();
    this.el("leader-device").onchange = () => this.updateControls();
    window.addEventListener("keydown", (e) => this.keyDown(e));
    window.addEventListener("keyup", (e) => {
      if (this.keys.delete(e.code)) {
        this.epoch++;
        this.updateKeys();
      }
      if (e.code.startsWith("Shift")) {
        this.shift = false;
        this.epoch++;
        this.updateKeys();
      }
    });
    window.addEventListener("blur", () => this.clearKeys());
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) this.clearKeys();
    });
    document.addEventListener("focusin", (e) => {
      if (e.target.matches('input,select,textarea,[contenteditable="true"]'))
        this.clearKeys();
    });
    this.timer = setInterval(() => this.tick(), 33);
    this.poseTimer = setInterval(() => this.refreshPose(), 50);
    this.hostTimer = setInterval(() => {
      if (this.visible && this.mode === "leader")
        this.safe(() => this.refreshHosts());
      if (this.leaderRun) this.safe(() => this.heartbeat());
    }, 500);
  }
  async safe(fn) {
    try {
      return await fn();
    } catch (e) {
      this.onError(e);
    }
  }
  note(message) {
    this.el("manual-note").textContent = message;
  }
  stateForArm() {
    return this.snapshot?.arms?.[this.arm] || {};
  }
  measured() {
    const j = this.snapshot?.joints?.[this.arm],
      g = this.stateForArm().gripper_percent;
    if (!j?.name?.length || !finite(g)) return null;
    const positions = Object.fromEntries(
      j.name.map((n, i) => [n.split("/").pop(), j.position?.[i]]),
    );
    if (!Object.values(positions).every(finite)) return null;
    return { positions, gripper: g, gripper_unit: "percent" };
  }
  isOwnStream() {
    return (
      this.connected &&
      this.ownsStream &&
      this.client.currentOperation?.armId === this.arm &&
      this.client.currentOperation?.mode === "STREAM" &&
      this.client.currentOperation?.type === "DIRECT"
    );
  }
  setVisible(value) {
    this.visible = value;
    this.gamepad?.setActive(value && this.mode === "gamepad");
    if (!value) this.clearKeys();
    else {
      this.refreshPose();
      if (this.mode === "leader") this.safe(() => this.refreshHosts());
    }
  }
  update({ snapshot, resources, arm, connected }) {
    const wasConnected = this.connected;
    const armChanged = this.arm !== arm;
    const operation = this.client.currentOperation;
    const ownSocketOpen = this.client.connected === true && this.ownsStream &&
      operation?.armId === arm && operation.type === "DIRECT" && operation.mode === "STREAM" &&
      operation.transport === "websocket" && operation.socketOpen === true;
    this.snapshot = snapshot;
    // A delayed monitoring request must not retire a healthy manual producer.
    // Explicit client disconnect or the operation's socket-close event still does.
    this.connected = Boolean(connected || ownSocketOpen);
    this.resources = resources;
    if (armChanged) {
      this.cancelIntent();
      this.arm = arm;
      this.target = null;
      this.dirty = false;
      this.descriptor = null;
      this.fk.resetArm(arm);
      this.leaderRun = null;
      this.ownsStream = false;
      this.config = (resources.arms || []).find((a) => a.id === arm) || {};
      this.resetTargets();
      this.gamepad?.resetForArm();
      this.renderJacobianRank(null,true);
    }
    // Initial status can select the default arm before connect() announces its
    // connected state. Fetch capabilities when that connection becomes ready.
    if (arm && this.connected && (armChanged || !wasConnected))
      this.safe(() => this.loadDescription());
    if (!this.target && this.measured()) this.resetTargets();
    if (!this.connected) {
      this.renderJacobianRank(null,true);
      this.cancelIntent();
      this.ownsStream = false;
      this.leaderRun = null;
    }
    this.updateControls();
  }
  updateOperation(event) {
    if (
      ["result", "closed"].includes(event.event) ||
      [
        "done",
        "ended",
        "error",
        "idle",
        "result",
        "closed",
        "completed",
        "stopped",
        "disconnected",
        "interrupted",
      ].includes(event.state)
    ) {
      this.ownsStream = false;
      const expectedHold =
        this.expectedTermination &&
        event.event === "closed" &&
        (event.terminalReason ?? event.state) === "stopping";
      if (!expectedHold) this.cancelIntent();
      else this.clearKeys();
      this.gamepad?.operationEnded();
      this.note("Stream stopped. Targets remain local.");
    }
    this.updateControls();
  }
  async loadDescription() {
    const arm = this.arm;
    const d = await this.client.request(
      `/api/manual/${encodeURIComponent(arm)}/describe`,
    );
    if (arm !== this.arm) return;
    this.descriptor = d;
    this.updateControls();
    this.refreshPose();
  }
  updateControls() {
    const active =
      !!this.client.currentOperation ||
      !!this.leaderRun ||
      !!this.stateForArm().operation_id;
    const ready =
      this.connected &&
      !!this.arm &&
      !this.config?.read_only &&
      this.stateForArm().feedback_valid &&
      !!this.measured();
    this.el("start-manual").disabled =
      !ready ||
      active ||
      this.busy ||
      (["ik","gamepad"].includes(this.mode) && !this.descriptor?.available) ||
      (this.mode === "gamepad" && (!this.descriptor?.analog_rates || !this.gamepad?.canStart())) ||
      (this.mode === "leader" && !this.el("leader-device").value);
    this.el("start-manual").innerHTML = this.busy
      ? "Starting…"
      : this.mode === "leader"
        ? "Follow"
        : this.mode === "gamepad" ? "Start <kbd>A / ✕</kbd>" : "Start";
    this.el("stop-manual").innerHTML = this.mode === "gamepad" ? "Stop <kbd>B / ○</kbd>" : "Stop <kbd>Esc</kbd>";
    this.el("home-manual").innerHTML = this.mode === "gamepad" ? "Home <kbd>Y / △</kbd>" : "Home <kbd>N</kbd>";
    this.el("stop-manual").disabled = !this.connected || !this.arm;
    for (const name of ["home", "fold"]) {
      const available = !!this.descriptor?.presets?.[name];
      this.el(name + "-manual").disabled = !ready || this.busy || !available;
      this.el(name + "-manual").title = available
        ? ""
        : "No configured " + name + " pose";
    }
    this.el("partial-home").disabled = this.el("home-manual").disabled;
    this.el("manual-mode").disabled = this.busy;
    this.el("leader-host").disabled = this.busy || !!this.leaderRun;
    this.el("leader-device").disabled = this.busy || !!this.leaderRun;
    for (const b of this.element.querySelectorAll("[data-jog]"))
      b.disabled = this.busy || !this.isOwnStream();
    for (const b of this.element.querySelectorAll("[data-roll]"))
      b.disabled = this.busy || !ready || !this.descriptor?.available;
    this.gamepad?.updateControls();
  }
  resetTargets() {
    this.dirty = false;
    this.target = this.measured();
    this.reference = this.target ? copy(this.target) : null;
    this.lastSolveAt = null;
    this.renderSliders();
    this.updateControls();
  }
  renderSliders() {
    const box = this.el("manual-joints");
    box.replaceChildren();
    if (!this.target) {
      box.innerHTML =
        '<p class="detail-note">Waiting for complete measured feedback.</p>';
      return;
    }
    for (const j of [
      ...(this.config.joints || []).map((j) => ({
        ...j,
        min: j.min ?? j.min_rad,
        max: j.max ?? j.max_rad,
        value: this.target.positions[j.name],
        unit: "rad",
      })),
      {
        name: this.config.gripper?.name || "gripper",
        min: 0,
        max: 100,
        value: this.target.gripper,
        unit: "%",
      },
    ]) {
      const row = document.createElement("div");
      row.className = "manual-joint";
      const label = document.createElement("label");
      label.className = "joint-label";
      const name = document.createElement("span");
      name.textContent = j.name;
      const numeric = document.createElement("input");
      Object.assign(numeric, {
        type: "number",
        min: String(j.min),
        max: String(j.max),
        step: j.unit === "%" ? "0.5" : "0.005",
        value: j.value.toFixed(j.unit === "%" ? 1 : 3),
      });
      numeric.setAttribute("aria-label", j.name + " target " + j.unit);
      const slider = document.createElement("input");
      Object.assign(slider, {
        type: "range",
        min: numeric.min,
        max: numeric.max,
        step: numeric.step,
        value: String(j.value),
      });
      slider.setAttribute("aria-label", j.name + " slider");
      const edit = (source) => {
        const v = Number(source.value);
        if (!finite(v) || v < j.min || v > j.max) return;
        numeric.value = String(v);
        slider.value = String(v);
        this.dirty = true;
        if (j.unit === "%") this.target.gripper = v;
        else this.target.positions[j.name] = v;
        if (this.isOwnStream() && this.mode === "joint")
          this.client.setManualTarget(copy(this.target));
      };
      numeric.oninput = () => edit(numeric);
      slider.oninput = () => edit(slider);
      label.append(name, numeric);
      const ends = document.createElement("div");
      ends.className = "range-endpoints";
      ends.innerHTML = `<span>${j.min.toFixed(1)} ${j.unit}</span><span>${j.max.toFixed(1)} ${j.unit}</span>`;
      row.append(label, slider, ends);
      box.append(row);
    }
  }
  actionBusy(delta) {
    this.pendingActions = Math.max(0, this.pendingActions + delta);
    this.busy = this.pendingActions > 0;
    this.updateControls();
  }
  cancelIntent() {
    this.intentGeneration++;
    this.clearKeys();
    this.gamepad?.suspend();
  }
  intentContext() {
    const state = this.stateForArm();
    return {
      generation: this.intentGeneration,
      arm: this.arm,
      mode: this.mode,
      boot: state.runtime_boot_id,
      session: state.controller_session,
      principal: this.client.operatorSessionId,
    };
  }
  currentIntent(context) {
    const state = this.snapshot?.arms?.[context.arm] || {};
    return (
      this.connected &&
      context.generation === this.intentGeneration &&
      context.arm === this.arm &&
      context.mode === this.mode &&
      context.boot === state.runtime_boot_id &&
      context.session === state.controller_session &&
      context.principal === this.client.operatorSessionId
    );
  }
  async holdArm(arm) {
    this.expectedTermination++;
    try {
      return await this.client.hold(arm);
    } finally {
      this.expectedTermination--;
    }
  }
  async stopLeader(run) {
    run.stopping = true;
    const result = await this.client.request(
      `/api/input-hosts/${encodeURIComponent(run.host)}/stop`,
      {
        run_id: run.run_id,
        operator_session_id: this.client.operatorSessionId,
      },
    );
    if (!result.stopped || !result.hold_confirmed) {
      this.leaderRun = run;
      throw Error(result.reason || "Waiting for leader stop confirmation.");
    }
    return result;
  }
  async retireCanceled(context, run = null) {
    // A late mutating reply may have granted authority after Stop. Revoke it,
    // but never apply an old connection's cleanup to a new authenticated session.
    const state = this.snapshot?.arms?.[context.arm] || {};
    if (
      !this.connected ||
      context.principal !== this.client.operatorSessionId ||
      context.boot !== state.runtime_boot_id ||
      context.session !== state.controller_session
    )
      return;
    if (run) await this.stopLeader(run);
    else await this.holdArm(context.arm);
  }
  async changeMode(mode) {
    this.cancelIntent();
    const generation = this.intentGeneration,
      arm = this.arm;
    this.actionBusy(1);
    try {
      if (this.leaderRun) {
        const run = this.leaderRun;
        this.leaderRun = null;
        await this.stopLeader(run);
      } else if (this.isOwnStream()) await this.holdArm(arm);
      if (generation !== this.intentGeneration || arm !== this.arm) return;
      this.ownsStream = false;
      this.mode = mode;
      this.gamepad?.setActive(this.visible && mode === "gamepad");
      this.el("manual-mode").value = mode;
      for (const v of this.element.querySelectorAll("[data-manual-view]"))
        v.hidden = v.dataset.manualView !== mode;
      this.resetTargets();
      if (mode === "leader") await this.refreshHosts();
      if (generation !== this.intentGeneration || arm !== this.arm) return;
      this.note(
        mode === "ik"
          ? "Start, then hold a jog button or its keyboard key."
          : "Targets stay local until you start.",
      );
    } finally {
      this.actionBusy(-1);
    }
  }
  async start() {
    if (this.busy || !this.connected || !this.arm) return;
    if (this.mode === "gamepad") {
      if (!this.descriptor?.available || !this.descriptor?.analog_rates) throw Error(this.descriptor?.reason || "Gamepad kinematics are not ready.");
      if (!this.gamepad?.canStart()) throw Error(this.gamepad?.startReason() || "Controller unavailable.");
    }
    this.cancelIntent();
    const context = this.intentContext();
    this.actionBusy(1);
    try {
      if (!this.dirty || this.mode !== "joint") this.resetTargets();
      if (!this.target) throw Error("Complete measured pose is unavailable.");
      const staged = copy(this.target);
      if (!this.stateForArm().permission) {
        await this.client.enable(context.arm);
        if (!this.currentIntent(context)) {
          await this.retireCanceled(context);
          return;
        }
      }
      if (!this.currentIntent(context)) return;
      if (context.mode === "leader") {
        const host = this.el("leader-host").value,
          device = this.el("leader-device").value;
        const result = await this.client.request(
          `/api/input-hosts/${encodeURIComponent(host)}/start`,
          {
            device_id: device,
            arm_id: context.arm,
            operator_session_id: context.principal,
            runtime_boot_id: context.boot,
            controller_session: context.session,
            hz: 100,
            follow_gripper: this.followGripper,
            gripper_reference: this.preservedGripper(this.measured()),
          },
        );
        if (!result.accepted)
          throw Error(result.reason || "Leader start rejected");
        const run = { host, ...result };
        if (!this.currentIntent(context)) {
          await this.retireCanceled(context, run);
          return;
        }
        this.leaderRun = run;
        this.note("Leader starting · waiting for fresh input.");
      } else {
        await this.client.startManual(context.arm);
        if (!this.currentIntent(context)) {
          await this.retireCanceled(context);
          return;
        }
        this.ownsStream = true;
        this.client.setManualTarget(staged);
        this.target = copy(staged);
        this.reference = copy(staged);
        this.lastSolveAt = null;
        if (context.mode === "gamepad") this.gamepad?.operationStarted();
        this.note(
          context.mode === "ik"
            ? "IK active · hold a key or jog button to move."
            : context.mode === "gamepad" ? "Gamepad active · stick deflection sets motion speed." : "Joint stream active · target edits are sent immediately.",
        );
      }
    } catch (error) {
      if (!this.currentIntent(context)) {
        await this.retireCanceled(context);
        return;
      }
      throw error;
    } finally {
      this.actionBusy(-1);
    }
  }
  async stop() {
    this.cancelIntent();
    const arm = this.arm,
      run = this.leaderRun;
    this.leaderRun = null;
    this.ownsStream = false;
    this.actionBusy(1);
    try {
      if (run) await this.stopLeader(run);
      else if (arm && this.connected) await this.holdArm(arm);
      this.note("Stopped. Support torque is preserved.");
    } finally {
      this.actionBusy(-1);
    }
  }
  preservedGripper(measured) {
    const record = this.snapshot?.motor_status?.[this.arm];
    const row = record?.motors?.find(
      (m) =>
        m.role === "gripper" ||
        m.name === (this.config?.gripper?.name || "gripper"),
    );
    if (
      record?.state !== "stale" &&
      Number.isFinite(row?.reference_position) &&
      row.position_age_ms <= 500
    )
      return row.reference_position;
    if (this.isOwnStream() && Number.isFinite(this.target?.gripper))
      return this.target.gripper;
    return measured.gripper;
  }
  async preset(name, preserve = false) {
    if (this.busy || !this.connected || !this.arm) return;
    this.cancelIntent();
    const context = this.intentContext(),
      measured = this.measured();
    if (!measured) throw Error("Measured pose is unavailable.");
    this.actionBusy(1);
    try {
      const result = await this.client.request(
        `/api/manual/${encodeURIComponent(context.arm)}/preset`,
        {
          ...measured,
          name,
          preserve_wrist: preserve,
          reference_gripper: this.preservedGripper(measured),
        },
      );
      if (!this.currentIntent(context)) return;
      await this.sendIntent(
        result.target,
        `${name === "home" ? "Home" : "Fold"} requested${preserve ? " · wrist preserved" : ""}.`,
        context,
      );
    } finally {
      this.actionBusy(-1);
    }
  }
  async roll(name) {
    if (this.busy || !this.connected || !this.arm) return;
    this.cancelIntent();
    const context = this.intentContext(),
      measured = this.measured();
    if (!measured) throw Error("Measured pose is unavailable.");
    this.actionBusy(1);
    try {
      const result = await this.client.request(
        `/api/manual/${encodeURIComponent(context.arm)}/roll`,
        {
          ...measured,
          name,
          reference_gripper: this.preservedGripper(measured),
        },
      );
      if (!this.currentIntent(context)) return;
      await this.sendIntent(
        result.target,
        "Wrist reference requested.",
        context,
      );
    } finally {
      this.actionBusy(-1);
    }
  }
  async sendIntent(target, message, context) {
    if (!this.currentIntent(context)) return;
    if (this.leaderRun) {
      const run = this.leaderRun;
      this.leaderRun = null;
      await this.stopLeader(run);
      if (!this.currentIntent(context)) return;
    }
    if (!this.isOwnStream()) {
      if (this.client.currentOperation) {
        if (this.client.currentOperation.armId !== context.arm)
          throw Error("Another arm owns the current operation.");
        await this.holdArm(context.arm);
        if (!this.currentIntent(context)) return;
      }
      if (!this.stateForArm().permission) {
        try {
          await this.client.enable(context.arm);
        } catch (error) {
          if (!this.currentIntent(context)) await this.retireCanceled(context);
          throw error;
        }
        if (!this.currentIntent(context)) {
          await this.retireCanceled(context);
          return;
        }
      }
      try {
        await this.client.startManual(context.arm);
      } catch (error) {
        if (!this.currentIntent(context)) await this.retireCanceled(context);
        throw error;
      }
      if (!this.currentIntent(context)) {
        await this.retireCanceled(context);
        return;
      }
      this.ownsStream = true;
    }
    if (!this.currentIntent(context)) return;
    this.client.setManualTarget(copy(target));
    this.target = copy(target);
    this.reference = copy(target);
    this.lastSolveAt = null;
    this.dirty = true;
    this.renderSliders();
    if (this.mode === "gamepad") this.gamepad?.suspend();
    this.note(message);
    this.updateControls();
  }
  clearKeys() {
    this.keys.clear();
    this.shift = false;
    this.epoch++;
    this.lastSolveAt = null;
    this.updateKeys();
  }
  updateKeys() {
    for (const b of this.element.querySelectorAll("[data-jog]"))
      b.classList.toggle("active", this.keys.has(b.dataset.jog));
    this.el("ik-speed-label").textContent = this.shift
      ? "Maximum · Shift"
      : ["", "Fine", "Normal", "Coarse", "Fast", "Maximum"][this.level];
  }
  setSpeed(level) {
    if (this.level !== level) {
      this.epoch++;
      if (this.gamepad) this.gamepad.lastSolveAt = null;
    }
    this.level = level;
    for (const b of this.element.querySelectorAll("[data-speed]"))
      b.classList.toggle("active", Number(b.dataset.speed) === level);
    this.updateKeys();
  }
  keyDown(e) {
    if (!this.visible || e.ctrlKey || e.metaKey || e.altKey || e.isComposing)
      return;
    if (e.code === "Escape") {
      e.preventDefault();
      this.safe(() => this.stop());
      return;
    }
    if (
      e.target.closest(
        'input:not([type="range"]),textarea,select,[contenteditable="true"],.fk-stage',
      )
    )
      return;
    if (e.code === "KeyN" || e.code === "KeyM") {
      if (e.repeat) return;
      e.preventDefault();
      this.safe(() => this.preset(e.code === "KeyN" ? "home" : "fold"));
      return;
    }
    if (this.mode === "leader" && e.code === "KeyO") {
      if (!e.repeat) {
        e.preventDefault();
        this.safe(() => this.setGripperFollow(!this.followGripper));
      }
      return;
    }
    if (this.mode !== "ik") return;
    if (e.code.startsWith("Shift")) {
      if (!this.shift) this.epoch++;
      this.shift = true;
      this.updateKeys();
      return;
    }
    if (/^Digit[1-5]$/.test(e.code)) {
      e.preventDefault();
      this.setSpeed(Number(e.code.slice(-1)));
      return;
    }
    if (MOVE_KEYS.has(e.code)) {
      e.preventDefault();
      if (!this.busy && this.isOwnStream()) {
        if (!this.keys.has(e.code)) this.epoch++;
        this.keys.add(e.code);
        this.updateKeys();
      }
      return;
    }
    if (e.repeat) return;
    if (e.code === "Tab") {
      e.preventDefault();
      this.el("ik-position-only").click();
    } else if (e.code === "Space") {
      e.preventDefault();
      this.safe(() => this.preset("home", true));
    } else if (["KeyR", "KeyT", "KeyE"].includes(e.code)) {
      e.preventDefault();
      this.safe(() =>
        this.roll(
          { KeyR: "minus_90", KeyT: "reference", KeyE: "plus_90" }[e.code],
        ),
      );
    }
  }
  async tick() {
    if (
      !this.visible ||
      document.hidden ||
      this.busy ||
      this.mode !== "ik" ||
      !this.isOwnStream() ||
      !this.client.currentOperation?.ready ||
      !this.keys.size ||
      this.solving ||
      !this.target
    )
      return;
    const measured = this.measured();
    if (!measured || !this.stateForArm().feedback_valid) return;
    this.solving = true;
    const epoch = this.epoch,
      context = this.intentContext();
    const started = performance.now();
    const dt =
      this.lastSolveAt === null
        ? 1 / 30
        : Math.min(0.1, Math.max(0, (started - this.lastSolveAt) / 1000));
    this.lastSolveAt = started;
    const reference = this.reference || this.target;
    try {
      const result = await this.client.request(
        `/api/manual/${encodeURIComponent(context.arm)}/step`,
        {
          ...measured,
          keys: [...this.keys],
          dt_s: dt,
          level: this.level,
          shift: !!this.shift,
          position_only: !!this.positionOnly,
          reference_positions: reference.positions,
          reference_gripper: reference.gripper,
        },
      );
      if (
        epoch !== this.epoch ||
        !this.currentIntent(context) ||
        !this.isOwnStream() ||
        !this.visible ||
        document.hidden
      )
        return;
      if (result.target) {
        this.client.setManualTarget(copy(result.target));
        this.target = copy(result.target);
        this.reference = copy(result.target);
      }
      this.note(
        result.status === "BLOCKED"
          ? "Target at workspace boundary."
          : `IK active · ${this.el("ik-speed-label").textContent.toLowerCase()}`,
      );
    } catch (e) {
      if (epoch === this.epoch && this.currentIntent(context)) {
        this.clearKeys();
        this.onError(e);
      }
    } finally {
      this.solving = false;
    }
  }
  async refreshPose() {
    if (!this.visible || !this.connected || !this.arm || this.poseLoading)
      return;
    const measured = this.measured();
    if (!measured) { this.renderJacobianRank(null,true); return; }
    this.poseLoading = true;
    const arm = this.arm;
    try {
      const host = this.hosts.find(
        (h) => h.id === this.el("leader-host").value,
      );
      const overlay =
        this.mode === "leader"
          ? host?.report_age_ms < 500 && host?.active
            ? host.latest_pose
            : null
          : this.target;
      // Jaw visualization has calibrated end stops; retain the unmodified
      // observation in host.latest_pose while drawing its bounded opening.
      const visualOverlay=this.mode==='leader' && overlay && Number.isFinite(overlay.gripper)
        ? {...overlay,gripper:Math.max(0,Math.min(100,overlay.gripper))}:overlay;
      const value = await this.client.request(
        `/api/manual/${encodeURIComponent(arm)}/fk`,
        { ...measured, target: visualOverlay },
      );
      if (arm !== this.arm || !this.connected) return;
      const g = value.measured;
      this.renderJacobianRank(g.jacobian, !this.stateForArm().feedback_valid);
      const readouts = {
        X: `${(g.position_m[0] * 1000).toFixed(0)} mm`,
        Y: `${(g.position_m[1] * 1000).toFixed(0)} mm`,
        Z: `${(g.position_m[2] * 1000).toFixed(0)} mm`,
        Elevation: `${((g.elevation_rad * 180) / Math.PI).toFixed(1)}°`,
        "Wrist roll": `${((g.tool_roll_rad * 180) / Math.PI).toFixed(1)}°`,
        Gripper: `${g.gripper_percent.toFixed(0)}%`,
      };
      for (const [name, value] of Object.entries(readouts))
        this.element.querySelector(`[data-axis="${name}"]`).textContent = value;
      const record = this.snapshot.motor_status?.[arm];
      this.fk.update(
        value.measured,
        value.target,
        record?.motors || [],
        !this.stateForArm().feedback_valid || record?.state === "stale",
        this.mode === "leader" ? "Leader" : "Target",
      );
    } catch (e) {
      this.renderJacobianRank(null);
      this.el("manual-fk").querySelector(".fk-empty").textContent =
        "Kinematic model unavailable";
    } finally {
      this.poseLoading = false;
    }
  }
  renderJacobianRank(value, stale = false) {
    const display = jacobianRankPresentation(value,stale);
    this.el('ik-mobility-rank').textContent = display.rank;
    this.el('ik-mobility-detail').textContent = display.detail;
    this.el('ik-mobility')?.style?.setProperty('--mobility-color',display.color);
  }
  async refreshHosts() {
    if (this.loadingHosts || !this.connected) return;
    this.loadingHosts = true;
    const observedRun = this.leaderRun;
    try {
      const data = await this.client.request("/api/input-hosts");
      if (!this.connected) return;
      this.hosts = data.hosts || [];
      const select = this.el("leader-host"),
        old = select.value,
        signature = JSON.stringify(
          this.hosts.map((h) => [h.id, h.label, h.state]),
        );
      if (select.dataset.signature !== signature) {
        select.replaceChildren(
          new Option(
            this.hosts.length
              ? "Select input host"
              : "No input hosts connected",
            "",
          ),
          ...this.hosts.map((h) => new Option(h.label || h.id, h.id)),
        );
        select.value = this.hosts.some((h) => h.id === old) ? old : "";
        select.dataset.signature = signature;
      }
      this.renderDevices();
      if (observedRun && this.leaderRun === observedRun) {
        const h = this.hosts.find((h) => h.id === observedRun.host);
        const state = h?.active?.state || h?.state;
        this.el("leader-status").textContent = [state || "Waiting for host",h?.reason].filter(Boolean).join(' · ');
        if (state === "STOPPING") observedRun.stopping = true;
        if (
          !h ||
          !h.active ||
          h.active.run_id !== observedRun.run_id ||
          ["ERROR", "STOPPED", "OFFLINE", "IDLE"].includes(state)
        ) {
          this.leaderRun = null;
          this.note(h?.reason || 'Leader run ended.');
          this.updateControls();
        }
      }
    } finally {
      this.loadingHosts = false;
    }
  }
  renderDevices() {
    const host = this.hosts.find((h) => h.id === this.el("leader-host").value),
      select = this.el("leader-device"),
      old = select.value;
    select.replaceChildren(
      new Option(host ? "Select leader device" : "Select an input host", ""),
      ...(host?.devices || []).map((d) => {
        const o = new Option(d.label || d.id, d.id);
        o.disabled = !d.configured;
        o.title = d.reason || "";
        return o;
      }),
    );
    select.value = (host?.devices || []).some(
      (d) => d.id === old && d.configured,
    )
      ? old
      : "";
    this.updateControls();
  }
  async setGripperFollow(value) {
    const run = this.leaderRun;
    if (run && !run.stopping) {
      const result = await this.client.request(
        `/api/input-hosts/${encodeURIComponent(run.host)}/settings`,
        {
          run_id: run.run_id,
          operator_session_id: this.client.operatorSessionId,
          follow_gripper: value,
        },
      );
      if (this.leaderRun !== run || !result.accepted) return;
    }
    this.followGripper = value;
    this.el("leader-gripper-follow").setAttribute(
      "aria-pressed",
      String(value),
    );
  }
  async heartbeat() {
    const run = this.leaderRun;
    if (!run || run.stopping) return;
    try {
      const result = await this.client.request(
        `/api/input-hosts/${encodeURIComponent(run.host)}/heartbeat`,
        {
          run_id: run.run_id,
          operator_session_id: this.client.operatorSessionId,
        },
      );
      if(this.leaderRun===run && result?.accepted===false) {
        this.leaderRun=null;
        this.note(result.reason || 'Leader run ended.');
        this.updateControls();
      }
    } catch (e) {
      if (this.leaderRun === run) {
        this.note("Leader status temporarily unavailable; use Stop to end following.");
      }
    }
  }
}
