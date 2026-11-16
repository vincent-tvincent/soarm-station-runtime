import {
  openDraft,
  describeBlocks,
  setValue,
  addStep,
  removeStep,
  moveStep,
  getJson,
} from "./payload-model.js";
import { StationClient } from "./transport.js";
import { GraphExplorer } from "./graph.js";
import { ManualPanel } from "./manual-panel.js";
import { PolicyManagement } from "./policy-management.js";
import { fitRect, packEqualCameras } from "./camera-layout.js";
import { JointIssueCache, motorIssuePresentation, operationHealthSummary } from "./fk-view.js";
const $ = (id) => document.getElementById(id);
const text = (id, value) => {
  $(id).textContent = value ?? "—";
};
const number = (v, d = 1) =>
  Number.isFinite(Number(v)) && v !== null && v !== ""
    ? Number(v).toFixed(d)
    : "—";
const esc = (s) =>
  String(s ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
let snapshot = {},
  resources = {},
  selectedArm = "",
  selectedMotor = "",
  activeTab = "status",
  cameraLayout = "equal",
  primaryCamera = "",
  cameraIds = [],
  cameraFrames = new Map(),
  pipPositions = new Map(),
  manual,
  noticeTimer,
  graph,
  policies,
  connected = false,
  motorStatusDelayed = false,
  torquePending = false,
  operation = null,
  historyRows = [];
const motorIssueCache = new JointIssueCache();
let payloadView = "blocks",
  payloadDraft = null,
  activePayloadRow = null,
  payloadRepresentations = {};
const client = new StationClient({
  onState: renderState,
  onConnection: connection,
  onOperation: operationChanged,
  onError: showError,
});
window.addEventListener("pagehide", () => client.disconnect());
function notice(message, error = false) {
  text("notice", message);
  $("notice").hidden = false;
  $("notice").classList.toggle("error", error);
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => ($("notice").hidden = true), 7000);
}
function showError(error) {
  notice(error?.message || String(error), true);
}
async function safely(fn) {
  try {
    return await fn();
  } catch (e) {
    showError(e);
  }
}
function connection(info) {
  const phase = typeof info === "string" ? info : info.state;
  motorStatusDelayed = phase === 'degraded' || phase === 'disconnected' || phase === 'connecting';
  // Status-poll health is independent of the established connection/control WS.
  connected = typeof info?.connected === "boolean"
    ? info.connected
    : phase === "connected" || (phase === "degraded" && client.connected);
  text(
    "connection",
    connected
      ? phase === "degraded" ? "Connected · status delayed" : "Connected"
      : info.reason || phase || "Disconnected",
  );
  // A transient motor/status poll error must not retire read-only graph watches.
  // The graph endpoint checks its own freshness; explicit disconnect still pauses it.
  graph?.setConnected(connected || phase === "degraded");
  policies?.setConnected(connected);
  $("disconnect").hidden = false;
  $("disconnect").disabled = phase === "connecting";
  $("disconnect").textContent = connected ? "Disconnect" : "Reconnect";
  if (phase === "disconnected") {
    cameraIds = [];
    cameraFrames.clear();
    arrangeCameras();
    operation = null;
  }
  renderMotors();
  updateControls();
}
function armConfig() {
  const list = resources.arms || [];
  return (
    (Array.isArray(list) ? list : Object.values(list)).find(
      (a) => a.id === selectedArm,
    ) || {}
  );
}
function armState() {
  return snapshot.arms?.[selectedArm] || {};
}
function joints() {
  const j = snapshot.joints?.[selectedArm] || {};
  return Object.fromEntries(
    (j.name || []).map((name, i) => [name.split("/").pop(), j.position?.[i]]),
  );
}
function profileList() {
  const p = resources.profiles || [];
  return Array.isArray(p)
    ? p
    : Object.entries(p).map(([id, value]) => ({ id, ...value }));
}
function updateControls() {
  const a = armState(),
    busy =
      a.operation_id ||
      (operation &&
        !["done", "ended", "error", "idle", "result", "closed"].includes(
          operation.state,
        ));
  $("enable-control").disabled =
    !connected ||
    !selectedArm ||
    armConfig().read_only ||
    a.permission ||
    !!busy;
  $("hold").disabled = !connected || !selectedArm;
  const torqueKnown = ['ON','OFF','MIXED'].includes(a.measured_torque);
  const torqueOff = a.measured_torque === 'OFF';
  $('toggle-torque').textContent = torquePending ? 'Applying torque…' : torqueKnown ? torqueOff ? 'Enable torque' : 'Disable torque' : 'Torque unavailable';
  $('toggle-torque').disabled = !connected || !selectedArm || armConfig().read_only || torquePending || !torqueKnown;
  $('toggle-torque').title = torqueOff ? 'Hold the current pose with motor torque; control remains a separate action.' : 'Removes motor holding force. Support the arm before disabling torque.';
  $("run-payload").disabled =
    !connected ||
    !selectedArm ||
    !a.permission ||
    !!busy ||
    ($("operation-type").value === "MODEL" && !$("profile-select").value);
  $("arm-select").disabled = !!busy || torquePending;
  manual?.updateControls();
  $("send-update").disabled =
    !busy ||
    operation?.type !== $("operation-type").value ||
    $("operation-mode").value !== "STREAM";
  $("send-update").title = $("send-update").disabled
    ? "Choose the active stream type and STREAM delivery to update it."
    : "";
  $("end-stream").disabled = !busy;
  if (payloadDraft?.valid === false) {
    $("run-payload").disabled = true;
    $("send-update").disabled = true;
  }
}
let connectionAttempt = null;
async function connectStation() {
  if (connectionAttempt) return connectionAttempt;
  connectionAttempt = (async () => {
    try {
      await client.connect();
      resources =
        snapshot.resources || (await client.request("/api/resources"));
      renderState(snapshot);
      await graph.refresh();
    } catch (error) {
      text("connection", "Connection unavailable");
      showError(error);
    }
  })();
  try {
    await connectionAttempt;
  } finally {
    connectionAttempt = null;
  }
}
$("disconnect").onclick = () =>
  connected ? safely(() => client.disconnect()) : connectStation();
let currentPage = "operations", changingPage = false;
async function setPage(value, { fromHash = false, initial = false } = {}) {
  const page = ["operations", "graph", "policies"].includes(value) ? value : "operations";
  if (changingPage) {
    if (fromHash) history.replaceState(null, "", "#" + currentPage);
    return false;
  }
  changingPage = true;
  try {
  if (!initial && currentPage === "policies" && page !== currentPage &&
      !(await policies.beforeLeave())) {
    if (fromHash) history.replaceState(null, "", "#" + currentPage);
    return false;
  }
  $("app").dataset.view = page;
  document
    .querySelectorAll("[data-page]")
    .forEach((b) => b.classList.toggle("active", b.dataset.page === page));
  $("operations").hidden = page !== "operations";
  $("graph").hidden = page !== "graph";
  $("policies").hidden = page !== "policies";
  text("page-title", page === "graph" ? "ROS graph" : page === "policies" ? "Policy management" : "Robot operations");
  text("page-series", page === "graph" ? "OBSERVABILITY" : page === "policies" ? "POLICY MANAGEMENT" : "CONTROL");
  manual?.setVisible(page === "operations" && activeTab === "manual", page === "operations");
  graph?.setVisible(page === "graph");
  policies?.setVisible(page === "policies");
  if (page === "graph" && connected) graph.refresh();
  currentPage = page;
  if (!fromHash && location.hash !== "#" + page) history.pushState(null, "", "#" + page);
  return true;
  } finally { changingPage = false; }
}
for (const item of document.querySelectorAll("[data-page]"))
  item.onclick = () => {
    void safely(() => setPage(item.dataset.page));
  };
