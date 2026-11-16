// A projected view of configured kinematics, following the original controller's SVG viewer.
const NS = "http://www.w3.org/2000/svg";
const DIFFERENCE_COLOR = "#b45b54";
const node = (tag, attrs = {}) => {
  const n = document.createElementNS(NS, tag);
  for (const [key, value] of Object.entries(attrs)) n.setAttribute(key, value);
  return n;
};
export function jointPositionDifferences(measured, target) {
  if (
    typeof measured?.frame_id !== "string" || !measured.frame_id ||
    measured.frame_id !== target?.frame_id ||
    [measured, target].some(g => g.units?.position && g.units.position !== "m")
  ) return [];
  const named = geometry => {
    const joints = new Map();
    for (const joint of Array.isArray(geometry.joints) ? geometry.joints : []) {
      if (typeof joint?.name !== "string" || !joint.name) continue;
      // Duplicate names are ambiguous, even when one duplicate has bad geometry.
      joints.set(joint.name, joints.has(joint.name) ? null : joint);
    }
    return joints;
  };
  const finitePoint = point => Array.isArray(point) && point.length === 3 && point.every(Number.isFinite);
  const actual = named(measured), desired = named(target), differences = [];
  for (const [name, joint] of actual) {
    const other = desired.get(name);
    if (!finitePoint(joint?.position_m) || !finitePoint(other?.position_m)) continue;
    const distance_mm = Math.hypot(...joint.position_m.map((value, axis) => other.position_m[axis] - value)) * 1000;
    if (!Number.isFinite(distance_mm) || distance_mm === 0) continue;
    differences.push({name, measured: [...joint.position_m], target: [...other.position_m], distance_mm});
  }
  return differences;
}
export class JointIssueColors {
  constructor() {
    this.states = new Map();
  }
  update(motors = [], {stale = false} = {}) {
    for (const motor of motors) {
      if(typeof motor?.name !== 'string' || !motor.name)continue;
      const issue = motor.motion_issue;
      if (!issue) continue;
      const previous = this.states.get(motor.name);
      let pending = previous?.pending || (previous ? [previous] : []);
      if (issue.active === true && ["fault", "stall"].includes(issue.kind)) {
        pending = [
          ...pending.filter((item) => item.kind !== issue.kind),
          issue,
        ];
      } else if (!stale && !['stale','unavailable'].includes(motor.source_state) &&
        issue.feedback_fresh && Array.isArray(issue.clear_kinds)) {
        pending = pending.filter(
          (item) => !issue.clear_kinds.includes(item.kind),
        );
      }
      if (pending.length) {
        const primary =
          pending.find((item) => item.kind === "fault") || pending.at(-1);
        this.states.set(motor.name, { ...primary, pending });
      } else this.states.delete(motor.name);
    }
    return this.states;
  }
  reset() {
    this.states.clear();
  }
}
export class JointIssueCache {
  constructor() {this.caches=new Map();this.sessions=new Map();}
  forArm(armId,controllerSession='') {
    const session=typeof controllerSession==='string' && controllerSession
      ?controllerSession:this.sessions.get(armId)||'';
    const previous=this.sessions.get(armId)||'';
    if(session && session!==previous) {
      this.caches.delete(JSON.stringify([armId,previous]));
      this.sessions.set(armId,session);
    }
    const key=JSON.stringify([armId,session]);
    if(!this.caches.has(key))this.caches.set(key,new JointIssueColors());
    return this.caches.get(key);
  }
  update(armId,controllerSession,motors=[],options={}) {
    const issues=this.forArm(armId,controllerSession);issues.update(motors,options);return issues;
  }
}
function issueLabel(issue) {
  if(['REFERENCE_OUTSIDE_ENVELOPE','TRACKING_ENVELOPE_LOST'].includes(issue.code))return 'Tracking blocked';
  if(issue.kind==='stall')return 'Tracking stalled';
  return {SERVO_PROTECTION:'Servo protection',FEEDBACK_ERROR:'Feedback error',
    CALIBRATION_MISMATCH:'Calibration fault'}[issue.code] || 'Reported fault';
}
function issueDetail(issue) {
  const error=Number.isFinite(issue.tracking_error)?
    `tracking error ${issue.tracking_error.toFixed(3)} ${issue.tracking_error_unit==='percent'?'%':issue.tracking_error_unit||''}`.trim():null;
  return [`${issueLabel(issue)} (${issue.kind}${issue.code?' · '+issue.code:''})`,issue.reason,
    error,issue.source?'source '+issue.source:null].filter(Boolean).join(' · ');
}
export function motorIssuePresentation(motor,states,{stale=false}={}) {
  const current=motor?.motion_issue,latched=states?.get(motor?.name);
  const pending=latched?.pending || (latched?[latched]:[]);
  const unavailable=stale || ['stale','unavailable'].includes(motor?.source_state);
  if(pending.length) {
    const retained=unavailable || !current?.feedback_fresh || pending.some(issue=>
      current?.active!==true || current.kind!==issue.kind || current.code!==issue.code);
    return {active:true,kind:latched.kind,state:retained?'retained':'active',
      label:issueLabel(latched)+(pending.length>1?` +${pending.length-1}`:'')+(retained?' · retained':''),
      detail:pending.map(issueDetail).join(' / ')+(retained?' · Retained report; awaiting a fresh explicit clear.':'')};
  }
  if(!unavailable && current?.state==='clear' && current.feedback_fresh && current.clear_kinds?.length)
    return {active:false,kind:null,state:'clear',label:'Following',detail:current.reason||'Fresh following feedback reported.'};
  return {active:false,kind:null,state:unavailable?'stale':'unknown',label:unavailable?'Status stale':'Not reported',
    detail:unavailable?'Motion issue status is stale or unavailable.':current?.reason||'No motor-specific motion issue observation was reported.'};
}
export class FKView {
  constructor(element) {
    this.element = element;
    this.issueCaches = new JointIssueCache();
    this.issues = this.issueCaches.forArm('');
    this.armId = "";
    this.controllerSession = "";
    this.yaw = -0.68;
    this.pitch = 0.42;
    this.zoom = 1;
    element.innerHTML = `<div class="panel-subheading"><h3>Arm pose</h3><div class="fk-jacobian" id="ik-mobility" title="Measured arm conditioning: 0 = singular; nearer 1 = more balanced motion."><span>Conditioning</span><strong id="ik-conditioning-value">—</strong><span id="ik-mobility-detail" class="sr-only">Measured pose</span></div><button type="button" class="text-button" data-reset>Reset view</button></div><div class="fk-stage" tabindex="0" aria-label="Arm pose. Drag to orbit, scroll to zoom, double-click to reset."><svg viewBox="0 0 420 300" role="img" aria-label="Measured robot joint positions"></svg><span class="fk-empty">Waiting for measured pose</span></div><div class="fk-legend"><span>— Measured</span><span class="muted">┄ Target</span><span class="fk-alert-key">● Fault / stall</span><span style="display:inline-flex;align-items:center;gap:4px"><svg width="18" height="10" viewBox="0 0 18 10" aria-hidden="true"><line x1="0" y1="5" x2="18" y2="5" stroke="${DIFFERENCE_COLOR}" stroke-width="1.1" stroke-dasharray="3 4"/></svg>Position difference</span></div><div class="fk-readout"></div><p class="detail-note fk-status">Drag to orbit · scroll to zoom</p>`;
    this.svg = element.querySelector("svg");
    this.stage = element.querySelector(".fk-stage");
    const reset = () => {
      this.yaw = -0.68;
      this.pitch = 0.42;
      this.zoom = 1;
      this.draw();
    };
    element.querySelector("[data-reset]").onclick = reset;
    this.stage.ondblclick = reset;
    this.stage.onpointerdown = (e) => {
      if (e.button !== 0) return;
      this.drag = { x: e.clientX, y: e.clientY };
      this.stage.setPointerCapture(e.pointerId);
    };
    this.stage.onpointermove = (e) => {
      if (!this.drag) return;
      this.yaw += (e.clientX - this.drag.x) * 0.012;
      this.pitch = Math.max(
        -1.25,
        Math.min(1.25, this.pitch + (e.clientY - this.drag.y) * 0.009),
      );
      this.drag = { x: e.clientX, y: e.clientY };
      this.draw();
    };
    this.stage.onpointerup = this.stage.onpointercancel = () => {
      this.drag = null;
    };
    this.stage.addEventListener(
      "wheel",
      (e) => {
        e.preventDefault();
        this.zoom = Math.max(
          0.5,
          Math.min(2.2, this.zoom * Math.exp(-e.deltaY * 0.0012)),
        );
        this.draw();
      },
      { passive: false },
    );
    this.stage.onkeydown = (e) => {
      if (
        !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home"].includes(
          e.key,
        )
      )
        return;
      e.preventDefault();
      e.stopPropagation();
      if (e.key === "Home") reset();
      else {
        this.yaw +=
          e.key === "ArrowRight" ? 0.08 : e.key === "ArrowLeft" ? -0.08 : 0;
        this.pitch = Math.max(
          -1.25,
          Math.min(
            1.25,
            this.pitch +
              (e.key === "ArrowDown" ? 0.08 : e.key === "ArrowUp" ? -0.08 : 0),
          ),
        );
        this.draw();
      }
    };
  }
  resetArm(armId = "",controllerSession = "") {
    this.armId = armId;
    this.controllerSession=controllerSession;
    this.issues = this.issueCaches.forArm(armId,controllerSession);
    this.geometry = null;
    this.target = null;
    this.draw();
  }
  update(geometry, target, motors, stale = false, overlayLabel = "Target",observation = {}) {
    this.element.querySelector(".fk-legend span:nth-child(2)").textContent =
      "┄ " + overlayLabel;
    this.geometry = geometry;
    this.target = target;
    if(observation.armId && observation.armId!==this.armId)this.controllerSession='';
    this.armId=observation.armId||this.armId;
    this.controllerSession=observation.controllerSession||this.controllerSession;
    this.issues=this.issueCaches.update(this.armId,this.controllerSession,motors,{stale});
    this.stale = stale;
    this.draw();
  }
  project(p) {
    const [x, y, z] = p,
      cy = Math.cos(this.yaw),
      sy = Math.sin(this.yaw),
      cp = Math.cos(this.pitch),
      sp = Math.sin(this.pitch);
    return {
      // Right/up/back form one right-handed camera basis. Positive pitch views
      // the arm from above; larger depth is nearer for the painter ordering.
      x: 210 + (-cy * x + sy * y) * 650,
      y: 245 - (cp * z - sp * (sy * x + cy * y)) * 650,
      depth: cp * (sy * x + cy * y) + sp * z,
    };
  }
  line(a, b, attributes) {
    const p = this.project(a),
      q = this.project(b);
    this.svg.append(
      node("line", { x1: p.x, y1: p.y, x2: q.x, y2: q.y, ...attributes }),
    );
  }
  frameAxes(origin, rotation, length) {
    return ["X", "Y", "Z"].map((label, axis) => ({
      label,
      origin,
      end: origin.map((value, row) => value + length * rotation[row][axis]),
    }));
  }
  drawAxes(axes, frame) {
    for (const { label, origin, end } of axes) {
      this.line(origin, end, {
        stroke: "#aaa",
        "stroke-width": 1,
        "data-frame": frame,
        "data-axis": label,
      });
      const q = this.project(end),
        t = node("text", {
          x: q.x + 5,
          y: q.y - 4,
          fill: "#767671",
          "font-size": 10,
          "data-frame": frame,
        });
      t.textContent = label;
      this.svg.append(t);
    }
  }
  drawPositionDifferences() {
    for (const difference of jointPositionDifferences(this.geometry, this.target)) {
      const a = this.project(difference.measured), b = this.project(difference.target);
      const connector = node("line", {
        x1: a.x, y1: a.y, x2: b.x, y2: b.y,
        stroke: DIFFERENCE_COLOR,
        "stroke-width": 1.1,
        "stroke-dasharray": "3 4",
        "stroke-opacity": 0.8,
        "vector-effect": "non-scaling-stroke",
        class: "fk-position-difference",
        "data-joint": difference.name,
        "data-distance-mm": difference.distance_mm,
      });
      const title = node("title");
      title.textContent = `${difference.name} · Position difference ${difference.distance_mm.toPrecision(3)} mm (3D)`;
      connector.append(title);
      this.svg.append(connector);
    }
  }
  draw() {
    this.svg.replaceChildren();
    const baseAxes = this.frameAxes(
      [0, 0, 0],
      [
        [1, 0, 0],
        [0, 1, 0],
        [0, 0, 1],
      ],
      0.09,
    );
    const eeAxes = this.geometry
      ? this.frameAxes(
          this.geometry.tcp.position_m,
          this.geometry.rotation_matrix,
          0.06,
        )
      : [];
    const points = [
      this.project([0, 0, 0]),
      ...[this.geometry, this.target]
        .filter(Boolean)
        .flatMap((g) =>
          g.segments.flatMap((s) => [
            this.project(s.start_m),
            this.project(s.end_m),
          ]),
        ),
      ...[...baseAxes, ...eeAxes].flatMap(({ origin, end }) => [
        this.project(origin),
        this.project(end),
      ]),
    ];
    const xs = points.map((p) => p.x),
      ys = points.map((p) => p.y);
    const minX = Math.min(...xs),
      maxX = Math.max(...xs),
      minY = Math.min(...ys),
      maxY = Math.max(...ys);
    const width = Math.max(130, maxX - minX + 55) / this.zoom,
      height = Math.max(130, maxY - minY + 40) / this.zoom;
    this.svg.setAttribute(
      "viewBox",
      `${(minX + maxX - width) / 2} ${(minY + maxY - height) / 2} ${width} ${height}`,
    );
    for (let k = -2; k <= 2; k++) {
      const d = k * 0.08;
      this.line([-0.18, d, 0], [0.18, d, 0], {
        stroke: "#e6e6e2",
        "stroke-width": 1,
      });
      this.line([d, -0.18, 0], [d, 0.18, 0], {
        stroke: "#e6e6e2",
        "stroke-width": 1,
      });
    }
    this.drawAxes(baseAxes, "base");
    // Difference lines sit behind the existing arm strokes and joint markers.
    this.drawPositionDifferences();
    const drawGeometry = (g, target) => {
      if (!g) return;
      const color = target ? "#a5a5a0" : "#191919";
      // Include the wrist-to-tool-tip segment as well as the gripper branch.
      for (const s of [...g.segments].sort(
        (a, b) =>
          this.project(a.start_m).depth +
          this.project(a.end_m).depth -
          (this.project(b.start_m).depth + this.project(b.end_m).depth),
      ))
        this.line(s.start_m, s.end_m, {
          stroke: color,
          "stroke-width": target ? 3 : 4,
          "stroke-dasharray": target ? "5 5" : "",
          "stroke-linecap": "round",
          "data-segment-to": s.to,
          "data-segment-role": s.role || "link",
        });
      for (const j of g.joints) {
        const p = this.project(j.position_m),
          issue = !target && this.issues.states.get(j.name),
          c = node("circle", {
            cx: p.x,
            cy: p.y,
            r: target ? 3.5 : 5.5,
            fill: issue ? "#d9231b" : color,
            "data-joint": j.name,
            "data-issue": issue?.state || "none",
          });
        const title = node("title");
        title.textContent =
          j.name + (issue ? " · " + (issue.reason || issue.state) : "");
        c.append(title);
        this.svg.append(c);
      }
    };
    drawGeometry(this.target, true);
    drawGeometry(this.geometry, false);
    this.drawAxes(eeAxes, "ee");
    this.element.querySelector(".fk-empty").hidden = !!this.geometry;
    const g = this.geometry,
      values = g?.position_m;
    this.element.querySelector(".fk-readout").textContent = values
      ? `X ${(values[0] * 1000).toFixed(0)}   Y ${(values[1] * 1000).toFixed(0)}   Z ${(values[2] * 1000).toFixed(0)} mm`
      : "X —   Y —   Z — mm";
    const issues = [...this.issues.states].map(
      ([name, v]) => `${name}: ${v.reason || v.state}`,
    );
    const status = this.element.querySelector(".fk-status");
    status.textContent = issues.length
      ? issues.join(" · ") + (this.stale ? " · awaiting fresh status" : "")
      : this.stale
        ? "Pose / health status is stale"
        : "Drag to orbit · scroll to zoom";
    status.title = status.textContent;
  }
}
