// Independent, data-driven ROS graph renderer. No robot names or hardware roles.
const $ = (id) => document.getElementById(id),
  NS = "http://www.w3.org/2000/svg";
const svg = (tag, attrs = {}) => {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
  return el;
};
const html = (tag, className, text) => {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
};
const label = (
  group,
  x,
  y,
  text,
  size = 12,
  fill = "#191919",
  weight = 400,
) => {
  const el = svg("text", {
    x,
    y,
    fill,
    "font-size": size,
    "font-weight": weight,
  });
  el.textContent = text;
  group.append(el);
  return el;
};
const localName = (name) => name.split("/").filter(Boolean).pop() || name;
const compactName = (name) => {
  const parts = name.split("/").filter(Boolean);
  while (parts.join("/").length > 46 && parts.length > 1) parts.shift();
  return parts.join("/");
};
const compactType = (types) =>
  (types || []).map((t) => t.split("/").pop()).join(" / ") ||
  "Type unavailable";
const infrastructure = (name) =>
  /(^|\/)(parameter_events|rosout)$|\/_action\/|\/(describe_parameters|get_parameter_types|get_parameters|list_parameters|set_parameters|set_parameters_atomically|get_type_description)$/.test(
    name,
  );
const rate = (t) =>
  t && t.rate_hz !== null && t.rate_hz !== undefined
    ? `${Number(t.rate_hz).toFixed(1)} Hz`
    : (t?.state || "not_sampled").replaceAll("_", " ");