window.addEventListener("hashchange", () => void safely(() => setPage(location.hash.slice(1), { fromHash: true })));
for (const tab of document.querySelectorAll("[data-tab]"))
  tab.onclick = () => {
    activeTab = tab.dataset.tab;
    document.querySelectorAll("[data-tab]").forEach((b) => {
      b.classList.toggle("active", b === tab);
      b.setAttribute("aria-selected", String(b === tab));
    });
    for (const n of ["status", "manual", "payload"])
      $("panel-" + n).hidden = n !== activeTab;
    $("operator-control-actions").hidden = activeTab !== "manual";
    manual?.setVisible(
      activeTab === "manual" && $("app").dataset.view === "operations",
      $("app").dataset.view === "operations",
    );
  };
$("arm-select").onchange = () => {
  selectedArm = $("arm-select").value;
  selectedMotor = "";

  loadExample();
  renderState(snapshot);
};
$("enable-control").onclick = () =>
  safely(async () => {
    const result = await client.enable(selectedArm);
    if (result?.accepted === false)
      throw Error(result.reason || "Permission was not granted.");
    notice("Control permission requested.");
  });
$("hold").onclick = () =>
  safely(async () => {
    if (manual) await manual.stop();
    else await client.hold(selectedArm);
    notice("Hold requested. Support torque is preserved.");
  });
