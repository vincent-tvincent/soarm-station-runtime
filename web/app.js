import { StationClient } from "./transport.js";
import { GraphExplorer } from "./graph.js";
import { fitRect, packEqualCameras } from "./camera-layout.js";
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
  cameraLayout = "primary",
  primaryCamera = "",
  cameraIds = [],
  cameraFrames = new Map(),
  pipPositions = new Map(),
  manualTarget = null,
  noticeTimer,
  graph,
  connected = false,
  operation = null,
  historyRows = [];
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
  connected = (typeof info === "string" ? info : info.state) === "connected";
  text(
    "connection",
    connected ? "Connected" : info.reason || info.state || "Disconnected",
  );
  graph?.setConnected(connected);
  $("disconnect").hidden = !connected;
  if (info.state === "disconnected") {
    cameraIds = [];
    cameraFrames.clear();
    arrangeCameras();
    $("login").hidden = false;
    operation = null;
  }
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
      operation &&
      !["done", "ended", "error", "idle", "result", "closed"].includes(
        operation.state,
      );
  $("enable-control").disabled =
    !connected ||
    !selectedArm ||
    armConfig().read_only ||
    a.permission ||
    !!busy;
  $("hold").disabled = !connected || !selectedArm;
  $("start-manual").disabled =
    !connected ||
    !selectedArm ||
    !a.permission ||
    !!busy ||
    !manualTarget ||
    !a.feedback_valid;
  $("run-payload").disabled =
    !connected ||
    !selectedArm ||
    !a.permission ||
    !!busy ||
    ($("operation-type").value === "MODEL" && !$("profile-select").value);
  $("arm-select").disabled = !!busy;
  $("send-update").disabled =
    !busy ||
    operation?.type !== $("operation-type").value ||
    $("operation-mode").value !== "STREAM";
  $("send-update").title = $("send-update").disabled
    ? "Choose the active stream type and STREAM delivery to update it."
    : "";
  $("end-stream").disabled = !busy;
}
$("login").onsubmit = async (event) => {
  event.preventDefault();
  text("login-error", "");
  try {
    await client.connect($("token").value.trim());
    $("token").value = "";
    $("login").hidden = true;
    connected = true;
    text("connection", "Connected");
    $("disconnect").hidden = false;
    try {
      resources =
        snapshot.resources || (await client.request("/api/resources"));
    } catch {
      resources = snapshot.resources || {};
    }
    renderState(snapshot);
    await graph.refresh();
  } catch (e) {
    text("login-error", e.message);
  }
};
$("disconnect").onclick = () => {
  client.disconnect();
  connected = false;
  cameraIds = [];
  cameraFrames.clear();
  arrangeCameras();
  $("login").hidden = false;
  text("connection", "Disconnected");
  $("disconnect").hidden = true;
  for (const item of cameraFrames.values()) {
    item.status.textContent = "Disconnected";
  }
  graph.setConnected(false);
  updateControls();
};
function setPage(value) {
  const page = value === "graph" ? "graph" : "operations";
  $("app").dataset.view = page;
  document
    .querySelectorAll("[data-page]")
    .forEach((b) => b.classList.toggle("active", b.dataset.page === page));
  document.querySelector(".brand").textContent =
    page === "graph" ? "ROS / GRAPH EXPLORER" : "SOARM / STATION";
  $("operations").hidden = page !== "operations";
  $("graph").hidden = page !== "graph";
  text("page-title", page === "graph" ? "ROS graph" : "Robot operations");
  text("page-series", page === "graph" ? "OBSERVABILITY / 02" : "CONTROL / 01");
  graph?.setVisible(page === "graph");
  if (page === "graph" && connected) graph.refresh();
}
for (const item of document.querySelectorAll("[data-page]"))
  item.onclick = () => {
    location.hash = item.dataset.page;
    setPage(item.dataset.page);
  };
