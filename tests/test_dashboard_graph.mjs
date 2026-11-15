import test from "node:test";
import assert from "node:assert/strict";
import { normalizeGraph, selectGraphView, selectionPaths } from "../web/graph.js";

// A generic graph: duplicate basenames, several publishers, a node publishing
// and subscribing to one topic, service/action peers, and an isolated node.
const node = (id, name, namespace = "/") => ({
  id,
  name,
  namespace,
  fully_qualified_name: `${namespace === "/" ? "" : namespace}/${name}`,
  instance_count: 1,
});
const fixture = () => ({
  schema_version: 1,
  nodes: [
    node("cameraA", "camera", "/a"),
    node("cameraB", "camera", "/b"),
    node("worker", "worker"),
    node("viewer", "viewer"),
    node("idle", "idle"),
  ],
  topics: [
    {
      id: "image",
      name: "/images",
      types: ["sensor_msgs/msg/Image"],
      traffic: { state: "not_sampled", rate_hz: null, messages: null },
    },
    { id: "result", name: "/result", types: ["example_msgs/msg/Result"] },
    { id: "rosout", name: "/rosout", types: ["rcl_interfaces/msg/Log"] },
  ],
  services: [
    {
      id: "service",
      name: "/inspect",
      types: ["example_interfaces/srv/Trigger"],
    },
  ],
  actions: [
    {
      id: "action",
      name: "/run",
      types: ["example_interfaces/action/Fibonacci"],
    },
  ],
  edges: [
    {
      id: "pA",
      kind: "publish",
      from: "cameraA",
      to: "image",
      endpoint_id: "gidA",
    },
    {
      id: "pB",
      kind: "publish",
      from: "cameraB",
      to: "image",
      endpoint_id: "gidB",
    },
    {
      id: "sW",
      kind: "subscribe",
      from: "image",
      to: "worker",
      endpoint_id: "gidW",
    },
    { id: "pW", kind: "publish", from: "worker", to: "result" },
    { id: "sSelf", kind: "subscribe", from: "result", to: "worker" },
    { id: "sV", kind: "subscribe", from: "result", to: "viewer" },
    { id: "sc", kind: "service_client", from: "worker", to: "service" },
    { id: "ss", kind: "service_server", from: "service", to: "cameraA" },
    { id: "ac", kind: "action_client", from: "viewer", to: "action" },
    { id: "as", kind: "action_server", from: "action", to: "worker" },
    { id: "log", kind: "publish", from: "worker", to: "rosout" },
  ],
});
const ids = (rows) => rows.map((x) => x.id).sort();

test("normalization retains all declared endpoint edges and unknown traffic", () => {
  const source = fixture(),
    before = JSON.stringify(source),
    graph = normalizeGraph(source);
  assert.equal(graph.nodes.length, 5);
  assert.equal(graph.entities.size, 10);
  assert.deepEqual(ids(graph.edges), ids(source.edges));
  assert.equal(graph.topics[0].traffic.rate_hz, null);
  assert.equal(graph.topics[0].traffic.messages, null);
  assert.notEqual(
    graph.entities.get("cameraA").fully_qualified_name,
    graph.entities.get("cameraB").fully_qualified_name,
  );
  selectGraphView(graph, { focus: "worker", scope: "focus" });
  assert.equal(JSON.stringify(source), before);
});

test("topic focus retains every publisher and subscription endpoint without duplicate nodes", () => {
  const view = selectGraphView(normalizeGraph(fixture()), {
    focus: "image",
    scope: "focus",
  });
  assert.deepEqual(ids(view.entities), [
    "cameraA",
    "cameraB",
    "image",
    "worker",
  ]);
  assert.deepEqual(ids(view.edges), ["pA", "pB", "sW"]);
  assert.equal(new Set(ids(view.entities)).size, view.entities.length);
});

