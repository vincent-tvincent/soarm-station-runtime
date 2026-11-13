import test from "node:test";
import assert from "node:assert/strict";
import {
  openDraft,
  describeBlocks,
  setValue,
  setSegmentDuration,
  addStep,
  removeStep,
  moveStep,
  getJson,
  getPayload,
} from "../web/payload-model.js";

const context = (type = "DIRECT", mode = "INLINE") => ({
  type,
  mode,
  armId: "arm1",
  profileId: "profile1",
});
const open = (value, ctx = context()) => openDraft(ctx, JSON.stringify(value));
const points = () => ({
  points: [
    {
      time_s: 1,
      positions: { shoulder: 0.1 },
      gripper: 30,
      label: "A",
      extra: null,
    },
    {
      time_s: 3,
      positions: { shoulder: 0.2 },
      gripper: 40,
      label: "B",
      vendor: { value: null },
    },
    { time_s: 6, positions: { shoulder: 0.3 }, gripper: 50, label: "C" },
  ],
  metadata: { retained: true, nullable: null },
});
const ids = (draft) => describeBlocks(draft).rows.map((row) => row.id);

test("view changes and descriptors retain original JSON text and semantic unknown/null fields", () => {
  const raw =
    '{ "points" : [{"time_s":1.00,"positions":{"shoulder":0.1},"gripper":30,"extra":null}], "future": {"values":[null,false,0,""]} }';
  const draft = openDraft(context(), raw);
  assert.equal(draft.valid, true);
  describeBlocks(draft);
  getPayload(draft);
  describeBlocks(draft);
  assert.equal(getJson(draft), raw);
  const edited = setValue(draft, ["points", 0, "positions", "shoulder"], 0.2);
  const expected = JSON.parse(raw);
  expected.points[0].positions.shoulder = 0.2;
  assert.deepEqual(getPayload(edited), expected);
  assert.deepEqual(ids(edited), ids(draft));
  assert.equal(getJson(draft), raw);
});

test("invalid JSON, non-object roots and nonfinite overflow have no last-valid payload", () => {
  const valid = open(points());
  for (const raw of ['{"points":[}', "null", "[]", '{"number":1e400}']) {
    const invalid = openDraft(valid.context, raw);
    assert.equal(invalid.valid, false);
    assert.equal(invalid.value, null);
    assert.equal(getJson(invalid), raw);
    assert.equal(describeBlocks(invalid).structuralEditsEnabled, false);
    assert.ok(describeBlocks(invalid).issues.length);
    assert.throws(() => getPayload(invalid));
    assert.throws(() => setValue(invalid, ["points"], []));
  }
  assert.deepEqual(getPayload(valid), points());
});

test("all six operation modes and configured Cartesian shape have the agreed descriptors", () => {
  const cases = [
    ["DIRECT", "INLINE", { points: [] }, "joint_sequence", "points"],
    [
      "DIRECT",
      "STREAM",
      { positions: { shoulder: 0.1 }, gripper: 40 },
      "direct_target",
      undefined,
    ],
    ["TRAJECTORY", "INLINE", { points: [] }, "joint_sequence", "points"],
    ["TRAJECTORY", "STREAM", { points: [] }, "joint_sequence", "points"],
    ["MODEL", "INLINE", { model_steps: [] }, "model_sequence", "model_steps"],
    ["MODEL", "STREAM", { instruction: "" }, "model_task", undefined],
    [
      "TRAJECTORY",
      "INLINE",
      { payload_kind: "CARTESIAN_TARGET", cartesian: [] },
      "cartesian_sequence",
      "cartesian",
    ],
  ];
  for (const [type, mode, payload, kind, collection] of cases) {
    const draft = open(payload, context(type, mode)),
      blocks = describeBlocks(draft);
    assert.equal(blocks.kind, kind);
    assert.equal(blocks.collection, collection);
    if (collection === undefined) {
      assert.equal(blocks.rows.length, 1);
      assert.deepEqual(blocks.rows[0].path, []);
      assert.equal(blocks.structuralEditsEnabled, false);
      assert.throws(() => addStep(draft, {}), /single target/);
    } else assert.equal(blocks.structuralEditsEnabled, true);
    assert.deepEqual(getPayload(draft), payload);
  }
  const unavailable = describeBlocks(
    open({ cartesian: [] }, context("TRAJECTORY", "STREAM")),
  );
  assert.equal(unavailable.structuralEditsEnabled, false);
  assert.match(unavailable.issues[0].message, /INLINE/);
});