window.addEventListener("hashchange", () => setPage(location.hash.slice(1)));
for (const tab of document.querySelectorAll("[data-tab]"))
  tab.onclick = () => {
    activeTab = tab.dataset.tab;
    document.querySelectorAll("[data-tab]").forEach((b) => {
      b.classList.toggle("active", b === tab);
      b.setAttribute("aria-selected", String(b === tab));
    });
    for (const n of ["status", "manual", "payload"])
      $("panel-" + n).hidden = n !== activeTab;
  };
$("arm-select").onchange = () => {
  selectedArm = $("arm-select").value;
  selectedMotor = "";
  manualTarget = null;
  renderManual();
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
    await client.hold(selectedArm);
    notice("Hold requested. Support torque is preserved.");
  });
function renderState(state) {
  snapshot = state || {};
  resources = state.resources || resources;
  graph?.setConnected(true);
  const env = state.environment || state.preview?.label || "";
  text("environment", typeof env === "string" ? env : env.label || "");
  const arms = Array.from(
    new Set([
      ...Object.keys(state.arms || {}),
      ...(Array.isArray(resources.arms)
        ? resources.arms.map((a) => a.id)
        : Object.keys(resources.arms || {})),
    ]),
  );
  const current = $("arm-select").value;
  if (JSON.stringify(arms) !== $("arm-select").dataset.ids) {
    $("arm-select").replaceChildren(
      new Option("Select an arm", ""),
      ...arms.map((id) => new Option(id, id)),
    );
    $("arm-select").dataset.ids = JSON.stringify(arms);
    $("arm-select").value = arms.includes(current) ? current : "";
    if (!arms.includes(selectedArm)) selectedArm = "";
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
  text("local-count", state.upload?.local_committed?.toLocaleString() ?? "—");
  text("remote-count", state.upload?.remote_committed?.toLocaleString() ?? "—");
  text("queued", state.upload?.queued?.toLocaleString() ?? "—");
  text(
    "upload-label",
    state.upload?.error
      ? "History upload · awaiting collector"
      : "History upload",
  );
  renderMotors();
  if (selectedArm && !manualTarget) renderManual();
  const ids = (resources.cameras || state.cameras || []).map((c) =>
    typeof c === "string" ? c : c.id,
  );
  updateCameras(ids);
  const profiles = profileList().filter(
    (p) => !p.arm_id || p.arm_id === selectedArm,
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
    snapshot.motors?.[selectedArm] ||
    snapshot.motor_status?.[selectedArm]?.motors ||
    snapshot.health?.[selectedArm]?.motors ||
    armState().motors ||
    [];
  return Array.isArray(source)
    ? source
    : Object.entries(source).map(([name, m]) => ({ name, ...m }));
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
  const motorRecord = snapshot.motor_status?.[selectedArm];
  text(
    "motor-source",
    motorRecord?.error
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
          return `<tr tabindex="0" data-motor="${esc(name)}" class="${name === selectedMotor ? "selected" : ""}"><td><span class="motor-name" title="${esc(name)}"><span class="motor-id">${esc(m.id ?? m.motor_id ?? "—")}</span>${esc(name)}</span></td><td>${number(value, unit === "rad" ? 3 : 1)}${unit === "rad" ? "" : unit === "percent" ? "%" : ""}</td><td>${label}</td><td>${number(m.temperature_c, 0)}</td><td>${number(m.voltage_v)}</td>${hasCurrent ? `<td>${number(m.current_a, 3)}</td>` : ""}</tr>`;
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
    const faults = m.faults ?? m.fault_status ?? m.fault;
    const faultText =
      faults === null || faults === undefined
        ? "not reported"
        : Array.isArray(faults)
          ? faults.join(", ") || "none reported"
          : String(faults);
    text(
      "motor-detail",
      `${selectedMotor} · target ${number(goal, 3)} ${unit} · fault ${faultText}`,
    );
  } else
    text(
      "motor-detail",
      "Positions in radians; gripper in %. Select a motor for its target and reported faults.",
    );
}
function renderManual() {
  const cfg = armConfig(),
    p = joints(),
    grip = armState().gripper_percent;
  $("manual-joints").replaceChildren();
  if (
    !selectedArm ||
    !cfg.joints?.length ||
    !cfg.joints.every((j) => Number.isFinite(p[j.name])) ||
    (cfg.gripper && !Number.isFinite(grip))
  ) {
    $("manual-joints").innerHTML =
      '<p class="detail-note">Waiting for joint limits and complete measured feedback, including the gripper.</p>';
    manualTarget = null;
    return;
  }
  manualTarget = {
    positions: { ...p },
    gripper: Number(grip),
    gripper_unit: "percent",
  };
  const controls = [
    ...cfg.joints.map((j) => ({
      name: j.name,
      min: j.min ?? j.min_rad,
      max: j.max ?? j.max_rad,
      value: p[j.name],
      unit: "rad",
    })),
    ...(cfg.gripper
      ? [
          {
            name: cfg.gripper.name || "gripper",
            min: 0,
            max: 100,
            value: grip,
            unit: "%",
          },
        ]
      : []),
  ];
  for (const j of controls) {
    const row = document.createElement("div");
    row.className = "manual-joint";
    const label = document.createElement("label");
    label.className = "joint-label";
    label.textContent = j.name;
    const numeric = document.createElement("input");
    Object.assign(numeric, {
      type: "number",
      min: String(j.min),
      max: String(j.max),
      step: j.unit === "%" ? "0.5" : "0.005",
      value: number(j.value, j.unit === "%" ? 1 : 3),
    });
    numeric.setAttribute("aria-label", j.name + " target " + j.unit);
    const slider = document.createElement("input");
    Object.assign(slider, {
      type: "range",
      min: String(j.min),
      max: String(j.max),
      step: numeric.step,
      value: String(j.value),
    });
    slider.setAttribute("aria-label", j.name + " slider");
    const update = (source) => {
      const v = Number(source.value);
      if (!Number.isFinite(v) || v < j.min || v > j.max) return;
      numeric.value = String(v);
      slider.value = String(v);
      if (j.unit === "%") manualTarget.gripper = v;
      else manualTarget.positions[j.name] = v;
      if (
        client.currentOperation?.type === "DIRECT" &&
        client.currentOperation?.mode === "STREAM"
      )
        client.setManualTarget({
          ...manualTarget,
          positions: { ...manualTarget.positions },
        });
    };
    numeric.oninput = () => update(numeric);
    slider.oninput = () => update(slider);
    label.append(numeric);
    const ends = document.createElement("div");
    ends.className = "range-endpoints";
    ends.innerHTML = `<span>${number(j.min, 1)} ${j.unit}</span><span>${number(j.max, 1)} ${j.unit}</span>`;
    row.append(label, slider, ends);
    $("manual-joints").append(row);
  }
  updateControls();
}
$("reset-targets").onclick = () => {
  renderManual();
  if (
    manualTarget &&
    client.currentOperation?.type === "DIRECT" &&
    client.currentOperation?.mode === "STREAM"
  )
    client.setManualTarget(manualTarget);
};
$("start-manual").onclick = () =>
  safely(async () => {
    await client.startManual(selectedArm);
    client.setManualTarget(manualTarget);
    text(
      "manual-note",
      "Manual stream is active. Changing a target sends it to the selected arm.",
    );
  });
function operationChanged(event) {
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
    text("manual-note", "Stream is not active. Targets remain local.");
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
function cameraAspect(id) {
  const item = cameraFrames.get(id);
  return item?.aspect || 4 / 3;
}
function updateCameras(ids) {
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
    fig.setAttribute("aria-label", `Make ${id} primary camera`);
    fig.dataset.cameraId = id;
    const img = document.createElement("img");
    img.alt = id + " camera";
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
      } else if (frame.state) item.status.textContent = frame.state;
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
loadExample();
drawHistory();

$("login").querySelector("button[type=submit]").disabled = false;

setPage(location.hash.slice(1));
