/** Pure shared Blocks/JSON draft model. No network, permissions or robot admission.
 * JSON text is retained verbatim until a block edit. Unknown JSON fields survive
 * edits. Row IDs live only in draft.meta, never in the transmitted payload.
 */

export class PayloadDraftError extends Error {
  constructor(message, path = []) {
    super(message);
    this.name = "PayloadDraftError";
    this.path = [...path];
  }
}

const object = (value) =>
  value !== null && typeof value === "object" && !Array.isArray(value);
const own = (value, key) => Object.hasOwn(value, key);
const copy = (value) => JSON.parse(JSON.stringify(value));
const positive = (value, label) => {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) {
    throw new PayloadDraftError(`${label} must be a positive finite number`);
  }
  return value;
};

function finiteTree(value, path = []) {
  if (typeof value === "number" && !Number.isFinite(value))
    throw new PayloadDraftError(
      "JSON number cannot be represented as a finite value",
      path,
    );
  if (value && typeof value === "object")
    for (const [key, child] of Object.entries(value))
      finiteTree(child, [...path, key]);
}

function shape(context, value) {
  const type = String(
    context.type ?? context.operation_type ?? "",
  ).toUpperCase();
  const mode = String(context.mode ?? "").toUpperCase();
  if (
    !["DIRECT", "TRAJECTORY", "MODEL"].includes(type) ||
    !["INLINE", "STREAM"].includes(mode)
  ) {
    return {
      kind: null,
      issue: "Choose MODEL, TRAJECTORY or DIRECT and INLINE or STREAM.",
    };
  }
  const cartesian =
    own(value, "cartesian") || value.payload_kind === "CARTESIAN_TARGET";
  if (cartesian) {
    if (type !== "TRAJECTORY" || mode !== "INLINE")
      return {
        kind: null,
        issue: "Cartesian endpoints are supported only for TRAJECTORY INLINE.",
      };
    if (own(value, "points"))
      return {
        kind: null,
        issue:
          "Choose cartesian or points in JSON; do not mix trajectory bodies.",
      };
    return { kind: "cartesian_sequence", collection: "cartesian" };
  }
  if (type === "MODEL")
    return mode === "INLINE"
      ? { kind: "model_sequence", collection: "model_steps" }
      : { kind: "model_task" };
  if (type === "DIRECT" && mode === "STREAM") return { kind: "direct_target" };
  return { kind: "joint_sequence", collection: "points" };
}

function metadata(context, value, previous, rowIds) {
  const descriptor = shape(context, value);
  const count = descriptor.collection
    ? Array.isArray(value[descriptor.collection])
      ? value[descriptor.collection].length
      : 0
    : descriptor.kind
      ? 1
      : 0;
  let nextRowId = previous?.nextRowId ?? 1;
  const same =
    previous?.kind === descriptor.kind &&
    previous?.collection === descriptor.collection;
  const preserved = rowIds ?? (same ? previous.rowIds : []);
  const ids = Array.from(
    { length: count },
    (_, index) => preserved[index] ?? `row-${nextRowId++}`,
  );
  return {
    kind: descriptor.kind,
    collection: descriptor.collection,
    rowIds: ids,
    nextRowId,
  };
}

/** `valid` means a parsed finite JSON object, not a valid robot command. */
export function openDraft(context, jsonText) {
  const text = String(jsonText);
  const base = {
    context: { ...context },
    text,
    value: null,
    valid: false,
    error: null,
    meta: { rowIds: [], nextRowId: 1 },
  };
  try {
    const value = JSON.parse(text);
    if (!object(value))
      throw new PayloadDraftError("The payload must be a JSON object.");
    finiteTree(value);
    return { ...base, value, valid: true, meta: metadata(base.context, value) };
  } catch (error) {
    return {
      ...base,
      error: { path: error.path || [], message: error.message },
    };
  }
}

function parsed(draft) {
  if (!draft?.valid || !object(draft.value))
    throw new PayloadDraftError(
      draft?.error?.message || "Fix the JSON draft before editing blocks.",
      draft?.error?.path || [],
    );
  return draft.value;
}

function edited(draft, value, rowIds) {
  if (!object(value))
    throw new PayloadDraftError("The payload must remain a JSON object.");
  finiteTree(value);
  const text = JSON.stringify(value, null, 2);
  const result = openDraft(draft.context, text);
  if (!result.valid)
    throw new PayloadDraftError(result.error.message, result.error.path);
  result.meta = metadata(result.context, result.value, draft.meta, rowIds);
  return result;
}