$('toggle-torque').onclick = () => safely(async()=>{
  if(torquePending)return;
  const arm=selectedArm,enabled=armState().measured_torque==='OFF';
  torquePending=true;updateControls();
  try{const result=await client.setTorque(arm,enabled);notice(result.message||`Torque ${enabled?'enabled':'disabled'}.`);}
  finally{torquePending=false;updateControls();}
});
function renderState(state) {
  snapshot = state || {};
  motorStatusDelayed = false;
  for(const [armId,record] of Object.entries(snapshot.motor_status || {}))
    motorIssueCache.update(armId,snapshot.arms?.[armId]?.controller_session,namedMotors(record.motors || []),
      {stale:motorStatusIsStale(record)});
  policies?.updateState(snapshot);
  resources = state.resources || resources;
  graph?.setConnected(true);
  const env = state.environment || state.preview?.label || "";
  text("environment", typeof env === "string" ? env : env.label || "");
  const arms = Array.from(
    new Set([
      ...(Array.isArray(resources.arms)
        ? resources.arms.map((a) => a.id)
        : Object.keys(resources.arms || {})),
      ...Object.keys(state.arms || {}),
    ]),
  );
  const activeArm =
    client.currentOperation?.armId || (manual?.leaderRun ? manual.arm : "");
  // Keep Stop attached to the current operation if resource metadata changes.
  if (activeArm && !arms.includes(activeArm)) arms.push(activeArm);
  const current = $("arm-select").value;
  if (JSON.stringify(arms) !== $("arm-select").dataset.ids) {
    $("arm-select").replaceChildren(
      ...(arms.length
        ? arms.map((id) => new Option(id, id))
        : [new Option("No arms available", "")]),
    );
    $("arm-select").dataset.ids = JSON.stringify(arms);
  }
  const next = activeArm || (arms.includes(current) ? current : (arms[0] || ""));
  $("arm-select").value = next;
  if (selectedArm !== next) {
    selectedArm = next;
    selectedMotor = "";
    loadExample();
  }
  const a = armState();
  text("torque-badge", "Torque " + (a.measured_torque || "—").toLowerCase());
  text(
    "permission-badge",
    a.permission ? "Control enabled" : "Control disabled",
  );
  $("permission-badge").classList.toggle("granted", !!a.permission);
  text("feedback-hz", number(a.measured_hz));
  text("feedback-age", number(a.feedback_age_ms, 0));
  text("status-phase", a.fault || a.phase || "—");
  const inf = state.inference?.[selectedArm];
  text("policy-state", inf?.status?.[0]?.message || "No policy running");
  renderMotors();
  manual?.update({ snapshot, resources, arm: selectedArm, connected });
  const ids = (resources.cameras || state.cameras || []).map((c) =>
    typeof c === "string" ? c : c.id,
  );
  updateCameras(ids);
  const allowedProfiles = armConfig().profile_ids;
  const profiles = profileList().filter((p) =>
    Array.isArray(allowedProfiles)
      ? allowedProfiles.includes(p.id)
      : p.arm_id === selectedArm,
  );
  if (
    JSON.stringify(profiles.map((p) => p.id)) !==
    $("profile-select").dataset.ids
  ) {
    const old = $("profile-select").value;
    $("profile-select").replaceChildren(
      new Option(
        profiles.length ? "Select a profile" : "No registered profiles",
        "",
      ),
      ...profiles.map((p) => new Option(p.label || p.id, p.id)),
    );
    $("profile-select").value = profiles.some((p) => p.id === old) ? old : "";
    $("profile-select").dataset.ids = JSON.stringify(profiles.map((p) => p.id));
  }
  const events = [...(state.events || []), ...(state.decisions || [])].slice(
    -30,
  );
  const recent = events.at(-1);
  text(
    "recent-event",
    recent ? eventLabel(recent) : state.upload?.error || "No events received",
  );
  $("event-log").replaceChildren(
    ...events.reverse().map((event) => {
      const el = document.createElement("div");
      el.textContent = eventLabel(event);
      return el;
    }),
  );
  if (!operation) {
    text(
      "active-operation",
      a.operation_id
        ? `${a.operation_type || "Operation"} · ${a.phase}`
        : "No active operation",
    );
    text("operation-detail", a.fault || "Permission is separate from torque.");
  }
  updateControls();
}
function eventLabel(e) {
  return [
    e.occurred_at ||
      e.timestamp ||
      (e.time?.sec ? new Date(e.time.sec * 1000).toLocaleTimeString() : ""),
    e.code || e.decision_code || "",
    e.detail ||
      e.summary ||
      e.reason ||
      e.message ||
      e.event_type ||
      e.kind ||
      e.decision ||
      "Event",
  ]
    .filter(Boolean)
    .join(" · ");
}
function motorList() {
  const source =
    snapshot.motor_status?.[selectedArm]?.motors ||
    snapshot.motors?.[selectedArm] ||
    snapshot.health?.[selectedArm]?.motors ||
    armState().motors ||
    [];
  return namedMotors(source);
}
function namedMotors(source) {
  const rows=Array.isArray(source)?source:Object.entries(source || {}).map(([name,m])=>({name,...m}));
  return rows.map((motor,i)=>({...motor,name:motor.name || motor.joint_name || motor.motor_name || String(motor.id ?? i)}));
}
function motorStatusIsStale(record) {
  return !connected || motorStatusDelayed || Boolean(record?.error) || ['stale','unavailable'].includes(record?.state);
}
function renderMotors() {
  let motors = motorList();
  const pos = joints();
  if (!motors.length && selectedArm) {
    motors = Object.entries(pos).map(([name, position]) => ({
      name,
      position,
      position_unit: "rad",
    }));
    if (armConfig().gripper)
      motors.push({
        name: armConfig().gripper.name || "gripper",
        position: armState().gripper_percent,
        position_unit: "percent",
      });
  }
  motors=namedMotors(motors);
  const motorRecord = snapshot.motor_status?.[selectedArm];
  const statusStale=motorStatusIsStale(motorRecord);
  const issues=motorIssueCache.update(selectedArm,armState().controller_session,motors,{stale:statusStale});
  const presentations=new Map(motors.map(m=>[m.name,motorIssuePresentation(m,issues.states,{stale:statusStale})]));
  const summary=operationHealthSummary({arm:armState(),motors,issues:issues.states,stale:statusStale,
    connected,recordError:motorRecord?.error,inference:snapshot.inference?.[selectedArm]?.status||[]});
  for(const [name,metric] of [['temperature',summary.temperature],['voltage',summary.voltage]]){
    text('summary-'+name,number(metric.value,name==='voltage'?2:1));
    $('summary-'+name).title=metric.count?`Average of ${metric.count} / ${metric.total} motor readings`:'No fresh motor readings';
  }
  text('summary-errors',summary.errors.value);$('summary-errors').title=summary.errors.detail;
  $('summary-errors').classList.toggle('has-issue',summary.errors.active);
  text('summary-torque',summary.torque.value);$('summary-torque').title=summary.torque.detail;
  if(!connected){text('summary-conditioning','—');$('summary-conditioning').title='Disconnected';}
  text(
    "motor-source",
    !connected || motorStatusDelayed
      ? motorRecord ? "Status delayed · last received sample" : "Waiting for telemetry"
      : motorRecord?.error
      ? "Telemetry error · last valid sample"
      : motorRecord
        ? `${motorRecord.state} · health ${number(motorRecord.health_age_ms, 0)} ms old`
        : motors.length
          ? "Health not reported"
          : "Waiting for telemetry",
  );
  $("motor-source").title = motorRecord?.error || "";
  const hasCurrent = motors.some((m) => Number.isFinite(m.current_a));
  $("current-heading").hidden = !hasCurrent;
  $("motor-rows").innerHTML = motors.length
    ? motors
        .map((m, i) => {
          const name =
            m.name || m.joint_name || m.motor_name || String(m.id ?? i);
          const unit =
            m.position_unit ||
            m.unit ||
            (name === "gripper" ? "percent" : "rad");
          const value = Object.hasOwn(m, "position")
            ? m.position
            : (m.position_rad ?? pos[name]);
          const torque = m.torque_enabled ?? m.torque;
          const label =
            torque === true || torque === 1 || torque === "ON"
              ? "On"
              : torque === false || torque === 0 || torque === "OFF"
                ? "Off"
                : "—";
          const issue=presentations.get(name);
          return `<tr tabindex="0" data-motor="${esc(name)}" class="${name === selectedMotor ? "selected" : ""}"><td><span class="motor-name" title="${esc(name)}"><span class="motor-id">${esc(m.id ?? m.motor_id ?? "—")}</span>${esc(name)}</span><span class="motor-issue${issue.active?' is-active':''}" data-issue-state="${esc(issue.state)}" title="${esc(issue.detail)}">${esc(issue.label)}</span></td><td>${number(value, unit === "rad" ? 3 : 1)}${unit === "rad" ? "" : unit === "percent" ? "%" : ""}</td><td>${label}</td><td>${number(m.temperature_c, 0)}</td><td>${number(m.voltage_v)}</td>${hasCurrent ? `<td>${number(m.current_a, 3)}</td>` : ""}</tr>`;
        })
        .join("")
    : '<tr><td colspan="5" class="muted">Select an arm to inspect its motors.</td></tr>';
  for (const row of $("motor-rows").querySelectorAll("[data-motor]")) {
    const select = () => {
      selectedMotor = row.dataset.motor;
      renderMotors();
    };
    row.onclick = select;
    row.onkeydown = (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        select();
      }
    };
  }
  const m = motors.find(
    (m) => (m.name || m.joint_name || m.motor_name) === selectedMotor,
  );
  if (m) {
    const unit = m.position_unit || m.unit || "rad",
      goal = m.goal_position ?? m.goal_position_rad ?? m.goal ?? m.target;
    text(
      "motor-detail",
      `${selectedMotor} · target ${number(goal, 3)} ${unit} · ${presentations.get(m.name).detail}`,
    );
  } else
    text(
      "motor-detail",
      "Positions in radians; gripper in %. Select a motor for its target and reported motion issue.",
    );
}
function operationChanged(event) {
  manual?.updateOperation(event);
  policies?.updateOperation(event);
  operation = event;
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
    operation = null;
    $("stream-actions").hidden = true;
  } else $("stream-actions").hidden = event.mode !== "STREAM";
  text(
    "active-operation",
    event.event === "result" ||
      event.state === "done" ||
      event.state === "result"
      ? `Operation ${event.result?.result_code || "completed"}`
      : `${event.type || event.operation_type || "Operation"} · ${event.state || event.phase || "active"}`,
  );
  text(
    "operation-detail",
    event.reason ||
      event.detail ||
      event.result?.reason ||
      event.armId ||
      event.arm_id ||
      "",
  );
  updateControls();
}
function loadExample() {
  const type = $("operation-type").value,
    mode = $("operation-mode").value,
    p = joints(),
    g = armState().gripper_percent ?? null;
  let value;
  if (type === "MODEL")
    value =
      mode === "INLINE"
        ? { model_steps: [{ instruction: "", duration_s: 30 }] }
        : { instruction: "" };
  else if (mode === "STREAM" && type === "DIRECT")
    value = { positions: p, gripper: g, gripper_unit: "percent" };
  else
    value = {
      points: [
        {
          time_s: 1,
          positions: p,
          ...(type === "DIRECT" ? { gripper: g } : {}),
        },
      ],
    };
  $("payload-editor").value = JSON.stringify(value, null, 2);
  payloadRepresentations = {};
  refreshPayloadDraft(true);
  text("validation-message", "");
  updatePayloadHelp();
}
function updatePayloadHelp() {
  const type = $("operation-type").value,
    mode = $("operation-mode").value;
  $("profile-field").hidden = type !== "MODEL";
  text(
    "load-example",
    type === "MODEL" ? "New model task" : "Use current pose",
  );
  text(
    "run-payload",
    mode === "STREAM"
      ? "Start " + type.toLowerCase() + " stream"
      : "Run " + type.toLowerCase(),
  );
  text(
    "payload-help",
    type === "MODEL"
      ? "Choose a registered deployment profile. Instructions depend on its policy capabilities."
      : type === "TRAJECTORY" && payloadDraft?.value?.cartesian
        ? "Cartesian positions use metres in the selected frame. Each endpoint has its own duration."
        : mode === "STREAM"
          ? "Start once, then send updated targets. End stream revokes control permission."
          : "Named joint positions use radians; gripper uses percent. Times are seconds from the start.",
  );
  updateControls();
}
$("operation-type").onchange = loadExample;
$("operation-mode").onchange = loadExample;
$("profile-select").onchange = updateControls;
$("load-example").onclick = loadExample;
function validateDraft() {
  const p = JSON.parse($("payload-editor").value),
    type = $("operation-type").value,
    mode = $("operation-mode").value;
  if (!p || typeof p !== "object" || Array.isArray(p))
    throw Error("The payload must be a JSON object.");
  const allowed = [
    "points",
    "positions",
    "gripper",
    "gripper_unit",
    "model_steps",
    "instruction",
    "cartesian",
    "payload_kind",
  ];
  for (const k of Object.keys(p))
    if (!allowed.includes(k))
      throw Error(
        `Unknown semantic field: ${k}. Transport identity is supplied by the station client.`,
      );
  const validatePositions = (q) => {
    if (!q || typeof q !== "object" || !Object.keys(q).length)
      throw Error("Add named joint targets.");
    const config = armConfig();
    for (const [name, v] of Object.entries(q)) {
      const j = config.joints?.find((j) => j.name === name);
      if (!j) throw Error("Unknown joint: " + name);
      if (
        !Number.isFinite(v) ||
        v < (j.min ?? j.min_rad) ||
        v > (j.max ?? j.max_rad)
      )
        throw Error(name + " is outside its configured range.");
    }
  };
  if (type === "MODEL") {
    if (!$("profile-select").value)
      throw Error("Select a registered policy profile.");
    if (
      mode === "INLINE" &&
      (!Array.isArray(p.model_steps) || !p.model_steps.length)
    )
      throw Error("Add at least one model step.");
    if (mode === "STREAM" && typeof p.instruction !== "string")
      throw Error("A model stream payload needs an instruction string.");
  } else if (p.cartesian) {
    if (type !== "TRAJECTORY" || mode !== "INLINE")
      throw Error(
        "Cartesian endpoints are only available as an INLINE trajectory.",
      );
    if (!armConfig().capabilities?.cartesian_endpoints)
      throw Error("Cartesian endpoints are not configured for this arm.");
    if (!Array.isArray(p.cartesian) || !p.cartesian.length)
      throw Error("Add Cartesian endpoints.");
  } else if (mode === "STREAM" && type === "DIRECT") {
    validatePositions(p.positions);
    if (!Number.isFinite(p.gripper) || p.gripper < 0 || p.gripper > 100)
      throw Error("Gripper must be between 0 and 100 percent.");
  } else {
    if (!Array.isArray(p.points) || !p.points.length)
      throw Error("Add at least one timed point.");
    let last = 0;
    for (const point of p.points) {
      if (
        type === "DIRECT" &&
        (!Number.isFinite(point.gripper) ||
          point.gripper < 0 ||
          point.gripper > 100)
      )
        throw Error(
          "Each direct step needs an explicit measured or intended gripper target (0–100%).",
        );
      if (!Number.isFinite(point.time_s) || point.time_s <= last)
        throw Error("Point times must be positive and strictly increasing.");
      last = point.time_s;
      validatePositions(point.positions);
    }
  }
  const duration = $("duration").value;
  if (duration && !(Number(duration) > 0))
    throw Error("Duration limit must be positive.");
  client.validatePayload(selectedArm, type, mode, p);
  return p;
}
$("validate-payload").onclick = () => {
  try {
    validateDraft();
    text(
      "validation-message",
      "Draft syntax and configured bounds checked. Runtime admission still applies.",
    );
  } catch (e) {
    text("validation-message", e.message);
  }
};
$("run-payload").onclick = () =>
  safely(async () => {
    const payload = validateDraft();
    await client.submit(
      selectedArm,
      $("operation-type").value,
      $("operation-mode").value,
      payload,
      $("profile-select").value,
      {
        duration_s: $("duration").value
          ? Number($("duration").value)
          : undefined,
      },
    );
  });