test("step-duration edits adjust later cumulative times without touching positions or row IDs", () => {
  const draft = open(points()),
    originalIds = ids(draft);
  assert.deepEqual(
    describeBlocks(draft).rows.map((row) => row.segmentDuration),
    [1, 2, 3],
  );
  const edited = setSegmentDuration(draft, originalIds[1], 4);
  assert.deepEqual(
    getPayload(edited).points.map((p) => p.time_s),
    [1, 5, 8],
  );
  assert.deepEqual(
    getPayload(edited).points.map((p) => p.positions),
    points().points.map((p) => p.positions),
  );
  assert.deepEqual(ids(edited), originalIds);
  assert.deepEqual(getPayload(edited).metadata, points().metadata);
  assert.deepEqual(getPayload(draft), points());
});

test("moving a point carries its segment duration and editor identity, then recalculates time_s", () => {
  const draft = open(points()),
    originalIds = ids(draft);
  const moved = moveStep(draft, originalIds[2], 0),
    value = getPayload(moved);
  assert.deepEqual(
    value.points.map((p) => p.label),
    ["C", "A", "B"],
  );
  assert.deepEqual(
    value.points.map((p) => p.time_s),
    [3, 4, 6],
  );
  assert.deepEqual(ids(moved), [
    originalIds[2],
    originalIds[0],
    originalIds[1],
  ]);
  assert.equal(value.points[1].extra, null);
  assert.deepEqual(value.points[2].vendor, { value: null });
  assert.equal(moveStep(moved, ids(moved)[0], 0), moved);
});

test("remove closes the deleted segment; add consumes only its explicit duration seed", () => {
  const draft = open(points()),
    originalIds = ids(draft);
  const removed = removeStep(draft, originalIds[1]);
  assert.deepEqual(
    getPayload(removed).points.map((p) => p.label),
    ["A", "C"],
  );
  assert.deepEqual(
    getPayload(removed).points.map((p) => p.time_s),
    [1, 4],
  );
  assert.deepEqual(ids(removed), [originalIds[0], originalIds[2]]);
  const seed = { duration_s: 0.5, positions: { shoulder: 0.12 }, note: null };
  const added = addStep(draft, seed, { index: 1 }),
    value = getPayload(added);
  assert.deepEqual(
    value.points.map((p) => p.time_s),
    [1, 1.5, 3.5, 6.5],
  );
  assert.equal(value.points[1].note, null);
  assert.equal(Object.hasOwn(value.points[1], "duration_s"), false);
  assert.equal(Object.hasOwn(value.points[1], "gripper"), false); // Never synthesize 50%.
  assert.deepEqual(seed, {
    duration_s: 0.5,
    positions: { shoulder: 0.12 },
    note: null,
  });
  assert.equal(ids(added)[0], originalIds[0]);
  assert.equal(ids(added)[2], originalIds[1]);
  assert.equal(new Set(ids(added)).size, 4);
  assert.throws(() => addStep(draft), /explicit step seed/);
  assert.throws(() => addStep(draft, { positions: {} }), /positive finite/);
});

test("empty arrays remain JSON drafts, can receive explicit steps, and do not gain automatic defaults", () => {
  const empty = open({ points: [] });
  assert.equal(empty.valid, true);
  assert.deepEqual(describeBlocks(empty).rows, []);
  const added = addStep(empty, {
    duration_s: 1,
    positions: { shoulder: 0 },
    gripper: 23,
  });
  const removed = removeStep(added, ids(added)[0]);
  assert.deepEqual(getPayload(removed), { points: [] });
  const next = addStep(removed, {
    duration_s: 2,
    positions: { shoulder: 0.1 },
    gripper: 31,
  });
  assert.notEqual(ids(next)[0], ids(added)[0]);
  assert.equal(getPayload(next).points[0].gripper, 31);
});

test("malformed cumulative timing disables structural edits until an explicit field repair", () => {
  for (const second of [null, 0, 1, -1]) {
    const draft = open({
      points: [
        { time_s: 1, positions: {} },
        { time_s: second, positions: {} },
      ],
    });
    const description = describeBlocks(draft);
    assert.equal(description.structuralEditsEnabled, false);
    assert.match(description.issues[0].message, /strictly increasing/);
    assert.throws(
      () => addStep(draft, { duration_s: 1, positions: {} }),
      /strictly increasing/,
    );
    assert.throws(
      () => removeStep(draft, ids(draft)[0]),
      /strictly increasing/,
    );
    assert.throws(
      () => moveStep(draft, ids(draft)[0], 1),
      /strictly increasing/,
    );
    assert.throws(
      () => setSegmentDuration(draft, ids(draft)[1], 2),
      /strictly increasing/,
    );
    const fixed = setValue(draft, ["points", 1, "time_s"], 3);
    assert.equal(describeBlocks(fixed).structuralEditsEnabled, true);
    assert.deepEqual(ids(fixed), ids(draft));
    assert.equal(getPayload(draft).points[1].time_s, second);
  }
});

