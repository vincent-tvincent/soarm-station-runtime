// A projected view of configured kinematics, following the original controller's SVG viewer.
const NS = "http://www.w3.org/2000/svg";
const node = (tag, attrs = {}) => {
  const n = document.createElementNS(NS, tag);
  for (const [key, value] of Object.entries(attrs)) n.setAttribute(key, value);
  return n;
};
export class JointIssueColors {
  constructor() {
    this.states = new Map();
  }
  update(motors = []) {
    for (const motor of motors) {
      const issue = motor.motion_issue;
      if (!issue) continue;
      if (issue.active === true && ["fault", "stall"].includes(issue.kind)) {
        this.states.set(motor.name, issue);
      } else {
        const previous = this.states.get(motor.name);
        if (
          previous &&
          issue.feedback_fresh &&
          issue.clear_kinds?.includes(previous.kind)
        )
          this.states.delete(motor.name);
      }
    }
    return this.states;
  }
  reset() {
    this.states.clear();
  }
}
export class FKView {
  constructor(element) {
    this.element = element;
    this.issues = new JointIssueColors();
    this.yaw = -0.68;
    this.pitch = 0.42;
    this.zoom = 1;
    element.innerHTML = `<div class="panel-subheading"><h3>Arm pose</h3><button type="button" class="text-button" data-reset>Reset view</button></div><div class="fk-stage" tabindex="0" aria-label="Arm pose. Drag to orbit, scroll to zoom, double-click to reset."><svg viewBox="0 0 420 300" role="img" aria-label="Measured robot joint positions"></svg><span class="fk-empty">Waiting for measured pose</span></div><div class="fk-legend"><span>— Measured</span><span class="muted">┄ Target</span><span class="fk-alert-key">● Fault / stall</span></div><div class="fk-readout"></div><p class="detail-note fk-status">Drag to orbit · scroll to zoom</p>`;
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
  resetArm() {
    this.geometry = null;
    this.target = null;
    this.issues.reset();
    this.draw();
  }
  update(geometry, target, motors, stale = false) {
    this.geometry = geometry;
    this.target = target;
    this.issues.update(motors);
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
      x: 210 + (cy * x - sy * y) * 650,
      y: 245 - (cp * z - sp * (sy * x + cy * y)) * 650,
      depth: sy * x + cy * y,
    };
  }
  line(a, b, attributes) {
    const p = this.project(a),
      q = this.project(b);
    this.svg.append(
      node("line", { x1: p.x, y1: p.y, x2: q.x, y2: q.y, ...attributes }),
    );
  }
  draw() {
    this.svg.replaceChildren();
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
    for (const [label, p] of [
      ["X", [0.09, 0, 0]],
      ["Y", [0, 0.09, 0]],
      ["Z", [0, 0, 0.09]],
    ]) {
      this.line([0, 0, 0], p, { stroke: "#aaa", "stroke-width": 1 });
      const q = this.project(p),
        t = node("text", {
          x: q.x + 5,
          y: q.y - 4,
          fill: "#767671",
          "font-size": 10,
        });
      t.textContent = label;
      this.svg.append(t);
    }
    const drawGeometry = (g, target) => {
      if (!g) return;
      const color = target ? "#a5a5a0" : "#191919";
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
    this.element.querySelector(".fk-empty").hidden = !!this.geometry;
    const g = this.geometry,
      values = g?.position_m;
    this.element.querySelector(".fk-readout").textContent = values
      ? `X ${(values[0] * 1000).toFixed(0)}   Y ${(values[1] * 1000).toFixed(0)}   Z ${(values[2] * 1000).toFixed(0)} mm`
      : "X —   Y —   Z — mm";
    const issues = [...this.issues.states].map(
      ([name, v]) => `${name}: ${v.reason || v.state}`,
    );
    this.element.querySelector(".fk-status").textContent = issues.length
      ? issues.join(" · ") + (this.stale ? " · awaiting fresh status" : "")
      : this.stale
        ? "Pose / health status is stale"
        : "Drag to orbit · scroll to zoom";
  }
}
