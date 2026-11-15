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
const internalInterface = n => n.transport_topic || infrastructure(n.name);
const displayName = n => n.display_name || n.name;
const activitySource = t => ({dashboard_subscription:'Existing dashboard subscription',
  transport_gid_projection:'Existing receipt · transport publisher GID',
  ros_subscription:'Graph observer'}[t.source] || (t.source?'Graph observer':'Not observed'));
const rate = (t) =>
  t && t.rate_hz !== null && t.rate_hz !== undefined
    ? `${Number(t.rate_hz).toFixed(1)} Hz`
    : ({blocked:'Not measured',inactive:'Monitor off',unavailable:'Measurement unavailable',
        limited:'Monitor capacity',throttled:'Monitor throttled',cleanup_pending:'Monitor cleanup',
        watching:'Rate not measured',sampling:'Measuring…',not_sampled:'Not measured'}[t?.state||'not_sampled']
      || (t.state||'not_sampled').replaceAll('_',' '));
const palette = {
  publish: ["#d52b22", "#bb4c21", "#c76c26", "#a73a2d"],
  subscribe: ["#2359ba", "#327f9b", "#297987", "#5264a4"],
};
// Motion is continuous while fresh counters advance; it never represents individual packets.
export const FLOW_LIMITS = Object.freeze({maxEdges:128,maxFps:30,cycleMs:1400,evidenceMs:3500,staleMs:4500});
export const GRAPH_UPDATE_INTERVALS = Object.freeze([500,1000,2000,5000,10000]);
export function normalizeUpdateInterval(value) {
  const parsed=Number(value);return GRAPH_UPDATE_INTERVALS.includes(parsed)?parsed:2000;
}
export function graphStructureSignature(graph) {
  const sorted=values=>values?.slice().sort((a,b)=>String(a.id??a).localeCompare(String(b.id??b)));
  const entity=n=>{
    const {activity,traffic,...metadata}=n;
    return {...metadata,types:sorted(metadata.types),publishers:sorted(metadata.publishers),
      subscriptions:sorted(metadata.subscriptions),clients:sorted(metadata.clients),servers:sorted(metadata.servers)};
  };
  return JSON.stringify({nodes:sorted(graph.nodes)?.map(entity),topics:sorted(graph.topics)?.map(entity),
    actions:sorted(graph.actions)?.map(entity),services:sorted(graph.services)?.map(entity),edges:sorted(graph.edges)});
}
function observationClock(graph) {
  const fields=['activity_clock_id','activity_clock_type','activity_captured_at_ns'];
  if(!fields.some(key=>Object.hasOwn(graph,key)))return {mode:'legacy',valid:true,id:null,ns:null};
  if(typeof graph.activity_clock_id==='string' && graph.activity_clock_id &&
      graph.activity_clock_type==='observer_monotonic' && typeof graph.activity_captured_at_ns==='string' &&
      /^\d{1,32}$/.test(graph.activity_captured_at_ns))
    return {mode:'server',valid:true,id:graph.activity_clock_id,ns:BigInt(graph.activity_captured_at_ns)};
  return {mode:'invalid',valid:false,id:null,ns:null};
}
const publisherKey = (id,gid) => `${id}::publisher:${gid}`;
const actionServerKey = (id,nodeId) => `${id}::server:${nodeId}`;
const unknownKey = id => `${id}::unknown`;
const liveMetric = n => n?.activity || n?.traffic || {};
const setText=(element,value)=>{if(element && element.textContent!==String(value))element.textContent=String(value);};
function historyPoints(entity,width,height) {
  const t=liveMetric(entity),history=t.history||(t.feedback?.history?.length?t.feedback.history:t.status?.history)||[];
  if(!history.length)return '';
  const values=history.map(b=>b.messages||0),maximum=Math.max(1,...values);
  return values.map((v,i)=>`${width-80+(i/Math.max(1,values.length-1))*68},${height-10-(v/maximum)*15}`).join(' ');
}
const observed = t => ['watching','sampling'].includes(t?.state);
const finiteAge = value => Number.isFinite(value) && value >= 0;
export function activityLabel(entity) {
  const t=liveMetric(entity);
  if (entity.kind==='action' && ['watching','partial'].includes(t.state)) {
    if (finiteAge(t.feedback?.last_message_age_ms) && t.feedback.last_message_age_ms<FLOW_LIMITS.evidenceMs) return 'Feedback active';
    const counts=t.status?.status_summary?.counts;
    if(counts) {
      const active=(counts.accepted||0)+(counts.executing||0)+(counts.canceling||0);
      return active ? `${active} goal${active===1?'':'s'} last reported active` : 'No active goals last reported';
    }
    return finiteAge(t.last_message_age_ms) && t.last_message_age_ms<FLOW_LIMITS.evidenceMs ? 'Status activity' : 'No recent response';
  }
  if (observed(t)) return finiteAge(t.last_message_age_ms) && t.last_message_age_ms<FLOW_LIMITS.evidenceMs && t.messages>0 ? 'Active' : 'No recent data';
  return ({inactive:'Monitoring off',blocked:'Not measured',limited:'Capacity limit',throttled:'Throttled',unavailable:'Unknown',cleanup_pending:'Cleanup pending'}[t.state] || 'Unknown');
}
export class TopicFlowActivity {
  constructor({updateIntervalMs=2000}={}) { this.samples = new Map();this.setUpdateInterval(updateIntervalMs); }
  setUpdateInterval(value) {
    this.updateIntervalMs=normalizeUpdateInterval(value);
    this.evidenceMs=Math.max(FLOW_LIMITS.evidenceMs,this.updateIntervalMs+1500);
    this.staleMs=Math.max(FLOW_LIMITS.staleMs,this.updateIntervalMs+2500);
  }
  clear() { this.samples.clear(); }
  observe(graph, now) {
    const clock=observationClock(graph);
    const fresh=clock.valid && ['ready','partial'].includes(graph.state) && finiteAge(graph.age_ms) && graph.age_ms<this.staleMs;
    const sources=new Map();
    for (const topic of graph.topics||[]) {
      const t=liveMetric(topic); sources.set(topic.id,t);
      if(typeof t.sample_id!=='string' || !t.sample_id) continue;
      for(const pub of t.publisher_counts||[]) sources.set(publisherKey(topic.id,pub.publisher_gid),{...t,...pub,sample_id:`${t.sample_id}:${pub.publisher_gid}`});
      if(Number.isSafeInteger(t.unknown_publisher_messages)) sources.set(unknownKey(topic.id),{...t,messages:t.unknown_publisher_messages,sample_id:`${t.sample_id}:unknown`});
    }
    for(const action of graph.actions||[]) {
      const a=action.activity||{}, parts=[a.status,a.feedback].filter(t=>observed(t) && typeof t.sample_id==='string' && t.sample_id);
      const counts=parts.map(t=>t.messages),ages=parts.map(t=>t.last_message_age_ms).filter(finiteAge);
      sources.set(action.id,{...a,state:['watching','partial'].includes(a.state)&&parts.length?'watching':a.state,
        messages:counts.length&&counts.every(n=>Number.isSafeInteger(n)&&n>=0)?counts.reduce((a,b)=>a+b,0):null,
        last_message_age_ms:ages.length?Math.min(...ages):null,
        sample_id:parts.length?JSON.stringify(parts.map(x=>x.sample_id)):null});
      for(const server of action.servers||[]) {
        const attributed=[];
        for(const name of [a.status_topic,a.feedback_topic]) {
          const topic=(graph.topics||[]).find(t=>t.name===name),t=liveMetric(topic);
          if(!observed(t)||typeof t.sample_id!=='string'||!t.sample_id)continue;
          const gids=new Set((topic.publishers||[]).filter(p=>p.node_id===server).map(p=>p.gid).filter(Boolean));
          for(const p of t.publisher_counts||[])if(gids.has(p.publisher_gid))attributed.push({...p,epoch:`${t.sample_id}:${p.publisher_gid}`});
        }
        attributed.sort((a,b)=>a.epoch.localeCompare(b.epoch));
        const ages=attributed.map(p=>p.last_message_age_ms).filter(finiteAge);
        if(attributed.length)sources.set(actionServerKey(action.id,server),{
          state:['watching','partial'].includes(a.state)?'watching':a.state,
          sample_id:JSON.stringify(attributed.map(p=>p.epoch)),
          messages:attributed.every(p=>Number.isSafeInteger(p.messages)&&p.messages>=0)?attributed.reduce((n,p)=>n+p.messages,0):null,
          last_message_age_ms:ages.length?Math.min(...ages):null,
        });
      }
    }
    let advanced=false;
    for(const [id,t] of sources) {
      const old=this.samples.get(id);
      if(!observed(t) || typeof t.sample_id!=='string' || !t.sample_id ||
          !Number.isSafeInteger(t.messages) || t.messages<0) {this.samples.delete(id);continue;}
      const comparable=old?.clockMode===clock.mode && old?.clockId===clock.id;
      const clockValid=clock.valid && (!comparable || clock.mode!=='server' || clock.ns>old.serverNs);
      const same=old?.sampleId===t.sample_id && comparable && old.clockValid && clockValid;
      const recent=finiteAge(t.last_message_age_ms) && t.last_message_age_ms<this.evidenceMs;
      const delta=same && t.messages>=old.messages?t.messages-old.messages:null;
      const current={sampleId:t.sample_id,messages:t.messages,start:old?.start,until:old?.until||0,
        observedAt:now,clockMode:clock.mode,clockId:clock.id,serverNs:clock.ns,clockValid,delta,
        intervalMs:delta===null?null:clock.mode==='server'?Number(clock.ns-old.serverNs)/1e6:Math.max(0,now-old.observedAt)};
      if(!same || !fresh || !recent || t.messages<old.messages || t.messages===0) current.until=0;
      else if(t.messages>old.messages) {
        if(!this.active(id,now)) current.start=now;
        current.until=now+Math.min(this.evidenceMs-t.last_message_age_ms,this.staleMs-graph.age_ms);
        advanced=true;
      } else current.until=Math.min(current.until,now+this.evidenceMs-t.last_message_age_ms,now+this.staleMs-graph.age_ms);
      this.samples.set(id,current);
    }
    for(const id of this.samples.keys()) if(!sources.has(id)) this.samples.delete(id);
    return advanced;
  }
  active(id,now) {const s=this.samples.get(id);return Boolean(s && now>=s.start && now<s.until);}
  progress(id,now) {return this.active(id,now)?((now-this.samples.get(id).start)%FLOW_LIMITS.cycleMs)/FLOW_LIMITS.cycleMs:null;}
  interval(id) {const s=this.samples.get(id);return s&&Number.isSafeInteger(s.delta)?{messages:s.delta,durationMs:s.intervalMs}:null;}
}
export function cubicPoint(points,t) {
  const u=1-t,weights=[u*u*u,3*u*u*t,3*u*t*t,t*t*t];
  return [0,1].map(axis=>points.reduce((sum,p,i)=>sum+p[axis]*weights[i],0));
}
// Expand through one interface, never recursively through a peer node's work.
// This keeps a selected subscriber's upstream publisher and its line consistent.
export function selectionPaths(graph,focus,visibleEdges=graph.edges) {
  const direct=visibleEdges.filter(e=>focus && (e.from===focus||e.to===focus));
  const highlighted=new Set(direct.map(e=>e.id));
  if(graph.entities.get(focus)?.kind==='node') {
    const opposite={publish:'subscribe',subscribe:'publish',action_client:'action_server',
      action_server:'action_client',service_client:'service_server',service_server:'service_client'};
    for(const e of direct) {
      const interfaceId=e.from===focus?e.to:e.from;
      for(const peer of visibleEdges) if(peer.kind===opposite[e.kind] &&
          (peer.from===interfaceId||peer.to===interfaceId))highlighted.add(peer.id);
    }
  }
  return {highlighted,publisherNodes:new Set(visibleEdges.filter(e=>
    e.kind==='publish' && highlighted.has(e.id)).map(e=>e.from))};
}
export function flowEdges(graph,visibleEdges,activity,now,{focus=null,focusedOnly=false}={}) {
  const publishers=new Map();
  for(const e of graph.edges) if(e.kind==='publish') {
    if(!publishers.has(e.to)) publishers.set(e.to,new Set());
    publishers.get(e.to).add(e.endpoint_id||e.id);
  }
  function publication(topic,nodeId,endpointId=null) {
    const endpoints=(topic.publishers||[]).filter(p=>endpointId?p.id===endpointId:p.node_id===nodeId);
    for(const endpoint of endpoints) if(endpoint.gid && activity.active(publisherKey(topic.id,endpoint.gid),now))
      return {sourceKey:publisherKey(topic.id,endpoint.gid),hollow:false,attribution:'publisher_gid'};
    // A lone discovered source can be illustrated without claiming GID attribution.
    const sole=graph.edges.find(e=>e.kind==='publish'&&e.to===topic.id);
    if(publishers.get(topic.id)?.size===1 && sole?.from===nodeId &&
        (!endpointId || sole.endpoint_id===endpointId) && activity.active(topic.id,now))
      return {sourceKey:topic.id,hollow:true,attribution:'single_discovered_publisher'};
    return null;
  }
  const selected=focusedOnly?selectionPaths(graph,focus,visibleEdges).highlighted:null;
  const result=visibleEdges.filter(e=>!focusedOnly || selected.has(e.id)).flatMap(e=>{
    if(e.kind==='publish'||e.kind==='subscribe') {
      const topicId=e.kind==='publish'?e.to:e.from,topic=graph.entities.get(topicId);
      if(topic?.kind!=='topic') return [];
      const source=e.kind==='publish'?publication(topic,e.from,e.endpoint_id):
        activity.active(topicId,now)?{sourceKey:topicId,hollow:true,attribution:'subscription_route'}:null;
      return source?[{...e,topicId,reverse:false,...source}]:[];
    }
    if(e.kind==='action_server'||e.kind==='action_client') {
      const id=e.kind==='action_server'?e.from:e.to,action=graph.entities.get(id),a=action?.activity;
      if(!a||!activity.active(id,now)) return [];
      if(e.kind==='action_client') return [{...e,topicId:id,sourceKey:id,reverse:true,hollow:true,attribution:'action_response_route'}];
      const serverKey=actionServerKey(id,e.to);
      if(activity.active(serverKey,now))return [{...e,topicId:id,sourceKey:serverKey,reverse:true,hollow:false,attribution:'action_server_gids'}];
      const topics=[a.feedback_topic,a.status_topic].map(name=>(graph.topics||[]).find(t=>t.name===name)).filter(Boolean);
      for(const topic of topics) {const source=publication(topic,e.to);if(source)return[{...e,topicId:id,reverse:true,...source}];}
      if((action.servers||[]).length===1) return [{...e,topicId:id,sourceKey:id,reverse:true,hollow:true,attribution:'single_discovered_server'}];
    }
    return [];
  });
  return result.sort((a,b)=>Number(b.from===focus||b.to===focus)-Number(a.from===focus||a.to===focus)||a.id.localeCompare(b.id)).slice(0,FLOW_LIMITS.maxEdges);
}
export class GraphLiveWatch {
  constructor({request,now=()=>performance.now(),clientId=crypto.randomUUID()}) {
    this.request=request;this.now=now;this.clientId=clientId;
    this.enabled=true;this.active=false;this.leaseId=null;this.expiresAt=0;this.renewAt=0;
    this.error='';this.pending=null;this.capability=null;this.generation=0;this.retryAt=0;
  }
  setEnabled(value) {if(this.enabled!==value){this.enabled=value;this.generation++;}if(!value)void this.sync();}
  setActive(value) {if(this.active!==value){this.active=value;this.generation++;}if(!value)void this.sync();}
  async release(id) {
    if(!id)return;
    try{await this.request('/api/v1/ros/watch',{operation:'release',lease_id:id});}catch{}
  }
  sync(capability=this.capability) {
    this.capability=capability;
    if(this.pending)return this.pending;
    this.pending=Promise.resolve().then(async()=>{
      const ready=this.enabled&&this.active&&this.capability?.enabled;
      if(!ready){const id=this.leaseId;this.leaseId=null;this.expiresAt=0;await this.release(id);return false;}
      const started=this.now();
      if(started<this.retryAt)return false;
      if(this.leaseId&&started<this.renewAt&&started<this.expiresAt)return true;
      if(started>=this.expiresAt)this.leaseId=null;
      const generation=this.generation,oldId=this.leaseId;
      try{
        const reply=await this.request('/api/v1/ros/watch',oldId?
          {operation:'renew',lease_id:oldId}:{operation:'acquire',client_id:this.clientId});
        if(generation!==this.generation||!this.enabled||!this.active){
          this.leaseId=null;await this.release(reply.lease_id||oldId);return false;
        }
        if(!reply.accepted||typeof reply.lease_id!=='string'||!Number.isFinite(reply.expires_in_ms)||reply.expires_in_ms<=0){
          this.leaseId=null;throw Error(reply.reason||reply.message||'Live watch was not acknowledged.');
        }
        this.leaseId=reply.lease_id;this.expiresAt=started+reply.expires_in_ms;
        this.renewAt=started+Math.min(4000,reply.expires_in_ms/2);
        this.error='';this.retryAt=0;return true;
      }catch(error){this.error=error.message;this.retryAt=this.now()+2000;return false;}
    }).finally(()=>{this.pending=null;});
    return this.pending;
  }
  dispose(){this.enabled=false;this.active=false;this.generation++;return this.sync();}
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
  const nodes = (data.nodes || []).map((n) => ({ ...n,
    ...(n.discovery_state==='unresolved_endpoint_owner'?{reported_name:n.name??null}:{}),
    name:n.name || (n.discovery_state==='unresolved_endpoint_owner'?'Unresolved owner':n.fully_qualified_name||n.id),kind: "node" }));
  const topics = (data.topics || []).map((n) => ({ ...n, kind: "topic" }));
  const topicByName=new Map(topics.map(t=>[t.name,t]));
  for(const topic of topics) {
    const baseName=topic.name.replace(/\/_buf_[a-zA-Z0-9_]+$/, '');
    const base=baseName!==topic.name?topicByName.get(baseName):null;
    topic.transport_topic=Boolean(base && topic.types?.length && base.types?.length &&
      JSON.stringify([...topic.types].sort())===JSON.stringify([...base.types].sort()));
  }
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
  for(const node of nodes) if(node.discovery_state==='unresolved_endpoint_owner') {
    const bindings=edges.filter(e=>e.from===node.id||e.to===node.id);
    node.transport_endpoint=bindings.length>0 && bindings.every(e=>
      entities.get(e.from===node.id?e.to:e.from)?.transport_topic);
    const pub=bindings.some(e=>e.kind==='publish'),sub=bindings.some(e=>e.kind==='subscribe');
    const role=pub&&!sub?'publisher':sub&&!pub?'subscriber':'endpoint';
    node.display_name=`${node.transport_endpoint?'Transport':'Unresolved'} ${role}`;
  }
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
      .filter((n) => showInfra || n.kind === "node" || !internalInterface(n))
      .map((n) => n.id),
  );
  // These placeholders describe endpoints, not independently discovered nodes.
  // Keep them only when an interface remains visible; named idle nodes stay.
  if(!showInfra)for(const n of d.nodes)if(n.discovery_state==='unresolved_endpoint_owner') {
    const bindings=d.edges.filter(e=>e.from===n.id||e.to===n.id);
    if(bindings.length && bindings.every(e=>!allowed.has(e.from===n.id?e.to:e.from)))allowed.delete(n.id);
  }
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
            displayName(n).toLowerCase().includes(search) ||
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
    let savedInterval;try{savedInterval=localStorage.getItem('ros-graph-update-ms');}catch{}
    this.updateIntervalMs=normalizeUpdateInterval(savedInterval);
    this.flowActivity = new TopicFlowActivity({updateIntervalMs:this.updateIntervalMs});
    this.flowDots = [];
    this.flowFrame = null;
    this.flowLastFrame = -Infinity;
    this.liveWatch = new GraphLiveWatch({request:this.request});
    this.reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
    this.motionChanged = () => {
      if (this.reduceMotion.matches) this.stopFlow();
      else this.refresh();
    };
    this.reduceMotion.addEventListener('change',this.motionChanged);
    this.visibilityChanged = () => {
      this.flowActivity.clear(); this.stopFlow(); this.updateWatchActivity();
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
    this.restartPolling();
    // Observation leases are independent of how often the display is refreshed.
    this.watchTimer=setInterval(()=>{
      if(!this.disposed && this.visible && this.connected && !document.hidden) void this.syncLiveWatch();
    },2000);
    this.resizeObserver = new ResizeObserver(() => {
      if (!this.didFit && this.data) this.fit();
    });
    this.resizeObserver.observe(this.host);
  }
  setConnected(value) {
    if (this.disposed) return;
    this.connected = value;
    if (!value) { this.flowActivity.clear(); this.stopFlow(); }
    this.updateWatchActivity();
    if (!value && this.data)
      $("graph-meta").textContent = "Disconnected · last discovery snapshot";
  }
  setVisible(value) {
    if (this.disposed) return;
    this.visible = value;
    if (value) requestAnimationFrame(() => this.fit());
    else { this.flowActivity.clear(); this.stopFlow(); }
    this.updateWatchActivity();
  }
  restartPolling() {
    if(this.disposed)return;
    clearInterval(this.timer);
    this.timer=setInterval(()=>{
      if(!this.disposed && this.visible && this.connected && !document.hidden) void this.refresh({scheduled:true});
    },this.updateIntervalMs);
  }
  setUpdateInterval(value) {
    if(this.disposed)return;
    this.updateIntervalMs=normalizeUpdateInterval(value);
    this.flowActivity.setUpdateInterval(this.updateIntervalMs);
    try{localStorage.setItem('ros-graph-update-ms',String(this.updateIntervalMs));}catch{}
    const select=$('graph-update-interval');if(select)select.value=String(this.updateIntervalMs);
    this.restartPolling();
    if(this.visible && this.connected && !document.hidden)void this.refresh();
  }
  updateWatchActivity() {
    this.liveWatch.setActive(!this.disposed && this.visible && this.connected && !document.hidden);
  }
  viewKey() {
    return JSON.stringify([this.focus,this.scope,this.isolatedId,$('graph-search')?.value||'',$('show-infrastructure')?.checked||false]);
  }
  updateMeasurements() {
    const data=this.data;
    setText($('graph-meta'),(Object.values(data.coverage||{}).includes('partial')?'Partial discovery':'Discovery snapshot')+
      ' · '+(data.age_ms===undefined?'':Math.round(data.age_ms)+' ms old · ')+'read only');
    for(const element of this.viewport.querySelectorAll('[data-live-rate]')) {
      const entity=data.entities.get(element.getAttribute('data-live-rate'));if(entity)setText(element,rate(liveMetric(entity)));
    }
    const baselines=new Map(Array.from(this.viewport.querySelectorAll('[data-live-baseline]'),e=>[e.getAttribute('data-live-baseline'),e]));
    for(const plot of this.viewport.querySelectorAll('[data-live-history]')) {
      const id=plot.getAttribute('data-live-history'),entity=data.entities.get(id);if(!entity)continue;
      const points=historyPoints(entity,Number(plot.getAttribute('data-plot-width')),Number(plot.getAttribute('data-plot-height')));
      if(plot.getAttribute('points')!==points)plot.setAttribute('points',points);
      plot.setAttribute('visibility',points?'visible':'hidden');baselines.get(id)?.setAttribute('visibility',points?'hidden':'visible');
    }
    for(const element of $('interface-list').querySelectorAll('[data-interface-rate]')) {
      const entity=data.entities.get(element.getAttribute('data-interface-rate'));
      if(entity && ['topic','action'].includes(entity.kind))setText(element,rate(liveMetric(entity)));
    }
    this.updateActivityInspector();this.syncFlowDots();this.renderLiveStatus();this.animateFlow();
  }
  updateActivityInspector() {
    const n=this.selected();if(!n || n.id!==this.inspectorEntityId || !['topic','action'].includes(n.kind))return;
    const host=$('graph-inspector'),t=liveMetric(n),interval=this.flowActivity.interval(n.id);
    const counts=t.status?.status_summary?.counts;
    const values={
      'Live state':activityLabel(n),'Monitor':t.state||'Unknown','Messages':t.messages==null?'—':String(t.messages),
      'Last update':interval?`${interval.messages} messages / ${(interval.durationMs/1000).toFixed(2)} s`:'Awaiting interval',
      'Serialized bytes':t.bytes==null?'Not measured':Number(t.bytes).toLocaleString(),
      'Last received':finiteAge(t.last_message_age_ms)?`${(t.last_message_age_ms/1000).toFixed(2)} s ago`:'No receipt observed',
      'Source':activitySource(t),
      'Status messages':t.status_messages==null?'—':String(t.status_messages),
      'Feedback messages':t.feedback_messages==null?'—':String(t.feedback_messages),
      'Last reported goals':counts?(Object.entries(counts).filter(([,v])=>v).map(([k,v])=>`${k}: ${v}`).join(' · ')||'None'):'Not observed',
    };
    setText(host.querySelector('.activity-value'),rate(t));
    for(const row of host.querySelectorAll('[data-graph-field]')) {
      const key=row.getAttribute('data-graph-field');if(Object.hasOwn(values,key))setText(row.querySelector('dd'),values[key]);
    }
    const reason=host.querySelector('[data-activity-reason]');if(reason){setText(reason,t.reason||'');reason.hidden=!t.reason;}
  }
  syncFlowDots() {
    if(!this.flowLayer)return;
    if(!this.liveWatch.enabled || this.reduceMotion.matches || !this.visible || !this.connected || document.hidden){this.stopFlow();return;}
    const old=new Map(this.flowDots.map(dot=>[dot.id,dot])),current=[];
    for(const edge of flowEdges(this.data,this.renderedEdges||[],this.flowActivity,performance.now(),{focus:this.focus,focusedOnly:true})) {
      const route=this.flowPaths.get(edge.id);if(!route)continue;
      let dot=old.get(edge.id);old.delete(edge.id);
      if(!dot) {
        const element=svg('g',{opacity:0,'data-flow-edge':edge.id,'data-flow-kind':edge.kind});
        const circle=svg('circle',{'stroke-width':1.4,'vector-effect':'non-scaling-stroke'});
        const count=svg('text',{'text-anchor':'middle','font-weight':600,stroke:'#fff','paint-order':'stroke',class:'graph-flow-count'});
        const title=svg('title');element.append(circle,count,title);this.flowLayer.append(element);
        dot={element,circle,count,title};
      }
      Object.assign(dot,edge,{points:route.points});
      // Marker fill expresses the route color; attribution stays in the tooltip.
      dot.circle.setAttribute('fill',route.color);dot.circle.setAttribute('stroke',route.color);
      dot.count.setAttribute('fill',route.color);
      const interval=this.flowActivity.interval(edge.sourceKey);
      setText(dot.count,interval===null?'':String(interval.messages));
      setText(dot.title,interval===null?'Observed activity':`${interval.messages} observed messages in the previous ${(interval.durationMs/1000).toFixed(2)} s observer update interval${edge.hollow?'; endpoint receipt is unverified':''}.`);
      this.sizeFlowDot(dot);current.push(dot);
    }
    for(const dot of old.values())dot.element.remove();
    this.flowDots=current;
  }
  sizeFlowDot(dot) {
    const k=this.transform.k,scale=Math.max(.25,k);
    dot.circle.setAttribute('r',Math.min(9,3.1/k));dot.count.setAttribute('font-size',String(11/scale));
    dot.count.setAttribute('y',String(-8/scale));dot.count.setAttribute('stroke-width',String(3/scale));
  }
  stopFlow() {
    if (this.flowFrame!==null) cancelAnimationFrame(this.flowFrame);
    this.flowFrame=null;
    for (const dot of this.flowDots) dot.element.remove();
    this.flowDots=[];
  }
  animateFlow() {
    if (this.flowFrame!==null || !this.flowDots.length || !this.liveWatch.enabled || !this.visible || !this.connected ||
        document.hidden || this.reduceMotion.matches) return;
    const tick = now => {
      this.flowFrame=null;
      if (!this.liveWatch.enabled || !this.visible || !this.connected || document.hidden || this.reduceMotion.matches) { this.stopFlow(); return; }
      if (now-this.flowLastFrame >= 1000/FLOW_LIMITS.maxFps) {
        this.flowLastFrame=now; let active=false;
        for (const dot of this.flowDots) {
          const phase=this.flowActivity.progress(dot.sourceKey,now);
          if (phase===null) { dot.element.setAttribute('opacity','0'); continue; }
          active=true;
          // All observed directions move concurrently, with a stable phase.
          const forward=(phase+(dot.hollow ? 0.45 : 0))%1;
          const t=dot.reverse?1-forward:forward;
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
  async syncLiveWatch() {
    this.updateWatchActivity();
    await this.liveWatch.sync(this.data?.watch);
    if(!this.disposed) this.renderLiveStatus();
  }
  renderLiveStatus() {
    const el=$("graph-flow-status");if(!el)return;
    $("graph-live-flow").checked=this.liveWatch.enabled;
    const watch=this.data?.watch;
    if(!this.liveWatch.enabled){el.textContent='Live monitoring paused';return;}
    if(!this.connected){el.textContent='Live monitoring paused · connection unavailable';return;}
    if(!watch?.enabled){el.textContent='Live monitoring unavailable on this backend';return;}
    if(this.liveWatch.error){el.textContent=this.liveWatch.error;return;}
    const coverage=watch.coverage||{};
    const observing=coverage.watching||0;
    const unknown=Object.entries(coverage).reduce((n,[key,value])=>n+(key==='watching'?0:Number(value)||0),0);
    el.textContent=watch.active?`Live · ${observing} topics observed${unknown?` · ${unknown} not measured`:''}${this.reduceMotion.matches?' · reduced motion':''}`:'Connecting live monitor…';
  }
  dispose() {
    this.disposed=true; this.visible=false; this.connected=false;
    clearInterval(this.timer); clearInterval(this.watchTimer); this.stopFlow(); this.flowActivity.clear(); void this.liveWatch.dispose();
    document.removeEventListener('visibilitychange',this.visibilityChanged);
    this.reduceMotion.removeEventListener('change',this.motionChanged);
    this.resizeObserver.disconnect();
  }
  async refresh({scheduled=false}={}) {
    if (this.disposed || this.pending || !this.connected) return;
    // An explicit refresh/resume establishes a new cadence instead of being
    // followed immediately by a nearly-due old timer tick.
    if(!scheduled)this.restartPolling?.();
    this.pending = true;
    try {
      const data = normalizeGraph(await this.request("/api/v1/ros/graph"));
      if (this.disposed || !this.connected) return;
      this.data = data;
      if (this.liveWatch.enabled && this.visible && !document.hidden) this.flowActivity.observe(data,performance.now());
      else this.flowActivity.clear();
      if (this.focus && !data.entities.has(this.focus)) { this.focus = null; }
      if (!this.focus && !this.initialized) {
        const topics = data.topics.filter((t) => !internalInterface(t));
        const score = (t) =>
          (t.publishers?.length === 1 ? 12 : 0) +
          (t.subscriptions?.length || 0) -
          Math.max(0, (t.publishers?.length || 0) - 2) * 3;
        topics.sort((a, b) => score(b) - score(a));
        this.focus = topics[0]?.id || data.nodes[0]?.id;
        this.initialized = true;
      }
      const signature=graphStructureSignature(data);
      if(signature===this.renderedSignature && this.viewKey()===this.renderedViewKey) {
        this.updateMeasurements();
      } else {
        this.render();this.inspector();
      }
      void this.syncLiveWatch();
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
    const interval=$('graph-update-interval');
    if(interval){interval.value=String(this.updateIntervalMs);interval.onchange=()=>this.setUpdateInterval(interval.value);}
    $("graph-live-flow").onchange = () => {
      this.liveWatch.setEnabled($("graph-live-flow").checked);
      this.flowActivity.clear();this.stopFlow();
      void this.syncLiveWatch();
      if(this.liveWatch.enabled) void this.refresh();
    };
    $("graph-search").oninput = () => {
      this.didFit = false;
      this.render();
    };
    $("show-infrastructure").onchange = () => {
      if(this.data && !$("show-infrastructure").checked) {
        const visible=new Set(selectGraphView(this.data).entities.map(n=>n.id));
        if(!visible.has(this.focus))this.focus=null;
        if(this.scope==='focus' && !visible.has(this.isolatedId)) {
          this.scope='all';this.isolatedId=null;this.savedOverview=null;$("graph-exit").hidden=true;
        }
      }
      this.didFit = false;
      this.render();
      this.inspector();
    };
    $("graph-clear").onclick = () => {
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
      (focus ? displayName(focus) : "none");
    const unresolved=d.nodes.filter(n=>n.discovery_state==='unresolved_endpoint_owner');
    const hiddenTransport=unresolved.filter(n=>n.transport_endpoint && !$("show-infrastructure").checked).length;
    $("graph-counts").textContent =
      `${d.nodes.length-unresolved.length} nodes${unresolved.length-hiddenTransport?` · ${unresolved.length-hiddenTransport} unresolved endpoints`:''}${hiddenTransport?` · ${hiddenTransport} transport endpoints hidden`:''} · ${d.topics.length} topics · ${d.actions.length} actions · ${d.services.length} services · ${entities.length} objects shown`;
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
    const nodeWidth = 230, topicWidth = 238, margin = 24,
      columnGap = Math.ceil(Math.max(nodeWidth, topicWidth) * 1.6),
      middleX = margin + nodeWidth + columnGap,
      rightX = middleX + topicWidth + columnGap;
    for (const [list, x, w] of [
      [left, margin, nodeWidth],
      [mid, middleX, topicWidth],
      [right, rightX, nodeWidth],
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
    this.bounds = { width: rightX + nodeWidth + margin, height: height + 64 };
    this.stopFlow();
    this.viewport.replaceChildren();
    const defs = svg("defs");
    this.viewport.append(defs);
    const {publisherNodes,highlighted}=selectionPaths(d,this.focus,edges);
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
          orient: "auto-start-reverse",
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
          ...(kind === "api" ? {"marker-start": `url(#${markerId})`} : {}),
          "data-edge-id": e.id,
        }),
      );
    }
    this.flowLayer=svg('g',{'aria-hidden':'true',class:'graph-flow-layer','pointer-events':'none'});
    this.viewport.append(this.flowLayer);
    this.flowPaths=flowPaths;this.renderedEdges=edges;
    this.syncFlowDots();
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
        "aria-label": n.kind + " " + displayName(n),
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
        n.kind === "node" ? displayName(n) : compactName(n.name),
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
        if (n.kind === "topic" || n.kind === "action") {
          const activity=liveMetric(n);
          label(g, 10, p.h - 10, rate(activity), 11, muted).setAttribute('data-live-rate',n.id);
          const plot=svg('polyline',{'data-live-history':n.id,'data-plot-width':p.w,'data-plot-height':p.h,
            points:historyPoints(n,p.w,p.h),fill:'none',stroke:ink,'stroke-width':1.3});
          const baseline=svg('line',{'data-live-baseline':n.id,x1:p.w-77,x2:p.w-12,y1:p.h-16,y2:p.h-16,
            stroke:muted,'stroke-width':1,'stroke-dasharray':'2 3'});
          plot.setAttribute('visibility',plot.getAttribute('points')?'visible':'hidden');
          baseline.setAttribute('visibility',plot.getAttribute('points')?'hidden':'visible');
          g.append(plot,baseline);
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
    this.renderedSignature=graphStructureSignature(this.data);this.renderedViewKey=this.viewKey();
    this.renderLiveStatus();
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
    for (const dot of this.flowDots) this.sizeFlowDot(dot);
  }
  list() {
    if (!this.data) return;
    const all = this.data[this.interfaceKind] || [],
      search = $("graph-search").value.toLowerCase(),
      infra = $("show-infrastructure").checked;
    const rows = all.filter(
      (n) =>
        (infra || !internalInterface(n)) &&
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
          (n.kind === "topic" || n.kind === "action")
            ? rate(liveMetric(n))
            : `${n.clients?.length || 0} clients`,
        ),
      );
      row.lastElementChild.setAttribute('data-interface-rate',n.id);
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
      this.inspectorEntityId=null;
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
    this.inspectorEntityId=n.id;
    const heading = html("div", "inspector-heading");
    heading.append(
      html(
        "span",
        "eyebrow",
        n.discovery_state === "unresolved_endpoint_owner"
          ? "UNRESOLVED OWNER"
          : n.kind.toUpperCase(),
      ),
      html("h2", "", n.kind === "node" ? displayName(n) : localName(n.name)),
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
      const r = html("div");r.setAttribute("data-graph-field",k);
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
      if(n.discovery_state==='unresolved_endpoint_owner') {
        row(interfaceSection,'Reported name',n.reported_name??'Not reported');
        interfaceSection.append(html('p','inspector-note',n.transport_endpoint
          ? 'Internal transport endpoint. ROS discovery does not report its owning node; this is not an additional identified application node.'
          : 'ROS discovery does not report the owning node for this endpoint. Its relationships are preserved without assigning an owner.'));
      }
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
          other?.display_name || other?.fully_qualified_name || other?.name || otherId,
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
    if (n.kind === "topic" || n.kind === "action") {
      const t=liveMetric(n), traffic=section(n.kind==='action'?'Action status / feedback':'Observed activity');
      traffic.append(html('div','activity-value',rate(t)));
      row(traffic,'Live state',activityLabel(n));
      row(traffic,'Monitor',t.state||'Unknown');
      row(traffic,'Messages',t.messages==null?'—':String(t.messages));
      const interval=this.flowActivity.interval(n.id);
      row(traffic,'Last update',interval?`${interval.messages} messages / ${(interval.durationMs/1000).toFixed(2)} s`:'Awaiting interval');
      if(n.kind==='topic') row(traffic,'Serialized bytes',t.bytes==null?'Not measured':Number(t.bytes).toLocaleString());
      row(traffic,'Last received',finiteAge(t.last_message_age_ms)?`${(t.last_message_age_ms/1000).toFixed(2)} s ago`:'No receipt observed');
      if(n.kind==='topic')row(traffic,'Source',activitySource(t));
      const reason=html('p','inspector-note',t.reason||'');reason.setAttribute('data-activity-reason','');reason.hidden=!t.reason;traffic.append(reason);
      if(n.kind==='action') {
        row(traffic,'Status messages',t.status_messages==null?'—':String(t.status_messages));
        row(traffic,'Feedback messages',t.feedback_messages==null?'—':String(t.feedback_messages));
        const counts=t.status?.status_summary?.counts;
        row(traffic,'Last reported goals',counts?(Object.entries(counts).filter(([,v])=>v).map(([k,v])=>`${k}: ${v}`).join(' · ')||'None'):'Not observed');
        traffic.append(html('p','inspector-note','Action dots follow observed server status/feedback toward clients. Goal requests and per-client receipt are not inferred.'));
      } else traffic.append(html('p','inspector-note','Sphere labels show observed messages since the previous update; a sphere is not one packet. Warm dots follow publications and cool dots follow subscriptions. Subscriber-route counts do not confirm receipt at each subscriber.'));
      if(!this.data.watch?.enabled && n.kind==='topic') {
        const button=html('button','sample-button','Sample activity for 5 s');
        button.onclick=async()=>{button.disabled=true;try{await this.request('/api/v1/ros/sample',{topic:n.name,duration_s:5});await this.refresh();}catch(e){this.onError(e);}finally{button.disabled=false;}};
        traffic.append(button,html('p','inspector-note','This backend supports finite samples only; it cannot provide continuous live monitoring.'));
      }
      if(this.reduceMotion.matches)traffic.append(html('p','inspector-note','Reduced motion: live rates and histories remain available; moving dots are disabled.'));
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
          [...this.data.errors.reduce((counts,e)=>{
            const message=e.message||e.reason||String(e);counts.set(message,(counts.get(message)||0)+1);return counts;
          },new Map())].map(([message,count])=>`${count>1?`${count} occurrences: `:''}${message}`).join('; '),
        ),
      );
  }
}