$("send-update").onclick = () =>
  safely(() => client.updateStream(validateDraft()));
$("end-stream").onclick = () => safely(() => client.endOperation());
const payloadElement = (tag, className, content) => {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (content !== undefined) el.textContent = content;
  return el;
};
function payloadContext() {
  return {
    type: $("operation-type").value,
    mode: $("operation-mode").value,
    armId: selectedArm,
    profileId: $("profile-select").value,
  };
}
function refreshPayloadDraft(openFirst = false) {
  const context = payloadContext();
  if (
    !payloadDraft ||
    getJson(payloadDraft) !== $("payload-editor").value ||
    ["type", "mode", "armId"].some(
      (key) => payloadDraft.context[key] !== context[key],
    )
  ) {
    payloadDraft = openDraft(context, $("payload-editor").value);
  }
  if (openFirst) activePayloadRow = null;
  renderPayloadBlocks();
  updatePayloadHelp();
}
function choosePayloadView(view) {
  payloadView = view;
  for (const tab of document.querySelectorAll("[data-payload-view]")) {
    const active = tab.dataset.payloadView === view;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  }
  $("payload-blocks-panel").hidden = view !== "blocks";
  $("payload-json-panel").hidden = view !== "json";
  if (view === "blocks") {
    refreshPayloadDraft();
    if (payloadDraft.error) text("validation-message", "");
  }
}
function changeBlock(update, rerender = false, focusIndex = null) {
  try {
    payloadDraft = update(payloadDraft);
    $("payload-editor").value = getJson(payloadDraft);
    text("validation-message", "");
    if (focusIndex !== null)
      activePayloadRow =
        describeBlocks(payloadDraft).rows[focusIndex]?.id || null;
    if (rerender) renderPayloadBlocks();
    else {
      renderBlockIssues();
      updateBlockSummaries();
    }
    updateControls();
  } catch (error) {
    text("payload-block-error", error.message);
  }
}
function renderBlockIssues() {
  const info = describeBlocks(payloadDraft);
  const messages = info.issues.map((x) =>
    typeof x === "string" ? x : x.message,
  );
  text("payload-block-error", messages.join(" "));
  $("add-payload-step").hidden = !info.collection;
  const limit =
    payloadContext().type === "TRAJECTORY" && info.kind === "joint_sequence"
      ? 256
      : 16;
  $("add-payload-step").disabled =
    !info.structuralEditsEnabled || info.rows.length >= limit;
  $("add-payload-step").title =
    info.rows.length >= limit ? `Maximum ${limit} steps` : "";
  return info;
}
function blockSummary(row, kind) {
  const value = row.value || {};
  if (kind === "joint_sequence")
    return `${Object.keys(value.positions || {}).length} joints · at ${number(value.time_s, 2)} s`;
  if (kind === "direct_target") return "Continuous joint target";
  if (kind === "model_sequence")
    return value.duration_s == null || value.duration_s === 0
      ? "Profile duration"
      : `${number(value.duration_s, 2)} s`;
  if (kind === "model_task") return "Replace current task";
  return `${value.frame_id || "Choose a frame"} · ${number(value.duration_s, 2)} s`;
}
function updateBlockSummaries() {
  const info = describeBlocks(payloadDraft);
  for (const row of info.rows) {
    const card = [...$("payload-blocks").children].find(
      (x) => x.dataset.rowId === row.id,
    );
    if (card)
      card.querySelector(".payload-step-meta").textContent = blockSummary(
        row,
        info.kind,
      );
  }
}
function blockNumber(
  parent,
  label,
  value,
  path,
  { min, max, step = "any", optional = false } = {},
) {
  const field = payloadElement("label", "payload-block-field");
  field.append(
    payloadElement("span", "", label.replace(/^Step \d+\s*(?:·\s*)?/, "")),
  );
  const input = document.createElement("input");
  input.type = "number";
  input.required = !optional;
  input.step = String(step);
  if (min !== undefined) input.min = String(min);
  if (max !== undefined) input.max = String(max);
  input.value = Number.isFinite(value) ? String(value) : "";
  input.placeholder = optional ? "Default" : "Required";
  input.setAttribute("aria-label", label);
  input.oninput = () =>
    changeBlock((draft) =>
      setValue(
        draft,
        path,
        input.value === ""
          ? optional
            ? undefined
            : null
          : Number(input.value),
      ),
    );
  field.append(input);
  parent.append(field);
  return input;
}
function blockText(
  parent,
  label,
  value,
  path,
  { multiline = false, placeholder = "" } = {},
) {
  const field = payloadElement(
    "label",
    "payload-block-field" + (multiline ? " full" : ""),
  );
  field.append(
    payloadElement("span", "", label.replace(/^Step \d+\s*(?:·\s*)?/, "")),
  );
  const input = document.createElement(multiline ? "textarea" : "input");
  if (!multiline) input.type = "text";
  input.value = typeof value === "string" ? value : "";
  input.placeholder = placeholder;
  input.setAttribute("aria-label", label);
  if (multiline) input.rows = 3;
  input.oninput = () =>
    changeBlock((draft) => setValue(draft, path, input.value));
  field.append(input);
  parent.append(field);
  return input;
}
function renderJointFields(body, row, type) {
  const value = row.value;
  if (
    !value.positions ||
    typeof value.positions !== "object" ||
    Array.isArray(value.positions)
  ) {
    body.append(
      payloadElement(
        "p",
        "detail-note",
        "The positions field must be an object. Repair it in JSON or use the current pose.",
      ),
    );
    return;
  }
  const config = armConfig(),
    names = [
      ...new Set([
        ...(config.joints || []).map((j) => j.name),
        ...Object.keys(value.positions),
      ]),
    ];
  const fields = payloadElement("div", "payload-joint-fields");
  for (const name of names) {
    const joint = config.joints?.find((j) => j.name === name);
    blockNumber(
      fields,
      `Step ${row.index + 1} · ${name} (rad)`,
      value.positions[name],
      [...row.path, "positions", name],
      {
        min: joint?.min ?? joint?.min_rad,
        max: joint?.max ?? joint?.max_rad,
        step: "any",
      },
    );
  }
  if (type === "DIRECT")
    blockNumber(
      fields,
      `Step ${row.index + 1} · gripper (%)`,
      value.gripper,
      [...row.path, "gripper"],
      { min: 0, max: 100, step: "any" },
    );
  body.append(fields);
}
function renderCartesianFields(body, row) {
  const value = row.value;
  blockText(body, `Step ${row.index + 1} · frame`, value.frame_id, [
    ...row.path,
    "frame_id",
  ]);
  const xyz = payloadElement("div", "payload-vector-fields");
  for (const [i, axis] of ["X", "Y", "Z"].entries())
    blockNumber(
      xyz,
      `Step ${row.index + 1} · ${axis} (m)`,
      value.position?.[i],
      [...row.path, "position", i],
    );
  body.append(xyz);
  const field = payloadElement("label", "payload-block-field");
  field.append(payloadElement("span", "", "Orientation"));
  const select = document.createElement("select");
  select.setAttribute("aria-label", `Step ${row.index + 1} orientation`);
  select.add(new Option("Position only", "POSITION_ONLY"));
  select.add(new Option("Strict pose", "STRICT_POSE"));
  select.value = value.orientation_mode || "POSITION_ONLY";
  select.onchange = () =>
    changeBlock((draft) => {
      let next = setValue(
        draft,
        [...row.path, "orientation_mode"],
        select.value,
      );
      if (
        !Array.isArray(row.value.orientation) ||
        row.value.orientation.length !== 4
      )
        next = setValue(next, [...row.path, "orientation"], [0, 0, 0, 1]);
      return next;
    }, true);
  field.append(select);
  body.append(field);
  if (select.value === "STRICT_POSE") {
    const quaternion = payloadElement("div", "payload-vector-fields four");
    for (const [i, axis] of ["Qx", "Qy", "Qz", "Qw"].entries())
      blockNumber(
        quaternion,
        `Step ${row.index + 1} · ${axis}`,
        value.orientation?.[i],
        [...row.path, "orientation", i],
      );
    body.append(quaternion);
  }
  blockNumber(
    body,
    `Step ${row.index + 1} duration (s)`,
    value.duration_s,
    [...row.path, "duration_s"],
    { min: 0.001 },
  );
}
function renderPayloadBlocks() {
  if (!payloadDraft)
    payloadDraft = openDraft(payloadContext(), $("payload-editor").value);
  const info = renderBlockIssues(),
    box = $("payload-blocks");
  box.replaceChildren();
  const context = payloadContext();
  $("payload-target-field").hidden =
    context.type !== "TRAJECTORY" || context.mode !== "INLINE";
  const target = $("payload-target-kind");
  target.value = info.kind === "cartesian_sequence" ? "cartesian" : "joints";
  target.options[1].disabled = !armConfig().capabilities?.cartesian_endpoints;
  if (!payloadDraft.valid) {
    box.append(
      payloadElement(
        "p",
        "detail-note",
        "The JSON draft is incomplete. Open JSON to repair it; no earlier draft will be sent.",
      ),
    );
    return;
  }
  if (!info.rows.length) {
    box.append(
      payloadElement(
        "div",
        "payload-empty",
        info.collection
          ? "Add a step to build this sequence."
          : "This payload structure cannot be shown as blocks. Open JSON to edit it.",
      ),
    );
    return;
  }
  const title =
    {
      joint_sequence: context.type === "DIRECT" ? "Joint target" : "Waypoint",
      direct_target: "Joint target",
      model_sequence: "Model task",
      model_task: "Model task",
      cartesian_sequence: "Cartesian endpoint",
    }[info.kind] || "Step";
  for (const row of info.rows) {
    const card = payloadElement("details", "payload-block-card");
    card.dataset.rowId = row.id;
    card.open = activePayloadRow
      ? activePayloadRow === row.id
      : row.index === 0;
    const heading = payloadElement("summary", "payload-block-heading");
    heading.append(
      payloadElement(
        "span",
        "payload-step-number",
        String(row.index + 1).padStart(2, "0"),
      ),
      payloadElement("span", "payload-step-title", title),
      payloadElement("span", "payload-step-meta", blockSummary(row, info.kind)),
      payloadElement(
        "span",
        "payload-step-toggle",
        card.open ? "Collapse" : "Edit",
      ),
    );
    card.append(heading);
    card.ontoggle = () => {
      card.querySelector(".payload-step-toggle").textContent = card.open
        ? "Collapse"
        : "Edit";
      if (card.open) {
        activePayloadRow = row.id;
        for (const other of box.querySelectorAll("details"))
          if (other !== card) other.open = false;
      }
    };
    const body = payloadElement("div", "payload-block-body");
    card.append(body);
    if (
      !row.value ||
      typeof row.value !== "object" ||
      Array.isArray(row.value)
    ) {
      body.append(
        payloadElement(
          "p",
          "detail-note",
          "This step must be an object. Repair it in JSON.",
        ),
      );
      box.append(card);
      continue;
    }
    if (info.kind === "joint_sequence") {
      blockNumber(
        body,
        `Step ${row.index + 1} time from start (s)`,
        row.value.time_s,
        [...row.path, "time_s"],
        { min: 0.001 },
      );
      renderJointFields(body, row, context.type);
    } else if (info.kind === "direct_target")
      renderJointFields(body, row, "DIRECT");
    else if (info.kind === "model_sequence" || info.kind === "model_task") {
      blockText(
        body,
        `Step ${row.index + 1} instruction`,
        row.value.instruction,
        [...row.path, "instruction"],
        { multiline: true, placeholder: "Describe the intended task" },
      );
      if (info.kind === "model_sequence")
        blockNumber(
          body,
          `Step ${row.index + 1} duration (s)`,
          row.value.duration_s,
          [...row.path, "duration_s"],
          { min: 0, optional: true },
        );
    } else renderCartesianFields(body, row);
    if (info.collection) {
      const tools = payloadElement("div", "payload-step-tools");
      for (const [caption, to] of [
        ["Move up", row.index - 1],
        ["Move down", row.index + 1],
      ]) {
        const b = payloadElement("button", "", caption);
        b.setAttribute("aria-label", `${caption} step ${row.index + 1}`);
        b.disabled =
          !info.structuralEditsEnabled || to < 0 || to >= info.rows.length;
        b.onclick = () =>
          changeBlock((draft) => moveStep(draft, row.id, to), true, to);
        tools.append(b);
      }
      const remove = payloadElement("button", "remove-step", "Remove");
      remove.setAttribute("aria-label", `Remove step ${row.index + 1}`);
      remove.disabled = !info.structuralEditsEnabled;
      remove.onclick = () =>
        changeBlock(
          (draft) => removeStep(draft, row.id),
          true,
          Math.max(0, row.index - 1),
        );
      tools.append(remove);
      body.append(tools);
    }
    box.append(card);
  }
}
function addPayloadStep() {
  const info = describeBlocks(payloadDraft),
    last = info.rows.at(-1)?.value || {},
    context = payloadContext();
  let seed;
  if (info.kind === "joint_sequence")
    seed = {
      duration_s: 1,
      positions: last.positions ? structuredClone(last.positions) : joints(),
      ...(context.type === "DIRECT"
        ? {
            gripper: Object.hasOwn(last, "gripper")
              ? last.gripper
              : (armState().gripper_percent ?? null),
          }
        : {}),
    };
  else if (info.kind === "model_sequence") seed = { instruction: "" };
  else if (info.kind === "cartesian_sequence")
    seed = {
      frame_id: last.frame_id || armConfig().base_frame || "",
      position: [null, null, null],
      orientation_mode: "POSITION_ONLY",
      orientation: [0, 0, 0, 1],
      duration_s: 1,
    };
  else return;
  changeBlock((draft) => addStep(draft, seed), true, info.rows.length);
}
for (const tab of document.querySelectorAll("[data-payload-view]"))
  tab.onclick = () => choosePayloadView(tab.dataset.payloadView);