test("node focus includes all endpoint peers of its adjacent interfaces", () => {
  const view = selectGraphView(normalizeGraph(fixture()), {
    focus: "worker",
    scope: "focus",
  });
  assert.deepEqual(ids(view.entities), [
    "action",
    "cameraA",
    "cameraB",
    "image",
    "result",
    "service",
    "viewer",
    "worker",
  ]);
  assert.deepEqual(ids(view.edges), [
    "ac",
    "as",
    "pA",
    "pB",
    "pW",
    "sSelf",
    "sV",
    "sW",
    "sc",
    "ss",
  ]);
  assert.equal(view.entities.filter((x) => x.id === "worker").length, 1);
});

test("service/action focus preserves client and server direction", () => {
  const graph = normalizeGraph(fixture());
  assert.deepEqual(
    selectGraphView(graph, { focus: "service", scope: "focus" }).edges.map(
      (e) => [e.kind, e.from, e.to],
    ),
    [
      ["service_client", "worker", "service"],
      ["service_server", "service", "cameraA"],
    ],
  );
  assert.deepEqual(
    selectGraphView(graph, { focus: "action", scope: "focus" }).edges.map(
      (e) => [e.kind, e.from, e.to],
    ),
    [
      ["action_client", "viewer", "action"],
      ["action_server", "action", "worker"],
    ],
  );
});

test("view filtering leaves original infrastructure discoverable and supports full type search", () => {
  const graph = normalizeGraph(fixture());
  assert(!selectGraphView(graph).entities.some((x) => x.id === "rosout"));
  assert(
    selectGraphView(graph, { showInfrastructure: true }).edges.some(
      (x) => x.id === "log",
    ),
  );
  assert(graph.entities.has("rosout"));
  const view = selectGraphView(graph, { search: "sensor_msgs/msg/Image" });
  assert.deepEqual(ids(view.edges), ["pA", "pB", "sW"]);
});

test("focusing an isolated discovered node keeps that node visible", () => {
  const view = selectGraphView(normalizeGraph(fixture()), {
    focus: "idle",
    scope: "focus",
  });
  assert.deepEqual(ids(view.entities), ["idle"]);
  assert.deepEqual(view.edges, []);
});

function pathFixture(){
  const names=['p1','p2','selected','other-sub','co-out','downstream','next-sub','service-client2','service-server1','service-server2','action-client1','action-client2','action-server2'];
  return normalizeGraph({schema_version:1,nodes:names.map(id=>node(id,id)),
    topics:[{id:'input',name:'/input',types:['example/msg/Input']},{id:'output',name:'/output',types:['example/msg/Output']},{id:'next',name:'/next',types:['example/msg/Next']}],
    services:[{id:'svc',name:'/inspect',types:['example/srv/Inspect']}],actions:[{id:'act',name:'/execute',types:['example/action/Execute']}],
    edges:[
      {id:'p1-in',kind:'publish',from:'p1',to:'input'},
      {id:'p2-in',kind:'publish',from:'p2',to:'input'},
      {id:'selected-in',kind:'subscribe',from:'input',to:'selected'},
      {id:'other-in',kind:'subscribe',from:'input',to:'other-sub'},
      {id:'selected-out',kind:'publish',from:'selected',to:'output'},
      {id:'co-out',kind:'publish',from:'co-out',to:'output'},
      {id:'down-out',kind:'subscribe',from:'output',to:'downstream'},
      {id:'down-next',kind:'publish',from:'downstream',to:'next'},
      {id:'next-sub',kind:'subscribe',from:'next',to:'next-sub'},
      {id:'selected-service',kind:'service_client',from:'selected',to:'svc'},
      {id:'other-service-client',kind:'service_client',from:'service-client2',to:'svc'},
      {id:'service-server1',kind:'service_server',from:'svc',to:'service-server1'},
      {id:'service-server2',kind:'service_server',from:'svc',to:'service-server2'},
      {id:'selected-action',kind:'action_server',from:'act',to:'selected'},
      {id:'other-action-server',kind:'action_server',from:'act',to:'action-server2'},
      {id:'action-client1',kind:'action_client',from:'action-client1',to:'act'},
      {id:'action-client2',kind:'action_client',from:'action-client2',to:'act'},
    ]});
}
const setIds=set=>[...set].sort();