test("model duration omission/null and Cartesian per-step durations survive reorder without accumulation", () => {
  const model = open(
    {
      model_steps: [
        { instruction: "", note: null },
        { instruction: "task", duration_s: 4 },
      ],
    },
    context("MODEL", "INLINE"),
  );
  const reordered = moveStep(model, ids(model)[1], 0);
  assert.equal(getPayload(reordered).model_steps[0].duration_s, 4);
  assert.equal(
    Object.hasOwn(getPayload(reordered).model_steps[1], "duration_s"),
    false,
  );
  const noDuration = setValue(
    reordered,
    ["model_steps", 0, "duration_s"],
    undefined,
  );
  assert.equal(
    Object.hasOwn(getPayload(noDuration).model_steps[0], "duration_s"),
    false,
  );
  const nullDuration = setValue(
    noDuration,
    ["model_steps", 0, "duration_s"],
    null,
  );
  assert.equal(getPayload(nullDuration).model_steps[0].duration_s, null);
  assert.deepEqual(ids(nullDuration), ids(reordered));
  const cart = open(
    {
      payload_kind: "CARTESIAN_TARGET",
      cartesian: [
        { frame_id: "base", position: [1, 2, 3], duration_s: 2, extra: null },
        {
          frame_id: "base",
          position: [4, 5, 6],
          duration_s: 5,
          orientation: [0, 0, 0, 1],
        },
      ],
    },
    context("TRAJECTORY", "INLINE"),
  );
  const moved = moveStep(cart, ids(cart)[1], 0);
  assert.deepEqual(
    getPayload(moved).cartesian.map((p) => p.duration_s),
    [5, 2],
  );
  assert.equal(getPayload(moved).cartesian[1].extra, null);
  assert.throws(
    () => setSegmentDuration(cart, ids(cart)[0], 3),
    /duration_s directly/,
  );
});

test("row metadata never leaks into payload and callers cannot mutate a draft through descriptors/getters", () => {
  const value = points();
  value.meta = { imported: true };
  const draft = open(value),
    description = describeBlocks(draft);
  description.rows[0].value.positions.shoulder = 99;
  const result = getPayload(draft);
  result.points[0].gripper = 99;
  assert.deepEqual(getPayload(draft), value);
  assert.deepEqual(getPayload(draft).meta, { imported: true });
  assert.equal(Object.hasOwn(getPayload(draft).meta, "rowIds"), false);
  assert.equal(Object.hasOwn(getPayload(draft).points[0], "id"), false);
  const edited = setValue(draft, ["points", 1, "gripper"], 37);
  assert.deepEqual(ids(edited), ids(draft));
});

test("setValue follows only own properties and safely preserves imported prototype-looking JSON keys", () => {
  const draft = openDraft(
    context("MODEL", "STREAM"),
    '{"instruction":"","outer":{"__proto__":{"owned":1}},"unknown":null}',
  );
  const edited = setValue(draft, ["outer", "__proto__", "owned"], 2);
  assert.equal(getPayload(edited).outer.__proto__.owned, 2);
  assert.equal({}.owned, undefined);
  assert.throws(
    () => setValue(draft, ["constructor", "prototype", "polluted"], true),
    /does not exist/,
  );
  const inserted = setValue(draft, ["__proto__"], { safe: true });
  assert.equal(Object.hasOwn(getPayload(inserted), "__proto__"), true);
  assert.equal({}.safe, undefined);
  assert.equal(getPayload(inserted).unknown, null);
});

test("malformed collection shapes remain intact, unsupported contexts are visible, and array holes are rejected", () => {
  const draft = open({ points: null, unknown: true });
  assert.equal(describeBlocks(draft).structuralEditsEnabled, false);
  assert.deepEqual(getPayload(draft), { points: null, unknown: true });
  assert.throws(() => addStep(draft, { duration_s: 1 }), /must be an array/);
  const list = open(points());
  assert.throws(() => setValue(list, ["points", 0], undefined), /remove array/);
  assert.throws(() => setValue(list, ["points", 99], {}), /existing index/);
  const mixed = open(
    { points: [], cartesian: [] },
    context("TRAJECTORY", "INLINE"),
  );
  assert.match(describeBlocks(mixed).issues[0].message, /do not mix/);
  assert.ok(describeBlocks(open({}, context("OTHER", "INLINE"))).issues.length);
});