$("payload-editor").oninput = () => {
  payloadDraft = openDraft(payloadContext(), $("payload-editor").value);
  text("validation-message", payloadDraft.error?.message || "");
  updateControls();
};
$("add-payload-step").onclick = addPayloadStep;
$("payload-target-kind").onchange = () => {
  const next = $("payload-target-kind").value,
    current =
      describeBlocks(payloadDraft).kind === "cartesian_sequence"
        ? "cartesian"
        : "joints";
  payloadRepresentations[current] = getJson(payloadDraft);
  if (next === "cartesian" && !armConfig().capabilities?.cartesian_endpoints) {
    $("payload-target-kind").value = current;
    return;
  }
  const value =
    next === "cartesian"
      ? {
          payload_kind: "CARTESIAN_TARGET",
          cartesian: [
            {
              frame_id: armConfig().base_frame || "",
              position: [null, null, null],
              orientation_mode: "POSITION_ONLY",
              orientation: [0, 0, 0, 1],
              duration_s: 1,
            },
          ],
        }
      : { points: [{ time_s: 1, positions: joints() }] };
  $("payload-editor").value =
    payloadRepresentations[next] || JSON.stringify(value, null, 2);
  refreshPayloadDraft(true);
};

function cameraAspect(id) {
  const item = cameraFrames.get(id);
  return item?.aspect || 4 / 3;
}
function refreshCameraLabels() {
  for (const [id, item] of cameraFrames) {
    const descriptor = (resources.cameras || []).find(camera => camera.id === id);
    const name = descriptor?.label || id;
    item.label.textContent = name.toUpperCase();
    item.name.textContent = name;
    item.img.alt = name + " camera";
    item.fig.setAttribute("aria-label", `Make ${name} primary camera`);
  }
}
function updateCameras(ids) {
  refreshCameraLabels();
  if (JSON.stringify(ids) === JSON.stringify(cameraIds)) return;
  for (const id of cameraIds)
    if (!ids.includes(id)) {
      client.closeCamera(id);
      cameraFrames.delete(id);
      pipPositions.delete(id);
    }
  cameraIds = ids;
  if (!ids.includes(primaryCamera)) primaryCamera = ids[0] || "";
  for (const id of ids) {
    if (cameraFrames.has(id)) continue;
    const config = (resources.cameras || []).find((c) => c.id === id) || {};
    const fig = document.createElement("figure");
    fig.className = "camera-frame";
    fig.tabIndex = 0;
    fig.setAttribute("role", "button");
    fig.setAttribute("aria-label", `Make ${config.label || id} primary camera`);
    fig.dataset.cameraId = id;
    const img = document.createElement("img");
    img.alt = (config.label || id) + " camera";
    img.draggable = false;
    const label = document.createElement("span");
    label.className = "camera-label";
    label.textContent = (config.label || id).toUpperCase();
    const waiting = document.createElement("span");
    waiting.className = "camera-wait";
    waiting.textContent = "Waiting for frames";
    const caption = document.createElement("figcaption"),
      name = document.createElement("span"),
      status = document.createElement("span");
    name.textContent = config.label || id;
    status.textContent = "Waiting";
    caption.append(name, status);
    fig.append(img, label, waiting, caption);
    const item = {
      fig,
      img,
      label,
      name,
      status,
      frames: 0,
      lastAt: 0,
      fps: null,
      aspect:
        config.width && config.height ? config.width / config.height : 4 / 3,
      suppressClickUntil: 0,
    };
    cameraFrames.set(id, item);
    img.onload = () => {
      const ratio = img.naturalWidth / img.naturalHeight;
      if (
        Number.isFinite(ratio) &&
        ratio > 0 &&
        Math.abs(ratio - item.aspect) > 0.001
      ) {
        item.aspect = ratio;
        layoutCameraFrames();
      }
    };
    const promote = () => {
      if (performance.now() < item.suppressClickUntil) return;
      primaryCamera = id;
      arrangeCameras();
    };
    fig.onclick = promote;
    fig.onkeydown = (e) => {
      if (["Enter", " "].includes(e.key)) {
        e.preventDefault();
        promote();
      } else if (
        cameraLayout === "floating" &&
        id !== primaryCamera &&
        ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(e.key)
      ) {
        e.preventDefault();
        const r = item.layout,
          b = item.dragBounds;
        if (!r || !b) return;
        movePip(
          id,
          r.x + (e.key === "ArrowRight" ? 12 : e.key === "ArrowLeft" ? -12 : 0),
          r.y + (e.key === "ArrowDown" ? 12 : e.key === "ArrowUp" ? -12 : 0),
        );
      }
    };
    let drag = null;
    fig.onpointerdown = (e) => {
      if (cameraLayout !== "floating" || id === primaryCamera || e.button !== 0)
        return;
      const r = item.layout;
      if (!r) return;
      drag = {
        clientX: e.clientX,
        clientY: e.clientY,
        x: r.x,
        y: r.y,
        moved: false,
      };
      fig.setPointerCapture(e.pointerId);
      fig.classList.add("dragging");
    };
    fig.onpointermove = (e) => {
      if (!drag) return;
      const dx = e.clientX - drag.clientX,
        dy = e.clientY - drag.clientY;
      if (!drag.moved && Math.hypot(dx, dy) < 4) return;
      drag.moved = true;
      e.preventDefault();
      movePip(id, drag.x + dx, drag.y + dy);
    };
    const finishDrag = (e) => {
      if (!drag) return;
      if (drag.moved) item.suppressClickUntil = performance.now() + 350;
      drag = null;
      fig.classList.remove("dragging");
      if (fig.hasPointerCapture(e.pointerId))
        fig.releasePointerCapture(e.pointerId);
    };
    fig.onpointerup = finishDrag;
    fig.onpointercancel = finishDrag;
    client.openCamera(id, (frame) => {
      const item = cameraFrames.get(id);
      if (!item) return;
      if (frame.url) {
        item.img.src = frame.url;
        item.fig.classList.add("has-frame");
        item.fig.classList.remove("stale");
        const now = performance.now();
        item.lastFrameAt = now;
        item.connectionState = "frame";
        item.frames++;
        if (!item.lastAt) item.lastAt = now;
        if (now - item.lastAt >= 1000) {
          item.fps = (item.frames * 1000) / (now - item.lastAt);
          item.frames = 0;
          item.lastAt = now;
        }
        item.status.textContent =
          (item.fps ? number(item.fps) + " FPS received" : "Receiving") +
          (item.img.naturalWidth
            ? " · " + item.img.naturalWidth + " × " + item.img.naturalHeight
            : "");
        item.status.title = item.status.textContent;
      } else if (frame.state) {
        item.connectionState = frame.state;
        item.status.textContent =
          frame.state === "reconnecting"
            ? "Reconnecting…"
            : frame.state === "waiting"
              ? "Waiting for frames"
              : frame.state === "decode_error"
                ? "Frame unavailable"
                : frame.state;
      }
    });
  }
  arrangeCameras();
}
function placeCamera(id, rect) {
  const item = cameraFrames.get(id);
  if (!item) return;
  item.layout = { ...rect };
  Object.assign(item.fig.style, {
    left: rect.x + "px",
    top: rect.y + "px",
    width: rect.width + "px",
    height: rect.height + "px",
  });
  item.img.style.height =
    (rect.imageHeight ?? Math.max(0, rect.height - 26)) + "px";
}
function movePip(id, x, y) {
  const item = cameraFrames.get(id),
    r = item.layout,
    b = item.dragBounds;
  if (!r || !b) return;
  const maxX = Math.max(b.x, b.x + b.width - r.width),
    maxY = Math.max(b.y, b.y + b.height - r.height);
  x = Math.max(b.x, Math.min(maxX, x));
  y = Math.max(b.y, Math.min(maxY, y));
  placeCamera(id, { ...r, x, y });
  pipPositions.set(id, {
    u: (x - b.x) / Math.max(1, maxX - b.x),
    v: (y - b.y) / Math.max(1, maxY - b.y),
  });
}
function arrangeCameras() {
  const stage = $("camera-stage");
  stage.className =
    "camera-stage layout-" +
    cameraLayout +
    (cameraIds.length === 1 ? " single-camera" : "");
  stage.replaceChildren();
  const dock = document.createElement("div");
  dock.className = "camera-dock";
  dock.setAttribute("aria-label", "Other camera views");
  for (const id of cameraIds) {
    const item = cameraFrames.get(id);
    item.fig.classList.toggle("primary-frame", id === primaryCamera);
    item.fig.classList.toggle(
      "floating-frame",
      cameraLayout === "floating" && id !== primaryCamera,
    );
    item.fig.setAttribute("aria-pressed", String(id === primaryCamera));
    item.fig.title =
      cameraLayout === "floating" && id !== primaryCamera
        ? "Drag to reposition · click to make primary · arrow keys to move"
        : "Click to make primary";
    if (cameraLayout === "primary" && id !== primaryCamera)
      dock.append(item.fig);
    else stage.append(item.fig);
  }
  if (dock.children.length) stage.append(dock);
  if (!cameraIds.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "No cameras configured or discovered.";
    stage.append(empty);
  }
  text(
    "camera-count",
    cameraIds.length +
      " camera" +
      (cameraIds.length === 1 ? "" : "s") +
      (cameraLayout === "floating"
        ? " · drag to position · click to promote"
        : " · click a view to promote"),
  );
  layoutCameraFrames();
}
function layoutCameraFrames() {
  const stage = $("camera-stage"),
    width = stage.clientWidth,
    height = stage.clientHeight,
    gap = 10,
    captionHeight = 26;
  if (!width || !height || !cameraIds.length) return;
  if (cameraLayout === "equal") {
    const rects = packEqualCameras({
      width,
      height,
      aspects: cameraIds.map(cameraAspect),
      gap,
      captionHeight,
    });
    rects.forEach((r, i) => placeCamera(cameraIds[i], r));
    return;
  }
  const supplements = cameraIds.filter((id) => id !== primaryCamera);
  if (cameraLayout === "primary" && supplements.length) {
    const dockHeight = Math.min(132, Math.max(88, height * 0.22));
    const image = fitRect(
      width,
      Math.max(1, height - dockHeight - gap - captionHeight),
      cameraAspect(primaryCamera),
    );
    const dock = stage.querySelector(".camera-dock");
    Object.assign(dock.style, {
      left: image.x + "px",
      top: "0px",
      width: image.width + "px",
      height: dockHeight + "px",
    });
    placeCamera(primaryCamera, {
      x: image.x,
      y: dockHeight + gap,
      width: image.width,
      height: image.height + captionHeight,
      imageHeight: image.height,
    });
    let x = 8;
    const imageHeight = Math.max(1, dockHeight - 16 - captionHeight);
    for (const id of supplements) {
      const w = imageHeight * cameraAspect(id);
      placeCamera(id, {
        x,
        y: 8,
        width: w,
        height: imageHeight + captionHeight,
        imageHeight,
      });
      x += w + 8;
    }
    return;
  }
  const image = fitRect(
    width,
    Math.max(1, height - captionHeight),
    cameraAspect(primaryCamera),
  );
  const main = {
    x: image.x,
    y: image.y,
    width: image.width,
    height: image.height + captionHeight,
    imageHeight: image.height,
  };
  placeCamera(primaryCamera, main);
  const bounds = {
    x: image.x + 8,
    y: image.y + 8,
    width: Math.max(1, image.width - 16),
    height: Math.max(1, image.height - 16),
  };
  let column = 0,
    cursorY = bounds.y + bounds.height,
    maxColumnWidth = 0,
    columnOffset = 0;
  for (const id of supplements) {
    const aspect = cameraAspect(id);
    let w = Math.min(
      210,
      Math.max(112, image.width * 0.25),
      bounds.width * 0.45,
    );
    w = Math.min(w, Math.max(1, (bounds.height - captionHeight) * aspect));
    let h = w / aspect + captionHeight;
    if (cursorY - h < bounds.y && maxColumnWidth) {
      columnOffset += maxColumnWidth + 8;
      column++;
      cursorY = bounds.y + bounds.height;
      maxColumnWidth = 0;
    }
    const x = Math.max(bounds.x, bounds.x + bounds.width - w - columnOffset),
      y = Math.max(bounds.y, cursorY - h);
    cursorY = y - 8;
    maxColumnWidth = Math.max(maxColumnWidth, w);
    const saved = pipPositions.get(id),
      item = cameraFrames.get(id);
    item.dragBounds = bounds;
    placeCamera(id, {
      x: saved ? bounds.x + saved.u * Math.max(0, bounds.width - w) : x,
      y: saved ? bounds.y + saved.v * Math.max(0, bounds.height - h) : y,
      width: w,
      height: h,
      imageHeight: w / aspect,
    });
  }
}

