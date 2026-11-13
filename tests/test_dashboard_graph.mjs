import test from "node:test";
import assert from "node:assert/strict";
import { normalizeGraph, selectGraphView } from "../web/graph.js";

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