test('subscriber selection includes its upstream publications but no sibling subscriptions',()=>{
  const graph=pathFixture(),paths=selectionPaths(graph,'other-sub');
  assert.deepEqual(setIds(paths.highlighted),['other-in','p1-in','p2-in']);
  assert.deepEqual(setIds(paths.publisherNodes),['p1','p2']);
});

test('publisher selection includes all downstream subscriptions but no co-publishers',()=>{
  const graph=pathFixture(),paths=selectionPaths(graph,'p1');
  assert.deepEqual(setIds(paths.highlighted),['other-in','p1-in','selected-in']);
  assert.deepEqual(setIds(paths.publisherNodes),['p1']);
});

test('node context expands opposite interface roles one hop without following the next pipeline stage',()=>{
  const graph=pathFixture(),before=JSON.stringify(graph.edges),paths=selectionPaths(graph,'selected');
  assert.deepEqual(setIds(paths.highlighted),[
    'action-client1','action-client2','down-out','p1-in','p2-in','selected-action','selected-in','selected-out','selected-service','service-server1','service-server2',
  ]);
  assert.deepEqual(setIds(paths.publisherNodes),['p1','p2','selected']);
  for(const absent of ['other-in','co-out','down-next','next-sub','other-service-client','other-action-server'])assert.equal(paths.highlighted.has(absent),false);
  assert.equal(JSON.stringify(graph.edges),before);
});

test('interface focus is direct; action clients and service servers expand only their opposing roles',()=>{
  const graph=pathFixture();
  assert.deepEqual(setIds(selectionPaths(graph,'input').highlighted),['other-in','p1-in','p2-in','selected-in']);
  assert.deepEqual(setIds(selectionPaths(graph,'act').highlighted),['action-client1','action-client2','other-action-server','selected-action']);
  assert.deepEqual(setIds(selectionPaths(graph,'svc').highlighted),['other-service-client','selected-service','service-server1','service-server2']);
  assert.deepEqual(setIds(selectionPaths(graph,'action-client1').highlighted),['action-client1','other-action-server','selected-action']);
  assert.deepEqual(setIds(selectionPaths(graph,'service-server1').highlighted),['other-service-client','selected-service','service-server1']);
});

test('selection paths respect visible edges and no selection produces no highlighted path',()=>{
  const graph=pathFixture(),visible=graph.edges.filter(e=>['p1-in','selected-in'].includes(e.id));
  assert.deepEqual(setIds(selectionPaths(graph,'p1',visible).highlighted),['p1-in','selected-in']);
  assert.deepEqual(setIds(selectionPaths(graph,null).highlighted),[]);
  assert.deepEqual(setIds(selectionPaths(graph,'missing').publisherNodes),[]);
});