for (const button of document.querySelectorAll("[data-layout]"))
  button.onclick = () => {
    cameraLayout = button.dataset.layout;
    document
      .querySelectorAll("[data-layout]")
      .forEach((b) => b.classList.toggle("active", b === button));
    arrangeCameras();
  };
$("camera-fullscreen").onclick = () =>
  safely(async () => {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await document.querySelector(".observation").requestFullscreen();
  });
document.addEventListener("fullscreenchange", () =>
  text("camera-fullscreen", document.fullscreenElement ? "Exit" : "Expand"),
);
$("events-toggle").onclick = () => {
  $("event-log").hidden = !$("event-log").hidden;
};
$("load-history").onclick = () =>
  safely(async () => {
    if (!selectedArm) throw Error("Select an arm first.");
    text("history-note", "Loading measured history…");
    try {
      const data = await client.request(
        "/api/history?arm=" + encodeURIComponent(selectedArm) + "&limit=120",
      );
      historyRows = (data.records || []).map((row) => {
        if (row.sample) return row;
        try {
          return {
            ...row,
            sample: JSON.parse(row.sample_json || row.json || "{}"),
          };
        } catch {
          return row;
        }
      });
      drawHistory();
      text(
        "history-note",
        historyRows.length
          ? "Measured radians · missing samples remain gaps."
          : "No measured history returned.",
      );
    } catch (e) {
      text("history-note", "History unavailable: " + e.message);
    }
  });