function durations(points) {
  let previous = 0;
  return points.map((point, index) => {
    if (!object(point))
      throw new PayloadDraftError(
        `Step ${index + 1} must be an object; edit JSON to repair it.`,
        ["points", index],
      );
    const time = point.time_s;
    if (
      typeof time !== "number" ||
      !Number.isFinite(time) ||
      time <= previous
    ) {
      throw new PayloadDraftError(
        "Point time_s values must be positive and strictly increasing. Correct the cumulative times before adding, removing or reordering steps.",
        ["points", index, "time_s"],
      );
    }
    const duration = time - previous;
    previous = time;
    return duration;
  });
}

function retime(points, intervals) {
  let elapsed = 0;
  return points.map((point, index) => {
    elapsed += positive(intervals[index], "Segment duration");
    if (!Number.isFinite(elapsed))
      throw new PayloadDraftError("Total point duration is too large.");
    return { ...point, time_s: elapsed };
  });
}

/** Rows: {id,index,path,value,segmentDuration?}; singleton path is [].
 * Each value is a copy. `segmentDuration` exists only on valid timed point rows.
 */
export function describeBlocks(draft) {
  if (!draft?.valid)
    return {
      kind: null,
      rows: [],
      structuralEditsEnabled: false,
      issues: [draft?.error || { message: "Open a JSON draft first." }],
    };
  const value = parsed(draft),
    descriptor = shape(draft.context, value);
  const result = {
    kind: descriptor.kind,
    collection: descriptor.collection,
    rows: [],
    structuralEditsEnabled: false,
    issues: [],
  };
  if (descriptor.issue) {
    result.issues.push({ path: [], message: descriptor.issue });
    return result;
  }
  if (!descriptor.collection) {
    result.rows = [
      { id: draft.meta.rowIds[0], index: 0, path: [], value: copy(value) },
    ];
    return result;
  }
  const list = value[descriptor.collection];
  if (!Array.isArray(list)) {
    result.issues.push({
      path: [descriptor.collection],
      message: `${descriptor.collection} must be an array. Edit JSON to create or repair it.`,
    });
    return result;
  }
  let intervals;
  try {
    if (descriptor.kind === "joint_sequence") intervals = durations(list);
    else if (list.some((step) => !object(step)))
      throw new PayloadDraftError(
        "Every step must be an object. Edit JSON to repair malformed steps.",
        [descriptor.collection],
      );
    result.structuralEditsEnabled = true;
  } catch (error) {
    result.issues.push({
      path: error.path || [descriptor.collection],
      message: error.message,
    });
  }
  result.rows = list.map((step, index) => ({
    id: draft.meta.rowIds[index],
    index,
    path: [descriptor.collection, index],
    value: copy(step),
    ...(intervals ? { segmentDuration: intervals[index] } : {}),
  }));
  return result;
}

/** Paths are arrays of object keys/array indices. undefined explicitly removes
 * an object field (e.g. model duration_s); array removal uses removeStep instead.
 */
export function setValue(draft, path, value) {
  const source = parsed(draft);
  if (!Array.isArray(path))
    throw new PayloadDraftError("Field path must be an array.");
  if (!path.length) {
    if (!object(value))
      throw new PayloadDraftError("The payload must remain a JSON object.");
    return edited(draft, copy(value), []);
  }
  if (value !== undefined) finiteTree(value);
  const next = copy(source);
  let container = next;
  for (let i = 0; i < path.length - 1; i++) {
    const key = path[i];
    if (!container || typeof container !== "object" || !own(container, key))
      throw new PayloadDraftError(
        "Field path does not exist; repair the structure in JSON first.",
        path.slice(0, i + 1),
      );
    container = container[key];
  }
  const key = path.at(-1);
  if (!container || typeof container !== "object")
    throw new PayloadDraftError(
      "Cannot edit a field inside a non-object value.",
      path.slice(0, -1),
    );
  if (Array.isArray(container)) {
    if (
      !Number.isInteger(key) ||
      key < 0 ||
      key >= container.length ||
      value === undefined
    )
      throw new PayloadDraftError(
        "Use step operations to add/remove array entries; setValue requires an existing index.",
        path,
      );
    container[key] = copy(value);
  } else {
    if (typeof key !== "string")
      throw new PayloadDraftError("Object field names must be strings.", path);
    if (value === undefined) delete container[key];
    else
      Object.defineProperty(container, key, {
        value: copy(value),
        enumerable: true,
        writable: true,
        configurable: true,
      });
  }
  // Replacing a collection wholesale creates new editor row identities.
  const resetRows = path.length === 1 && key === draft.meta.collection;
  return edited(draft, next, resetRows ? [] : undefined);
}

