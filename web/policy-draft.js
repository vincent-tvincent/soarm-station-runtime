/** Shared Form/Raw editor state. This module never performs I/O or activates a policy.
 *
 * Paths are arrays (['remote', 'endpoint']); a string means one literal YAML key.
 * Parsing and reading leave raw text byte-for-byte unchanged. Form edits mutate a
 * YAML document so comments, unknown fields and their order survive serialization.
 * A saved response must be normalized to { text?, revision (or etag), id? }.
 */
import { isMap, isNode, parseDocument } from './yaml-vendor.js';

export class ProfileDraftError extends Error {
  constructor(message, { code = 'INVALID_DRAFT', path = [], errors = [] } = {}) {
    super(message);
    this.name = 'ProfileDraftError';
    this.code = code;
    this.path = [...path];
    this.errors = [...errors];
  }
}

const has = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
const textValue = (text) => {
  if (typeof text !== 'string') throw new TypeError('Profile text must be a string');
  return text;
};
const pathValue = (path) => {
  const result = Array.isArray(path) ? [...path] : [path];
  if (!result.length || result.some((part) =>
    typeof part !== 'string' && !(Number.isInteger(part) && part >= 0))) {
    throw new TypeError('Field path must contain string keys or nonnegative indices');
  }
  return result;
};
const nonemptyId = (id) => typeof id === 'string' && id.trim().length > 0;
const revisionOf = (source) => source.revision ?? source.etag ?? null;

function inspect(text) {
  const document = parseDocument(text, {
    keepSourceTokens: true,
    prettyErrors: true,
    uniqueKeys: true,
  });
  const errors = document.errors.map((error) => error.message);
  if (!errors.length && !isMap(document.contents)) {
    errors.push('A profile must be a YAML/JSON mapping, not a scalar or list.');
  }
  let value = null;
  if (!errors.length) {
    try { value = document.toJS({ maxAliasCount: 100 }); }
    catch (error) { errors.push(error.message); }
  }
  return {
    document,
    value: errors.length ? null : value,
    error: errors.length ? errors.join('\n') : null,
    errors,
    warnings: document.warnings.map((warning) => warning.message),
  };
}

function requireValid(result) {
  if (result.error) throw new ProfileDraftError(result.error, { errors: result.errors });
  return result;
}

function changeField(text, path, value, remove = false) {
  const keys = pathValue(path);
  const { document } = requireValid(inspect(text));
  const previous = document.getIn(keys, true);
  try {
    if (remove) document.deleteIn(keys);
    else {
      if (value === undefined) throw new TypeError('Use deleteField to remove a field');
      document.setIn(keys, value);
      const current = document.getIn(keys, true);
      // Replacing a collection may create a fresh node; retain its attached
      // comments as well as the untouched neighboring nodes in the document.
      if (isNode(previous) && isNode(current) && previous !== current) {
        for (const key of ['comment', 'commentBefore', 'spaceBefore', 'anchor']) {
          if (previous[key] !== undefined && current[key] === undefined) {
            current[key] = previous[key];
          }
        }
      }
    }
    const updated = document.toString({ lineWidth: 0 });
    requireValid(inspect(updated));
    return updated;
  } catch (error) {
    if (error instanceof ProfileDraftError) throw error;
    throw new ProfileDraftError(error.message, { code: 'FIELD_EDIT_FAILED', path: keys });
  }
}

export class ProfileDraft {
  constructor({ text = '', revision = null, etag, id } = {}) {
    this._epoch = 0;
    this._saveSequence = 0;
    this._lastAcknowledged = 0;
    this._snapshots = new WeakMap();
    this._editVersion = 0;
    this.reset({ text, revision: revision ?? etag ?? null, id });
  }

  get text() { return this._text; }
  get raw() { return this._text; }
  get baseText() { return this._baseText; }
  get revision() { return this._revision; }
  get etag() { return this._revision; }
  get id() { return this._id; }
  get editVersion() { return this._editVersion; }
  get isNew() { return this._isNew; }
  get dirty() { return this._isNew || this._text !== this._baseText; }
  get error() { return this.parse().error; }
  get valid() { return !this.error; }
  get conflict() { return this._conflict ? { ...this._conflict } : null; }

  parse() {
    const { value, error, errors, warnings } = inspect(this._text);
    return { value, error, errors, warnings };
  }

  getField(path, fallback = undefined) {
    let value = requireValid(this.parse()).value;
    for (const part of pathValue(path)) {
      if (value === null || typeof value !== 'object' || !has(value, part)) return fallback;
      value = value[part];
    }
    return value;
  }

  setRaw(text) {
    textValue(text);
    if (text !== this._text) {
      this._text = text;
      this._editVersion += 1;
    }
    return this.parse();
  }

  setField(path, value) {
    return this.setRaw(changeField(this._text, path, value));
  }

  deleteField(path) {
    const keys = pathValue(path);
    const parsed = requireValid(inspect(this._text));
    if (!parsed.document.hasIn(keys)) return this.parse();
    return this.setRaw(changeField(this._text, keys, undefined, true));
  }