function infrastructureFixture(){
  const unresolved=(id,name=null)=>({id,name,namespace:null,fully_qualified_name:null,instance_count:null,discovery_state:'unresolved_endpoint_owner'});
  return{schema_version:1,nodes:[node('named','named'),node('idle','idle'),node('named-log-only','named-log-only'),
    unresolved('transport-only'),unresolved('mixed-owner'),unresolved('ordinary-owner','reported-placeholder'),unresolved('log-owner')],
    topics:[
      {id:'base',name:'/camera/image',types:['sensor_msgs/msg/Image']},
      {id:'buffer',name:'/camera/image/_buf_tcp',types:['sensor_msgs/msg/Image']},
      {id:'buffer2',name:'/camera/image/_buf_shared_memory',types:['sensor_msgs/msg/Image']},
      {id:'mismatch',name:'/camera/image/_buf_udp',types:['sensor_msgs/msg/CompressedImage']},
      {id:'orphan',name:'/application/_buf_tcp',types:['example/msg/Value']},
      {id:'unknown-base',name:'/unknown',types:[]},
      {id:'unknown-buffer',name:'/unknown/_buf_shm',types:[]},
      {id:'ordinary',name:'/control/state',types:['sensor_msgs/msg/JointState']},
      {id:'middle',name:'/application/_buf_tcp/ordinary',types:['example/msg/Value']},
      {id:'log',name:'/rosout',types:['rcl_interfaces/msg/Log']},
    ],services:[],actions:[],edges:[
      {id:'base-pub',kind:'publish',from:'named',to:'base'},
      {id:'buffer-pub',kind:'publish',from:'transport-only',to:'buffer'},
      {id:'buffer-sub',kind:'subscribe',from:'buffer2',to:'transport-only'},
      {id:'mixed-pub',kind:'publish',from:'mixed-owner',to:'buffer'},
      {id:'ordinary-sub',kind:'subscribe',from:'ordinary',to:'mixed-owner'},
      {id:'orphan-pub',kind:'publish',from:'ordinary-owner',to:'orphan'},
      {id:'log-unknown',kind:'publish',from:'log-owner',to:'log'},
      {id:'log-named',kind:'publish',from:'named-log-only',to:'log'},
    ]};
}

test('transport buffers are infrastructure only with an existing base and the same nonempty type set',()=>{
  const graph=normalizeGraph(infrastructureFixture()),view=selectGraphView(graph),visible=new Set(ids(view.entities));
  assert.equal(graph.entities.get('buffer').transport_topic,true);assert.equal(graph.entities.get('buffer2').transport_topic,true);
  for(const id of ['mismatch','orphan','unknown-buffer','middle']){
    assert.equal(graph.entities.get(id).transport_topic,false,id);assert.equal(visible.has(id),true,id);
  }
  assert.equal(visible.has('buffer'),false);assert.equal(visible.has('buffer2'),false);assert.equal(visible.has('base'),true);
});

test('only unresolved endpoints whose entire context is hidden infrastructure disappear by default',()=>{
  const graph=normalizeGraph(infrastructureFixture()),view=selectGraphView(graph),visible=new Set(ids(view.entities));
  for(const id of ['transport-only','log-owner'])assert.equal(visible.has(id),false,id);
  for(const id of ['mixed-owner','ordinary-owner','named','idle','named-log-only'])assert.equal(visible.has(id),true,id);
  assert.equal(graph.entities.get('transport-only').transport_endpoint,true);assert.equal(graph.entities.get('mixed-owner').transport_endpoint,false);
  const full=selectGraphView(graph,{showInfrastructure:true});
  assert.deepEqual(ids(full.edges),ids(graph.edges));assert.deepEqual(new Set(ids(full.entities)),new Set(graph.entities.keys()));
});

test('friendly unresolved labels retain reported names and transport topics retain their raw identity',()=>{
  const original=infrastructureFixture(),before=JSON.stringify(original),graph=normalizeGraph(original);
  const hidden=graph.entities.get('transport-only'),ordinary=graph.entities.get('ordinary-owner');
  assert.equal(hidden.reported_name,null);assert.equal(typeof hidden.name,'string');assert.match(hidden.display_name,/Transport/);
  assert.equal(ordinary.reported_name,'reported-placeholder');assert.equal(ordinary.name,'reported-placeholder');
  assert.equal(ordinary.fully_qualified_name,null);assert.match(ordinary.display_name,/Unresolved/);
  assert.equal(graph.entities.get('buffer').name,'/camera/image/_buf_tcp');assert.equal(graph.entities.get('buffer').id,'buffer');
  assert.equal(JSON.stringify(original),before);
});