function drawHistory() {
  const c = $("history-chart"),
    r = c.getBoundingClientRect(),
    dpr = devicePixelRatio || 1;
  c.width = r.width * dpr;
  c.height = r.height * dpr;
  const ctx = c.getContext("2d");
  ctx.scale(dpr, dpr);
  const w = r.width,
    h = r.height;
  ctx.clearRect(0, 0, w, h);
  if (!historyRows.length) {
    ctx.fillStyle = "#62625d";
    ctx.font = "11px Helvetica";
    ctx.fillText("Refresh to inspect measured history", 10, h / 2);
    return;
  }
  const colors = [
    "#191919",
    "#2359ba",
    "#d52b22",
    "#36889a",
    "#9b5730",
    "#888",
  ];
  let range = 0.1;
  for (const row of historyRows)
    for (const q of row.sample?.measured_joints?.position || [])
      if (Number.isFinite(q)) range = Math.max(range, Math.abs(q));
  const count = Math.max(
    ...historyRows.map((r) => r.sample?.measured_joints?.position?.length || 0),
  );
  for (let j = 0; j < count; j++) {
    ctx.beginPath();
    ctx.strokeStyle = colors[j % colors.length];
    ctx.lineWidth = 1;
    let pen = false;
    historyRows.forEach((row, i) => {
      const q = row.sample?.measured_joints?.position?.[j];
      if (!row.sample?.valid || !Number.isFinite(q)) {
        pen = false;
        return;
      }
      const x = (i / Math.max(1, historyRows.length - 1)) * w,
        y = h / 2 - (q / range) * (h / 2 - 5);
      if (pen) ctx.lineTo(x, y);
      else ctx.moveTo(x, y);
      pen = true;
    });
    ctx.stroke();
  }
}
setInterval(() => {
  for (const item of cameraFrames.values()) {
    if (item.lastFrameAt && performance.now() - item.lastFrameAt > 2000) {
      item.fig.classList.add("stale");
      if (item.connectionState !== "reconnecting")
        item.status.textContent =
          "No frame received for " +
          Math.floor((performance.now() - item.lastFrameAt) / 1000) +
          " s";
    }
  }
}, 1000);
new ResizeObserver(drawHistory).observe($("history-chart"));
new ResizeObserver(layoutCameraFrames).observe($("camera-stage"));
graph = new GraphExplorer({
  request: (path, body) => client.request(path, body),
  onError: showError,
});
manual = new ManualPanel({
  element: $("panel-manual"),
  poseElement: $("manual-fk"),
  actionElement: $("operator-control-actions"),
  client,
  onError: showError,
  onNotice: notice,
  onConditioning: result => {
    if(result.armId!==selectedArm || result.controllerSession!==armState().controller_session)return;
    text('summary-conditioning',connected?result.value:'—');
    $('summary-conditioning').title=connected?result.detail:'Disconnected';
    $('summary-conditioning').style.color=result.color;
  },
  onCameraCycle: () => {
    if (!cameraIds.length) return;
    primaryCamera = cameraIds[(cameraIds.indexOf(primaryCamera) + 1) % cameraIds.length];
    if (cameraLayout === 'equal') cameraLayout = 'primary';
    document.querySelectorAll('[data-layout]').forEach(button => button.classList.toggle('active',button.dataset.layout === cameraLayout));
    arrangeCameras();
  },
  onCameraLayout: () => {
    const layouts = ['equal','primary','floating'];
    cameraLayout = layouts[(layouts.indexOf(cameraLayout) + 1) % layouts.length];
    document.querySelectorAll('[data-layout]').forEach(button => button.classList.toggle('active',button.dataset.layout === cameraLayout));
    arrangeCameras();
  },
});
policies = new PolicyManagement({ element: $("policies"), client, onError: showError, onNotice: notice });
loadExample();
drawHistory();

void safely(() => setPage(location.hash.slice(1), { fromHash: true, initial: true }));

// The station is already reached through the owner's private connection.
void connectStation();