function sequence(draft) {
  parsed(draft);
  const description = describeBlocks(draft);
  if (!description.collection)
    throw new PayloadDraftError(
      "This payload is a single target/task; it has no steps to reorder.",
    );
  if (!description.structuralEditsEnabled)
    throw new PayloadDraftError(
      description.issues[0]?.message || "Repair the sequence in JSON first.",
      description.issues[0]?.path || [],
    );
  return description;
}

function rowIndex(description, rowId) {
  const index = description.rows.findIndex((row) => row.id === rowId);
  if (index < 0)
    throw new PayloadDraftError(
      "Step no longer exists. Refresh the block view.",
    );
  return index;
}

export function setSegmentDuration(draft, rowId, seconds) {
  const description = sequence(draft);
  if (description.kind !== "joint_sequence")
    throw new PayloadDraftError(
      "Use duration_s directly for model and Cartesian steps.",
    );
  positive(seconds, "Segment duration");
  const intervals = description.rows.map((row) => row.segmentDuration);
  intervals[rowIndex(description, rowId)] = seconds;
  const next = copy(draft.value);
  next.points = retime(next.points, intervals);
  return edited(draft, next);
}

/** Point seed: {duration_s,positions,gripper?,...}. duration_s is editor input
 * only: consume it to calculate time_s, retaining every other seed property.
 * Model/Cartesian seeds are their actual row objects, including optional fields.
 */
export function addStep(draft, seed, { index } = {}) {
  const description = sequence(draft);
  if (!object(seed))
    throw new PayloadDraftError(
      "Supply an explicit step seed; no joint or gripper defaults are invented.",
    );
  finiteTree(seed);
  const at = index ?? description.rows.length;
  if (!Number.isInteger(at) || at < 0 || at > description.rows.length)
    throw new PayloadDraftError("Insertion index is outside the sequence.");
  const next = copy(draft.value),
    row = copy(seed),
    intervals = description.rows.map((item) => item.segmentDuration);
  if (description.kind === "joint_sequence") {
    intervals.splice(at, 0, positive(row.duration_s, "New segment duration"));
    delete row.duration_s;
  }
  next[description.collection].splice(at, 0, row);
  if (description.kind === "joint_sequence")
    next.points = retime(next.points, intervals);
  const ids = [...draft.meta.rowIds];
  ids.splice(at, 0, `row-${draft.meta.nextRowId}`);
  return edited(
    { ...draft, meta: { ...draft.meta, nextRowId: draft.meta.nextRowId + 1 } },
    next,
    ids,
  );
}

export function removeStep(draft, rowId) {
  const description = sequence(draft),
    at = rowIndex(description, rowId);
  const next = copy(draft.value),
    ids = [...draft.meta.rowIds],
    intervals = description.rows.map((row) => row.segmentDuration);
  next[description.collection].splice(at, 1);
  ids.splice(at, 1);
  intervals.splice(at, 1);
  if (description.kind === "joint_sequence")
    next.points = retime(next.points, intervals);
  return edited(draft, next, ids);
}

export function moveStep(draft, rowId, toIndex) {
  const description = sequence(draft),
    from = rowIndex(description, rowId);
  if (
    !Number.isInteger(toIndex) ||
    toIndex < 0 ||
    toIndex >= description.rows.length
  )
    throw new PayloadDraftError("Destination index is outside the sequence.");
  if (from === toIndex) return draft;
  const next = copy(draft.value),
    ids = [...draft.meta.rowIds],
    intervals = description.rows.map((row) => row.segmentDuration);
  const move = (list) => list.splice(toIndex, 0, list.splice(from, 1)[0]);
  move(next[description.collection]);
  move(ids);
  move(intervals);
  if (description.kind === "joint_sequence")
    next.points = retime(next.points, intervals);
  return edited(draft, next, ids);
}

export function getJson(draft) {
  return draft.text;
}
/** Returns the parsed semantic object. Existing UI/transport validation still
 * decides whether its shape/fields/capabilities are admissible for submission.
 */
export function getPayload(draft) {
  return copy(parsed(draft));
}

export default {
  openDraft,
  describeBlocks,
  setValue,
  setSegmentDuration,
  addStep,
  removeStep,
  moveStep,
  getJson,
  getPayload,
};