const palette = {
  publish: ["#d52b22", "#bb4c21", "#c76c26", "#a73a2d"],
  subscribe: ["#2359ba", "#327f9b", "#297987", "#5264a4"],
};
// A pulse summarizes a newly observed batch, never one animation per ROS message.
export const FLOW_LIMITS = Object.freeze({maxEdges:64,maxFps:30,pulseMs:1400,staleMs:4500});
const MESSAGE_FRESH_MS = 2500;
export class TopicFlowActivity {
  constructor() { this.samples = new Map(); }
  clear() { this.samples.clear(); }
  beginSample(id, sampleId) {
    if (typeof sampleId==='string' && sampleId) this.samples.set(id,{sampleId,messages:0,until:0});
  }
  observe(graph, now) {
    const fresh = ['ready','partial'].includes(graph.state) &&
      Number.isFinite(graph.age_ms) && graph.age_ms >= 0 && graph.age_ms < FLOW_LIMITS.staleMs;
    const seen = new Set(); let pulsed = false;
    for (const topic of graph.topics || []) {
      seen.add(topic.id);
      const t = topic.traffic || {}, old = this.samples.get(topic.id);
      const valid = t.state === 'sampling' && typeof t.sample_id === 'string' && t.sample_id &&
        Number.isSafeInteger(t.messages) && t.messages >= 0;
      if (!valid) { this.samples.delete(topic.id); continue; }
      const current = {sampleId:t.sample_id,messages:t.messages,start:old?.start,until:old?.until || 0};
      const same = old?.sampleId === t.sample_id;
      const recent = Number.isFinite(t.last_message_age_ms) && t.last_message_age_ms >= 0 &&
        t.last_message_age_ms < MESSAGE_FRESH_MS;
      if (!same || !fresh || !recent || t.messages < old.messages || t.messages === 0) current.until = 0;
      else if (t.messages > old.messages) {
        current.start = now;
        current.until = now + Math.min(FLOW_LIMITS.pulseMs,
          FLOW_LIMITS.staleMs - graph.age_ms, MESSAGE_FRESH_MS - t.last_message_age_ms);
        pulsed = true;
      }
      else current.until=Math.min(current.until,now+FLOW_LIMITS.staleMs-graph.age_ms,
        now+MESSAGE_FRESH_MS-t.last_message_age_ms);
      // Even a stale observation advances the watermark: it must not replay later.
      this.samples.set(topic.id,current);
    }
    for (const id of this.samples.keys()) if (!seen.has(id)) this.samples.delete(id);
    return pulsed;
  }
  active(id, now) { const s=this.samples.get(id); return Boolean(s && now >= s.start && now < s.until); }
  progress(id, now) { return this.active(id,now) ? (now-this.samples.get(id).start)/FLOW_LIMITS.pulseMs : null; }
}
export function cubicPoint(points, t) {
  const u=1-t, weights=[u*u*u,3*u*u*t,3*u*t*t,t*t*t];
  return [0,1].map(axis=>points.reduce((sum,p,i)=>sum+p[axis]*weights[i],0));
}
export function flowEdges(graph, visibleEdges, activity, now, {focus=null}={}) {
  const publishers=new Map();
  for (const e of graph.edges) if (e.kind==='publish') {
    if (!publishers.has(e.to)) publishers.set(e.to,new Set());
    publishers.get(e.to).add(e.endpoint_id || e.id);
  }
  return visibleEdges.flatMap(e=>{
    if (!['publish','subscribe'].includes(e.kind)) return [];
    const topicId=e.kind==='publish'?e.to:e.from;
    if (graph.entities.get(topicId)?.kind!=='topic' || !activity.active(topicId,now)) return [];
    if (e.kind==='publish' && publishers.get(topicId)?.size!==1) return [];
    return [{...e,topicId,hollow:e.kind==='subscribe'}];
  }).sort((a,b)=>Number(b.from===focus||b.to===focus)-Number(a.from===focus||a.to===focus)||a.id.localeCompare(b.id))
    .slice(0,FLOW_LIMITS.maxEdges);
}
function pieces(value, max = 27) {
  const result = [];
  let rest = value;
  while (rest.length > max && result.length < 2) {
    let cut = Math.max(rest.lastIndexOf("/", max), rest.lastIndexOf("_", max));
    if (cut < max / 2) cut = max;
    else cut++;
    result.push(rest.slice(0, cut));
    rest = rest.slice(cut);
  }
  result.push(rest.length > max ? rest.slice(0, max - 1) + "…" : rest);
  return result.slice(0, 3);
}
export function normalizeGraph(data) {
  if (data?.schema_version !== 1) throw Error("Unsupported ROS graph schema.");
  const nodes = (data.nodes || []).map((n) => ({ ...n, kind: "node" }));
  const topics = (data.topics || []).map((n) => ({ ...n, kind: "topic" }));
  const services = (data.services || []).map((n) => ({
    ...n,
    kind: "service",
  }));
  const actions = (data.actions || []).map((n) => ({ ...n, kind: "action" }));
  const entities = new Map(
    [...nodes, ...topics, ...services, ...actions].map((n) => [n.id, n]),
  );
  const edges = (data.edges || []).filter(
    (e) => entities.has(e.from) && entities.has(e.to),
  );
  return { ...data, nodes, topics, services, actions, edges, entities };
}
export function selectGraphView(
  d,
  { focus = null, scope = "all", search = "", showInfrastructure = false } = {},
) {
  if (!d) return { entities: [], edges: [] };
  search = search.trim().toLowerCase();
  const showInfra = showInfrastructure;
  let allowed = new Set(
    [...d.entities.values()]
      .filter((n) => showInfra || n.kind === "node" || !infrastructure(n.name))
      .map((n) => n.id),
  );
  if (scope === "focus" && focus) {
    const neighbours = new Set([focus]);
    for (const edge of d.edges)
      if (edge.from === focus || edge.to === focus) {
        neighbours.add(edge.from);
        neighbours.add(edge.to);
      }
    if (d.entities.get(focus)?.kind === "node") {
      const interfaces = new Set(neighbours);
      for (const edge of d.edges)
        if (
          (interfaces.has(edge.from) && edge.from !== focus) ||
          (interfaces.has(edge.to) && edge.to !== focus)
        ) {
          neighbours.add(edge.from);
          neighbours.add(edge.to);
        }
    }
    allowed = new Set([...allowed].filter((id) => neighbours.has(id)));
  }
  if (search) {
    const matches = new Set(
      [...d.entities.values()]
        .filter(
          (n) =>
            (n.fully_qualified_name || n.name).toLowerCase().includes(search) ||
            (n.types || []).some((t) => t.toLowerCase().includes(search)),
        )
        .map((n) => n.id),
    );
    const ids = new Set(matches);
    for (const e of d.edges)
      if (matches.has(e.from) || matches.has(e.to)) {
        ids.add(e.from);
        ids.add(e.to);
      }
    allowed = new Set([...allowed].filter((id) => ids.has(id)));
  }
  let edges = d.edges.filter((e) => allowed.has(e.from) && allowed.has(e.to));
  const used = new Set(edges.flatMap((e) => [e.from, e.to]));
  const entities = [...d.entities.values()].filter(
    (n) =>
      allowed.has(n.id) &&
      (used.has(n.id) ||
        n.id === focus ||
        n.kind !== "node" ||
        search ||
        scope === "all"),
  );
  return { entities, edges };
}
export class GraphExplorer {
  constructor({ request, onError }) {
    this.request = request;
    this.onError = onError;
    this.data = null;
    this.focus = null;
    this.isolatedId = null;
    this.scope = "all";
    this.savedOverview = null;
    this.visible = false;
    this.connected = false;
    this.interfaceKind = "topics";
    this.transform = { x: 0, y: 0, k: 1 };
    this.annotations = new Map();
    this.lastTopology = "";
    this.pending = false;
    this.disposed = false;
    this.didFit = false;
    this.flowActivity = new TopicFlowActivity();
    this.flowDots = [];
    this.flowFrame = null;
    this.flowLastFrame = -Infinity;
    this.watchTopic = null;
    this.watchPending = false;
    this.watchError = '';
    this.reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
    this.motionChanged = () => {
      if (this.reduceMotion.matches) this.stopFlow();
      else this.refresh();
    };
    this.reduceMotion.addEventListener('change',this.motionChanged);
    this.visibilityChanged = () => {
      this.flowActivity.clear(); this.stopFlow();
      if (!document.hidden && this.visible && this.connected) this.refresh();
    };
    document.addEventListener('visibilitychange',this.visibilityChanged);
    this.host = $("graph-canvas");
    this.svg = svg("svg", {
      "aria-label": "Discovered ROS communication graph",
      role: "img",
    });
    this.viewport = svg("g");
    this.svg.append(this.viewport);
    this.host.append(this.svg);
    this.bind();
    this.timer = setInterval(() => {
      if (this.visible && this.connected && !document.hidden) this.refresh();
    }, 2000);
    this.resizeObserver = new ResizeObserver(() => {
      if (!this.didFit && this.data) this.fit();
    });
    this.resizeObserver.observe(this.host);
  }
  setConnected(value) {
    if (this.disposed) return;
    this.connected = value;
    if (!value) { this.watchTopic=null; this.flowActivity.clear(); this.stopFlow(); }
    if (!value && this.data)
      $("graph-meta").textContent = "Disconnected · last discovery snapshot";
  }
  setVisible(value) {
    if (this.disposed) return;
    this.visible = value;
    if (value) requestAnimationFrame(() => this.fit());
    else { this.flowActivity.clear(); this.stopFlow(); }
  }
  stopFlow() {
    if (this.flowFrame!==null) cancelAnimationFrame(this.flowFrame);
    this.flowFrame=null;
    for (const dot of this.flowDots) dot.element.remove();
    this.flowDots=[];
  }
  animateFlow() {
    if (this.flowFrame!==null || !this.flowDots.length || !this.visible || !this.connected ||
        document.hidden || this.reduceMotion.matches) return;
    const tick = now => {
      this.flowFrame=null;
      if (!this.visible || !this.connected || document.hidden || this.reduceMotion.matches) { this.stopFlow(); return; }
      if (now-this.flowLastFrame >= 1000/FLOW_LIMITS.maxFps) {
        this.flowLastFrame=now; let active=false;
        for (const dot of this.flowDots) {
          const phase=this.flowActivity.progress(dot.topicId,now);
          if (phase===null) { dot.element.setAttribute('opacity','0'); continue; }
          active=true;
          // Show publication first, then illustrative subscriber routes.
          const t=dot.hollow?(phase-.5)*2:phase*2;
          if (t<0 || t>1) { dot.element.setAttribute('opacity','0'); continue; }
          const [x,y]=cubicPoint(dot.points,t);
          dot.element.setAttribute('transform',`translate(${x},${y})`);
          dot.element.setAttribute('opacity','1');
        }
        if (!active) { this.stopFlow(); return; }
      }
      this.flowFrame=requestAnimationFrame(tick);
    };
    this.flowFrame=requestAnimationFrame(tick);
  }
  async watchSelected({retry=false}={}) {
    const topic=this.selected(), target=this.watchTopic;
    if (this.disposed || !target || target!==topic?.id || topic.kind!=='topic' || this.watchPending ||
        !this.visible || !this.connected || document.hidden) return;
    if (!['ready','partial'].includes(this.data.state) || !Number.isFinite(this.data.age_ms) ||
        this.data.age_ms<0 || this.data.age_ms>=FLOW_LIMITS.staleMs) return;
    const t=topic.traffic || {};
    // Reuse an observation reported active. The topic-scoped API does not reserve
    // ownership across clients; never issue an automatic Stop on its behalf.
    if (t.state==='sampling') return;
    if (this.data.sampling?.enabled===false || (!retry && (['blocked','unavailable'].includes(t.state) ||
        /budget|exceeds/i.test(t.reason||'')))) {
      this.watchTopic=null;
      this.watchError=t.reason || 'Traffic sampling is unavailable.';
      this.inspector(); return;
    }
    const active=this.data.topics.filter(n=>n.traffic?.state==='sampling').length;
    if (active+(this.data.sampling?.cleanup_pending||0) >= (this.data.sampling?.max_topics||2)) return;
    this.watchPending=true;
    try {
      const result=await this.request('/api/v1/ros/sample',{
        topic:topic.name,duration_s:Math.min(5,this.data.sampling?.max_duration_s||5),
      });
      if (this.watchTopic===target && result.accepted===false) {
        this.watchTopic=null; this.watchError=result.reason || 'Traffic sampling is unavailable.';
      }
      else if (this.watchTopic===target && result.accepted===true && this.visible && !document.hidden) {
        this.flowActivity.beginSample(topic.id,result.sample_id);
      }
    } catch(error) {
      if (this.watchTopic===target) { this.watchTopic=null; this.watchError=error.message; }
    } finally {
      this.watchPending=false;
      if (this.visible && !document.hidden) this.inspector();
    }
  }
  dispose() {
    this.disposed=true; this.visible=false; this.connected=false;
    clearInterval(this.timer); this.stopFlow(); this.flowActivity.clear(); this.watchTopic=null;
    document.removeEventListener('visibilitychange',this.visibilityChanged);
    this.reduceMotion.removeEventListener('change',this.motionChanged);
    this.resizeObserver.disconnect();
  }
  async refresh() {
    if (this.disposed || this.pending || !this.connected) return;
    this.pending = true;
    try {
      const data = normalizeGraph(await this.request("/api/v1/ros/graph"));
      if (this.disposed || !this.connected) return;
      this.data = data;
      if (this.visible && !document.hidden) this.flowActivity.observe(data,performance.now());
      else this.flowActivity.clear();
      if (this.focus && !data.entities.has(this.focus)) { this.focus = null; this.watchTopic=null; }
      if (!this.focus && !this.initialized) {
        const topics = data.topics.filter((t) => !infrastructure(t.name));
        const score = (t) =>
          (t.publishers?.length === 1 ? 12 : 0) +
          (t.subscriptions?.length || 0) -
          Math.max(0, (t.publishers?.length || 0) - 2) * 3;
        topics.sort((a, b) => score(b) - score(a));
        this.focus = topics[0]?.id || data.nodes[0]?.id;
        this.initialized = true;
      }
      this.render();
      this.inspector();
      void this.watchSelected();
    } catch (e) {
      this.flowActivity.clear(); this.stopFlow();
      if (!this.data) this.message("ROS discovery unavailable. " + e.message);
      $("graph-meta").textContent = "Discovery unavailable · " + e.message;
    } finally {
      this.pending = false;
    }
  }
  bind() {
    $("refresh-graph").onclick = () => this.refresh();
    $("graph-search").oninput = () => {
      this.didFit = false;
      this.render();
    };
    $("show-infrastructure").onchange = () => {
      this.didFit = false;
      this.render();
    };
    $("graph-clear").onclick = () => {
      this.watchTopic=null; this.watchError='';
      this.focus = null;
      this.render();
      this.inspector();
    };
    $("graph-exit").onclick = () => this.exitFocus();
    $("graph-fit").onclick = () => this.fit();
    $("graph-zoom-in").onclick = () => this.zoom(1.25);
    $("graph-zoom-out").onclick = () => this.zoom(0.8);
    for (const b of document.querySelectorAll("[data-interface]"))
      b.onclick = () => {
        this.interfaceKind = b.dataset.interface;
        document.querySelectorAll("[data-interface]").forEach((x) => {
          x.classList.toggle("active", x === b);
          x.setAttribute("aria-selected", String(x === b));
        });
        this.list();
      };
    let drag = null;
    this.svg.addEventListener("pointerdown", (e) => {
      if (e.target.closest(".graph-object")) return;
      drag = {
        x: e.clientX,
        y: e.clientY,
        tx: this.transform.x,
        ty: this.transform.y,
      };
      this.svg.setPointerCapture(e.pointerId);
    });
    this.svg.addEventListener("pointermove", (e) => {
      if (!drag) return;
      this.transform.x = drag.tx + e.clientX - drag.x;
      this.transform.y = drag.ty + e.clientY - drag.y;
      this.applyTransform();
    });
    this.svg.addEventListener("pointerup", () => (drag = null));
    this.svg.addEventListener("pointercancel", () => (drag = null));
    this.svg.addEventListener(
      "wheel",
      (e) => {
        e.preventDefault();
        const r = this.svg.getBoundingClientRect();
        this.zoom(
          e.deltaY > 0 ? 0.9 : 1.1,
          e.clientX - r.left,
          e.clientY - r.top,
        );
      },
      { passive: false },
    );
    this.host.onkeydown = (e) => {
      if (e.target !== this.host) return;
      if (e.key === "+" || e.key === "=") this.zoom(1.2);
      if (e.key === "-") this.zoom(0.8);
      if (e.key === "0") this.fit();
      if (e.key === "Escape" && this.scope === "focus") this.exitFocus();
    };
  }
  message(text) {
    this.stopFlow();
    this.viewport.replaceChildren();
    let el = this.host.querySelector(".graph-message");
    if (!el) {
      el = html("div", "graph-message");
      this.host.append(el);
    }
    el.textContent = text;
  }
  selected() {
    return this.data?.entities.get(this.focus);
  }
  select(id) {
    if (id!==this.focus) { this.watchTopic=null; this.watchError=''; }
    this.focus = id;
    this.render();
    this.inspector();
  }
  activate(id) {
    const now = performance.now();
    if (this.lastClick?.id === id && now - this.lastClick.at < 450) {
      this.lastClick = null;
      this.isolate(id);
    } else {
      this.lastClick = { id, at: now };
      this.select(id);
    }
  }
  isolate(id) {
    if (id!==this.focus) { this.watchTopic=null; this.watchError=''; }
    if (this.scope === "all") this.savedOverview = { ...this.transform };
    this.scope = "focus";
    this.isolatedId = id;
    this.focus = id;
    this.didFit = false;
    $("graph-exit").hidden = false;
    this.render();
    this.inspector();
  }
  exitFocus() {
    const saved = this.savedOverview;
    this.scope = "all";
    this.isolatedId = null;
    this.didFit = false;
    this.render();
    if (saved) {
      this.transform = saved;
      this.didFit = true;
      this.applyTransform();
    }
    this.savedOverview = null;
    $("graph-exit").hidden = true;
    this.inspector();
  }
  visibleData() {
    return selectGraphView(this.data, {
      focus: this.scope === "focus" ? this.isolatedId : this.focus,
      scope: this.scope,
      search: $("graph-search").value,
      showInfrastructure: $("show-infrastructure").checked,
    });
  }
  render() {
    if (!this.data) return;
    this.host.querySelector(".graph-message")?.remove();
    const d = this.data,
      { entities, edges } = this.visibleData();
    const focus = this.selected();
    $("graph-focus").textContent =
      (this.scope === "focus" ? "Focused view · " : "Selected: ") +
      (focus?.name || "none");
    $("graph-counts").textContent =
      `${d.nodes.filter((n) => n.discovery_state !== "unresolved_endpoint_owner").length} nodes${d.nodes.some((n) => n.discovery_state === "unresolved_endpoint_owner") ? ` · ${d.nodes.filter((n) => n.discovery_state === "unresolved_endpoint_owner").length} unresolved owners` : ""} · ${d.topics.length} topics · ${d.actions.length} actions · ${d.services.length} services · ${entities.length} objects shown`;
    $("graph-meta").textContent =
      (Object.values(d.coverage || {}).includes("partial")
        ? "Partial discovery"
        : "Discovery snapshot") +
      " · " +
      (d.age_ms === undefined ? "" : Math.round(d.age_ms) + " ms old · ") +
      "read only";
    if (!entities.length) {
      this.message("No objects match the current filters.");
      this.list();
      return;
    }
    const nodes = entities.filter((n) => n.kind === "node"),
      mid = entities.filter((n) => n.kind !== "node");
    const left = [],
      right = [];
    for (const n of nodes) {
      const outgoing = edges.filter((e) => e.from === n.id).length,
        incoming = edges.filter((e) => e.to === n.id).length;
      (outgoing >= incoming ? left : right).push(n);
    }
    const sort = (a, b) => a.name.localeCompare(b.name);
    left.sort(sort);
    right.sort(sort);
    mid.sort((a, b) => a.kind.localeCompare(b.kind) || sort(a, b));
    const positions = new Map(),
      height = Math.max(
        260,
        left.length * 100,
        right.length * 100,
        mid.length * 96,
      );
    for (const [list, x, w] of [
      [left, 24, 230],
      [mid, 358, 238],
      [right, 700, 230],
    ])
      list.forEach((n, i) =>
        positions.set(n.id, {
          x,
          y:
            34 +
            (height - list.length * (n.kind === "node" ? 100 : 96)) / 2 +
            i * (n.kind === "node" ? 100 : 96),
          w,
          h: n.kind === "node" ? 88 : 82,
        }),
      );
    this.bounds = { width: 954, height: height + 64 };
    this.stopFlow();
    this.viewport.replaceChildren();
    const defs = svg("defs");
    this.viewport.append(defs);
    const related = new Set([this.focus]);
    for (const e of d.edges)
      if (e.from === this.focus || e.to === this.focus) {
        related.add(e.from);
        related.add(e.to);
      }
    const publisherNodes = new Set();
    if (focus?.kind === "topic")
      for (const e of d.edges)
        if (e.to === this.focus && e.kind === "publish")
          publisherNodes.add(e.from);
        else {
        }
    if (focus?.kind === "node")
      for (const e of d.edges)
        if (e.to === this.focus && e.kind === "subscribe") {
          for (const pub of d.edges)
            if (pub.to === e.from && pub.kind === "publish")
              publisherNodes.add(pub.from);
        }
    const connectedEdges = edges.filter(
      (e) => e.from === this.focus || e.to === this.focus,
    );
    const highlighted = new Set(connectedEdges.map((e) => e.id));
    const perObject = new Map();
    for (const e of edges)
      for (const id of [e.from, e.to]) {
        if (!perObject.has(id)) perObject.set(id, []);
        perObject.get(id).push(e.id);
      }
    for (const ids of perObject.values()) ids.sort();
    const ordered = [...edges].sort(
      (a, b) => Number(highlighted.has(a.id)) - Number(highlighted.has(b.id)),
    );
    const uniqueKinds = new Map();
    const flowPaths = new Map();
    for (const e of ordered) {
      const a = positions.get(e.from),
        b = positions.get(e.to);
      if (!a || !b) continue;
      const isFocus = highlighted.has(e.id),
        kind =
          e.kind === "publish"
            ? "publish"
            : e.kind === "subscribe"
              ? "subscribe"
              : "api";
      const nodeId = e.kind === "publish" ? e.from : e.to;
      const nodeIndex = d.nodes.findIndex((n) => n.id === nodeId);
      const color = isFocus
        ? palette[kind]?.[Math.max(0, nodeIndex) % 4] || "#8a8a84"
        : "#dcdcd8";
      const markerId = "arrow-" + color.replace("#", "");
      if (!uniqueKinds.has(markerId)) {
        const marker = svg("marker", {
          id: markerId,
          viewBox: "0 0 7 6",
          refX: 6.5,
          refY: 3,
          markerWidth: 6,
          markerHeight: 6,
          orient: "auto",
        });
        marker.append(svg("path", { d: "M0 0 L7 3 L0 6 Z", fill: color }));
        defs.append(marker);
        uniqueKinds.set(markerId, true);
      }
      const fromRight = a.x < b.x;
      const ax = fromRight ? a.x + a.w : a.x,
        bx = fromRight ? b.x : b.x + b.w;
      const port = (id, box) =>
        box.y +
        12 +
        ((perObject.get(id).indexOf(e.id) + 1) /
          (perObject.get(id).length + 1)) *
          (box.h - 24);
      const ay = port(e.from, a),
        by = port(e.to, b),
        dx = Math.max(45, Math.abs(bx - ax) * 0.48),
        sign = fromRight ? 1 : -1;
      const path = `M ${ax} ${ay} C ${ax + dx * sign} ${ay}, ${bx - dx * sign} ${by}, ${bx} ${by}`;
      flowPaths.set(e.id,{color,points:[[ax,ay],[ax+dx*sign,ay],[bx-dx*sign,by],[bx,by]]});
      this.viewport.append(
        svg("path", {
          d: path,
          fill: "none",
          stroke: "#fff",
          "stroke-width": 4.2,
        }),
      );
      this.viewport.append(
        svg("path", {
          d: path,
          fill: "none",
          stroke: color,
          "stroke-width": isFocus ? 1.55 : 1,
          "stroke-dasharray": kind === "api" ? "5 4" : "none",
          "marker-end": `url(#${markerId})`,
          "data-edge-id": e.id,
        }),
      );
    }
    const flowLayer=svg('g',{'aria-hidden':'true',class:'graph-flow-layer','pointer-events':'none'});
    this.viewport.append(flowLayer);
    if (!this.reduceMotion.matches) for (const edge of flowEdges(d,edges,this.flowActivity,performance.now(),{focus:this.focus})) {
      const route=flowPaths.get(edge.id); if (!route) continue;
      const dot=svg('circle',{r:3.1,fill:edge.hollow?'#fff':route.color,stroke:route.color,
        'stroke-width':1.4,'vector-effect':'non-scaling-stroke',opacity:0,
        'data-flow-edge':edge.id,'data-flow-kind':edge.kind});
      flowLayer.append(dot);
      this.flowDots.push({...edge,element:dot,points:route.points});
    }
    for (const n of entities) {
      const p = positions.get(n.id),
        selected = n.id === this.focus,
        publisher = publisherNodes.has(n.id) && !selected;
      const fill = selected
          ? "#191919"
          : publisher
            ? "#d52b22"
            : n.kind === "topic"
              ? "#f1f1ee"
              : "#fff",
        ink = selected || publisher ? "#fff" : "#191919",
        muted = selected || publisher ? "#e4e4df" : "#62625d";
      const g = svg("g", {
        class: "graph-object",
        tabindex: 0,
        role: "button",
        "aria-label": n.kind + " " + n.name,
        "aria-pressed": selected,
        "data-object-id": n.id,
        transform: `translate(${p.x},${p.y})`,
      });
      g.append(
        svg("rect", {
          width: p.w,
          height: p.h,
          fill,
          stroke:
            selected || publisher
              ? fill
              : n.kind === "topic"
                ? "#dededa"
                : "#777",
          "stroke-width": 0.75,
          "stroke-dasharray": ["service", "action"].includes(n.kind)
            ? "5 3"
            : "none",
        }),
      );
      if (n.kind === "node")
        g.append(
          svg("line", {
            x1: 0,
            x2: p.w,
            y1: 1,
            y2: 1,
            stroke: ink,
            "stroke-width": 2,
          }),
        );
      const lines = pieces(
        n.kind === "node" ? n.name : compactName(n.name),
        28,
      );
      lines
        .slice(0, 2)
        .forEach((line, i) =>
          label(
            g,
            10,
            21 + i * 14,
            line,
            15,
            ink,
            n.kind === "node" ? 400 : 700,
          ),
        );
      const y = lines.length > 1 ? 50 : 39;
      if (n.kind === "node") {
        label(g, 10, y, n.namespace ?? "Owner unresolved", 12, muted);
        const pubs = d.edges.filter(
            (e) => e.from === n.id && e.kind === "publish",
          ).length,
          subs = d.edges.filter(
            (e) => e.to === n.id && e.kind === "subscribe",
          ).length;
        label(
          g,
          10,
          p.h - 12,
          this.annotations.get(n.id) ||
            `${pubs} publications · ${subs} subscriptions`,
          11,
          muted,
        );
      } else {
        label(
          g,
          10,
          y,
          (n.kind === "topic" ? "" : n.kind + " · ") + compactType(n.types),
          12,
          muted,
        );
        if (n.kind === "topic") {
          label(g, 10, p.h - 10, rate(n.traffic), 11, muted);
          const history = n.traffic?.history || [];
          if (history.length) {
            const values = history.map((b) => b.messages || 0),
              max = Math.max(1, ...values);
            const points = values
              .map(
                (v, i) =>
                  `${p.w - 80 + (i / Math.max(1, values.length - 1)) * 68},${p.h - 10 - (v / max) * 15}`,
              )
              .join(" ");
            g.append(
              svg("polyline", {
                points,
                fill: "none",
                stroke: ink,
                "stroke-width": 1.3,
              }),
            );
          } else
            g.append(
              svg("line", {
                x1: p.w - 77,
                x2: p.w - 12,
                y1: p.h - 16,
                y2: p.h - 16,
                stroke: muted,
                "stroke-width": 1,
                "stroke-dasharray": "2 3",
              }),
            );
        } else
          label(
            g,
            10,
            p.h - 10,
            `${n.clients?.length || 0} clients · ${n.servers?.length || 0} servers`,
            9.5,
            muted,
          );
      }
      const title = svg("title");
      title.textContent =
        (n.fully_qualified_name || n.name) + "\n" + (n.types || []).join("\n");
      g.append(title);
      g.onclick = (e) => {
        e.stopPropagation();
        this.activate(n.id);
      };
      g.ondblclick = (e) => {
        e.stopPropagation();
        this.isolate(n.id);
      };
      g.onkeydown = (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          if (e.shiftKey) this.isolate(n.id);
          else this.select(n.id);
        }
      };
      this.viewport.append(g);
    }
    const topology = entities.map((n) => n.id).join("|");
    if (this.lastTopology !== topology) {
      this.didFit = false;
      this.lastTopology = topology;
    }
    this.applyTransform();
    if (!this.didFit && this.visible) this.fit();
    this.list();
    this.animateFlow();
  }
  fit() {
    if (!this.bounds || !this.host.clientWidth || !this.host.clientHeight)
      return;
    const w = this.host.clientWidth,
      h = this.host.clientHeight,
      k = Math.min(w / this.bounds.width, h / this.bounds.height) * 0.96;
    this.transform = {
      k,
      x: (w - this.bounds.width * k) / 2,
      y: (h - this.bounds.height * k) / 2,
    };
    this.didFit = true;
    this.applyTransform();
  }
  zoom(factor, x = this.host.clientWidth / 2, y = this.host.clientHeight / 2) {
    const old = this.transform.k,
      k = Math.max(0.12, Math.min(4, old * factor));
    this.transform.x = x - ((x - this.transform.x) * k) / old;
    this.transform.y = y - ((y - this.transform.y) * k) / old;
    this.transform.k = k;
    this.didFit = true;
    this.applyTransform();
  }
  applyTransform() {
    const { x, y, k } = this.transform;
    this.viewport.setAttribute("transform", `translate(${x},${y}) scale(${k})`);
    for (const dot of this.flowDots) dot.element.setAttribute('r',Math.min(9,3.1/k));
  }
  list() {
    if (!this.data) return;
    const all = this.data[this.interfaceKind] || [],
      search = $("graph-search").value.toLowerCase(),
      infra = $("show-infrastructure").checked;
    const rows = all.filter(
      (n) =>
        (infra || !infrastructure(n.name)) &&
        (!search ||
          n.name.toLowerCase().includes(search) ||
          (n.types || []).some((t) => t.toLowerCase().includes(search))),
    );
    $("interface-count").textContent =
      `${rows.length} / ${all.length} ${this.interfaceKind}`;
    const list = $("interface-list");
    list.replaceChildren();
    for (const n of rows) {
      const row = html(
        "button",
        "interface-row" + (this.focus === n.id ? " selected" : ""),
      );
      row.append(
        html("span", "", n.name),
        html("small", "", compactType(n.types)),
        html(
          "small",
          "",
          n.kind === "topic"
            ? rate(n.traffic)
            : `${n.clients?.length || 0} clients`,
        ),
      );
      row.title = (n.types || []).join(", ");
      row.onclick = () => this.activate(n.id);
      row.ondblclick = () => this.isolate(n.id);
      list.append(row);
    }
    if (!rows.length)
      list.append(html("div", "empty-state", "No matching interfaces."));
  }
  inspector() {
    const host = $("graph-inspector"),
      n = this.selected();
    if (!n) {
      host.replaceChildren(
        html(
          "div",
          "empty-state",
          "Select an object to inspect its relationships.",
        ),
      );
      return;
    }
    const active =
      host.contains(document.activeElement) &&
      document.activeElement.classList.contains("graph-annotation");
    if (active) return;
    host.replaceChildren();
    const heading = html("div", "inspector-heading");
    heading.append(
      html(
        "span",
        "eyebrow",
        n.discovery_state === "unresolved_endpoint_owner"
          ? "UNRESOLVED OWNER"
          : n.kind.toUpperCase(),
      ),
      html("h2", "", n.kind === "node" ? n.name : localName(n.name)),
      html("code", "", n.fully_qualified_name || n.name),
    );
    host.append(heading);
    const section = (title) => {
      const el = html("section", "inspector-section");
      el.append(html("h3", "", title));
      host.append(el);
      return el;
    };
    const row = (parent, k, v) => {
      const dl = parent.querySelector("dl") || parent.appendChild(html("dl"));
      const r = html("div");
      r.append(html("dt", "", k), html("dd", "", v));
      dl.append(r);
    };
    const interfaceSection = section(
      n.kind === "node" ? "Discovery" : "Interface",
    );
    if (n.types)
      interfaceSection.append(
        html("div", "inspector-type", n.types.join("\n")),
      );
    if (n.kind === "node") {
      row(interfaceSection, "Namespace", n.namespace ?? "Unknown");
      row(
        interfaceSection,
        "Discovered instances",
        n.instance_count == null ? "Unknown" : String(n.instance_count),
      );
    }
    const bindings = section("Bindings");
    const edges = this.data.edges.filter(
      (e) => e.from === n.id || e.to === n.id,
    );
    for (const e of edges) {
      const otherId = e.from === n.id ? e.to : e.from,
        other = this.data.entities.get(otherId);
      const el = html("div", "binding"),
        role =
          {
            publish: "PUB",
            subscribe: "SUB",
            service_client: "CLIENT",
            service_server: "SERVER",
            action_client: "CLIENT",
            action_server: "SERVER",
          }[e.kind] || e.kind;
      el.append(html("span", "role", role));
      const body = html("div"),
        button = html(
          "button",
          "",
          other?.fully_qualified_name || other?.name || otherId,
        );
      button.onclick = () => this.select(otherId);
      body.append(button);
      if (n.kind === "topic") {
        const endpoint = [
          ...(n.publishers || []),
          ...(n.subscriptions || []),
        ].find((p) => p.id === e.endpoint_id);
        const q = endpoint?.qos;
        body.append(
          html(
            "small",
            "",
            q
              ? [
                  q.reliability,
                  q.durability,
                  q.depth !== null && q.depth !== undefined
                    ? "depth " + q.depth
                    : "",
                ]
                  .filter(Boolean)
                  .join(" · ")
              : "QoS unavailable",
          ),
        );
      }
      el.append(body);
      bindings.append(el);
    }
    if (!edges.length)
      bindings.append(html("p", "muted", "No discovered bindings."));
    if (n.kind === "topic") {
      const traffic = section("Observed at collector"),
        t = n.traffic || {};
      traffic.append(html("div", "activity-value", rate(t)));
      row(
        traffic,
        "Sample state",
        (t.state || "not_sampled").replaceAll("_", " "),
      );
      row(
        traffic,
        "Messages",
        t.messages === null || t.messages === undefined
          ? "—"
          : String(t.messages),
      );
      row(
        traffic,
        "Serialized bytes",
        t.bytes === null || t.bytes === undefined
          ? "—"
          : Number(t.bytes).toLocaleString(),
      );
      if (t.reason) traffic.append(html("p", "inspector-note", t.reason));
      const button = html(
        "button",
        "sample-button",
        t.state === "sampling" ? "Stop sampling" : "Sample activity for 5 s",
      );
      button.disabled = this.data.sampling?.enabled === false || this.watchPending;
      button.onclick = async () => {
        this.watchTopic=null; this.watchError='';
        button.disabled = true;
        try {
          const result = await this.request(
            "/api/graph/sample",
            t.state === "sampling"
              ? { topic: n.name, stop: true }
              : { topic: n.name, duration_s: 5 },
          );
          if (result.accepted === false)
            throw Error(result.reason || "Sampling unavailable");
          await this.refresh();
        } catch (e) {
          this.onError(e);
        } finally {
          button.disabled = false;
        }
      };
      const watch=html('button','sample-button watch-button',this.watchTopic===n.id?'Stop live watch':'Watch live');
      watch.disabled=this.data.sampling?.enabled===false || this.watchPending;
      watch.onclick=async()=>{
        if (this.watchTopic===n.id) {
          this.watchTopic=null; this.watchError='';
          this.flowActivity.clear(); this.stopFlow(); this.inspector();
        } else {
          this.watchTopic=n.id; this.watchError='';
          await this.watchSelected({retry:true}); await this.refresh();
        }
      };
      const buttons=html('div','sampling-actions'); buttons.append(button,watch);
      traffic.append(
        buttons,
        html(
          "p",
          "inspector-note",
          "Dots summarize new topic activity. Filled dots trace a single discovered publisher; hollow dots show subscription routes, not confirmed receipt. Multiple publishers cannot be distinguished. Message contents are never exported.",
        ),
        html('p','inspector-note',this.watchError || (this.watchTopic===n.id?
          'Live watch renews a bounded 5 s sample while this topic is selected and the graph is visible.':
          'Watch live for repeated 5 s samples. Leaving this view or stopping a watch lets its current bounded sample finish.')),
      );
      if(this.reduceMotion.matches) traffic.append(html('p','inspector-note','Reduced motion: activity is shown as counters; moving dots are disabled.'));
    }
    if (n.kind === "node") {
      const note = section("Comment");
      const input = html("textarea", "graph-annotation");
      input.placeholder = "Add a note to this node";
      input.value = this.annotations.get(n.id) || "";
      input.maxLength = 70;
      input.oninput = () => {
        this.annotations.set(n.id, input.value);
        this.render();
      };
      note.append(
        input,
        html(
          "p",
          "inspector-note",
          "Local to this view. No changes to the ROS node.",
        ),
      );
    }
    const coverage = section("Coverage");
    coverage.append(
      html(
        "p",
        "inspector-note",
        "Observer-visible ROS domain. Filtered interfaces remain in discovery data. Crossings never join.",
      ),
    );
    if (this.data.errors?.length)
      coverage.append(
        html(
          "p",
          "inspector-note",
          this.data.errors
            .map((e) => e.message || e.reason || String(e))
            .join("; "),
        ),
      );
  }
}