  /** Freeze the precise request body and its precondition before async I/O. */
  prepareSave() {
    const { value } = requireValid(this.parse());
    if (!nonemptyId(this._id)) {
      throw new ProfileDraftError('A profile ID is required before saving.', { code: 'MISSING_ID' });
    }
    if (value.id !== this._id) {
      throw new ProfileDraftError('The raw ID differs from this profile. Use cloneAs(newId) to save a new identity.', {
        code: 'ID_CHANGED', path: ['id'],
      });
    }
    if (this._conflict) {
      throw new ProfileDraftError('Resolve the server conflict before saving again.', { code: 'CONFLICT' });
    }
    const snapshot = Object.freeze({
      text: this._text,
      revision: this._revision,
      id: this._id,
      editVersion: this._editVersion,
    });
    this._snapshots.set(snapshot, {
      epoch: this._epoch, sequence: ++this._saveSequence, settled: false,
    });
    return snapshot;
  }

  _snapshot(snapshot) {
    const record = snapshot && this._snapshots.get(snapshot);
    if (!record) throw new ProfileDraftError('Save snapshot does not belong to this draft.', { code: 'UNKNOWN_SAVE' });
    if (record.epoch !== this._epoch || record.settled || record.sequence < this._lastAcknowledged) return null;
    return record;
  }

  /** Acknowledge only successful server saves; never replace newer local edits. */
  acknowledgeSave(snapshot, response = {}) {
    const record = this._snapshot(snapshot);
    if (!record) return { acknowledged: false, reason: 'stale', dirty: this.dirty };
    if (response.status === 409 || response.status === 412 || response.conflict === true) {
      return this.markConflict(response, snapshot);
    }
    const revision = revisionOf(response);
    if (revision === null) throw new ProfileDraftError('A saved response must include its revision or ETag.', { code: 'MISSING_REVISION' });
    const id = response.id ?? snapshot.id;
    if (id !== snapshot.id) throw new ProfileDraftError('The server saved response has a different profile ID.', { code: 'ID_MISMATCH' });
    const savedText = textValue(response.text ?? snapshot.text);
    if (requireValid(inspect(savedText)).value.id !== id) {
      throw new ProfileDraftError('The server document ID does not match the saved profile.', { code: 'ID_MISMATCH' });
    }
    const matches = this._editVersion === snapshot.editVersion && this._text === snapshot.text;
    this._baseText = savedText;
    this._revision = revision;
    this._isNew = false;
    this._conflict = null;
    this._lastAcknowledged = record.sequence;
    record.settled = true;
    if (matches) this.setRaw(savedText);
    return { acknowledged: true, replacedDraft: matches, dirty: this.dirty };
  }

  /** Record a conflict without replacing local edits or adopting a new ETag. */
  markConflict(response = {}, snapshot = null) {
    const record = snapshot ? this._snapshot(snapshot) : null;
    if (snapshot && !record) return { acknowledged: false, reason: 'stale', dirty: this.dirty };
    if (record) record.settled = true;
    this._conflict = Object.freeze({
      reason: String(response.reason ?? response.message ?? 'The profile changed on the server.'),
      revision: revisionOf(response),
      text: typeof response.text === 'string' ? response.text : null,
      id: response.id ?? this._id,
    });
    return { acknowledged: false, reason: 'conflict', dirty: this.dirty };
  }

  /** Explicit resolution: adopt fetched server baseline, retaining the local draft. */
  rebase({ text, revision = null, etag, id = this._id } = {}) {
    textValue(text);
    if (id !== this._id || requireValid(inspect(text)).value.id !== id) {
      throw new ProfileDraftError('Cannot rebase a different profile identity.', { code: 'ID_MISMATCH' });
    }
    const nextRevision = revision ?? etag ?? null;
    if (nextRevision === null) throw new ProfileDraftError('Rebase requires the server revision.', { code: 'MISSING_REVISION' });
    this._epoch += 1;
    this._baseText = text;
    this._revision = nextRevision;
    this._isNew = false;
    this._conflict = null;
    return this.parse();
  }

  /** Load/import a document; invalid raw text is still retained for repair. */
  reset({ text = '', revision = null, etag, id } = {}) {
    textValue(text);
    this._epoch += 1; // Late responses from the previous document are ignored.
    this._editVersion += 1;
    this._text = text;
    this._baseText = text;
    this._revision = revision ?? etag ?? null;
    this._isNew = this._revision === null;
    const parsed = this.parse();
    this._id = id ?? (nonemptyId(parsed.value?.id) ? parsed.value.id : null);
    this._conflict = null;
    return parsed;
  }

  /** Discard to the latest acknowledged base. A server conflict still needs resolution. */
  discard() {
    // Increment even if content happens to match, preventing an in-flight save
    // from treating this explicit user decision as its unmodified submitted draft.
    this._editVersion += 1;
    this._text = this._baseText;
    return this.parse();
  }

  cloneAs(newId) {
    if (!nonemptyId(newId)) throw new ProfileDraftError('A new profile ID is required.', { code: 'MISSING_ID' });
    const text = changeField(this._text, ['id'], newId);
    return new ProfileDraft({ text, revision: null, id: newId });
  }
}
