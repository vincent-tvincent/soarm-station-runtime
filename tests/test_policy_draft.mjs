import test from 'node:test';
import assert from 'node:assert/strict';
import { ProfileDraft, ProfileDraftError } from '../web/policy-draft.js';

const source = `# Cup profile; keep this comment
id: cup_act # stable identity
label: "Cup placement" # operator label
model:
  path: models/cup/pretrained_model
cameras:
  observation.images.camera1: forward # trained global view
  observation.images.camera2: wrist
# Future plugin data is not owned by the form.
extension:
  tuning: [null, false, 0, ""]
  nested: {custom: 7}
`;
const open = (text = source) => new ProfileDraft({ text, id: 'cup_act', revision: '"r1"' });

test('reads and view switches retain verbatim raw text; form edits preserve comments and unknown fields', () => {
  const draft = open();
  assert.equal(draft.dirty, false);
  const parsed = draft.parse();
  parsed.value.extension.nested.custom = 900; // Returned data is not editor state.
  assert.equal(draft.getField(['extension', 'nested', 'custom']), 7);
  assert.equal(draft.text, source);
  assert.equal(draft.getField(['cameras', 'observation.images.camera1']), 'forward');
  draft.setField(['label'], 'Cup sorting');
  draft.setField(['cameras', 'observation.images.camera1'], 'overview');
  assert.match(draft.text, /# Cup profile; keep this comment/);
  assert.match(draft.text, /# operator label/);
  assert.match(draft.text, /# trained global view/);
  assert.match(draft.text, /# Future plugin data is not owned by the form\./);
  assert.deepEqual(draft.getField(['extension']), { tuning: [null, false, 0, ''], nested: { custom: 7 } });
  assert.equal(draft.getField(['cameras', 'observation.images.camera1']), 'overview');
  assert.equal(draft.dirty, true);
});

test('JSON raw documents share the same form state and preserve unknown fields', () => {
  const raw = '{ "id": "cup_act", "label": "A", "advanced": {"keep": [0, false, null]} }';
  const draft = open(raw);
  draft.parse();
  assert.equal(draft.text, raw);
  draft.setField('label', 'B');
  assert.deepEqual(draft.parse().value, { id: 'cup_act', label: 'B', advanced: { keep: [0, false, null] } });
  draft.setRaw('{"id":"cup_act","label":"Raw edit","extra":42}');
  assert.equal(draft.getField('label'), 'Raw edit');
  assert.equal(draft.getField('extra'), 42);
});

test('invalid raw syntax, duplicate keys and non-mapping roots remain visible with no last-valid payload', () => {
  for (const invalid of ['id: [broken', 'id: cup_act\nid: other\n', '[1, 2]', 'null', '---\nid: a\n---\nid: b']) {
    const draft = open();
    const result = draft.setRaw(invalid);
    assert.equal(draft.text, invalid);
    assert.equal(result.value, null);
    assert.ok(result.error);
    assert.ok(result.errors.length);
    assert.equal(draft.valid, false);
    assert.throws(() => draft.setField('label', 'X'), ProfileDraftError);
    assert.throws(() => draft.prepareSave(), ProfileDraftError);
    assert.equal(draft.text, invalid);
    draft.discard();
    assert.equal(draft.text, source);
    assert.equal(draft.dirty, false);
  }
});

test('save snapshot is immutable; late successful save preserves newer edits and updates baseline/ETag', () => {
  const draft = open();
  draft.setField('label', 'Submitted');
  const submitted = draft.prepareSave();
  assert.equal(Object.isFrozen(submitted), true);
  assert.equal(submitted.revision, '"r1"');
  assert.throws(() => { submitted.text = 'overwrite'; }, TypeError);
  draft.setField('label', 'Still editing');
  const current = draft.text;
  const result = draft.acknowledgeSave(submitted, { revision: '"r2"', text: submitted.text });
  assert.deepEqual(result, { acknowledged: true, replacedDraft: false, dirty: true });
  assert.equal(draft.text, current);
  assert.equal(draft.baseText, submitted.text);
  assert.equal(draft.revision, '"r2"');
  assert.equal(draft.prepareSave().revision, '"r2"');
  draft.discard();
  assert.equal(draft.getField('label'), 'Submitted');
  assert.equal(draft.dirty, false);
});

test('matching successful save accepts canonical response; a stale/duplicate acknowledgment cannot roll it back', () => {
  const draft = open();
  draft.setField('label', 'First');
  const first = draft.prepareSave();
  const serverText = `${first.text}\n# server annotation\n`;
  assert.equal(draft.acknowledgeSave(first, { etag: '"r2"', text: serverText }).replacedDraft, true);
  assert.equal(draft.text, serverText);
  assert.equal(draft.dirty, false);
  draft.setField('label', 'Second');
  const second = draft.prepareSave();
  draft.acknowledgeSave(second, { revision: '"r3"' });
  assert.equal(draft.acknowledgeSave(first, { revision: '"r2"' }).acknowledged, false);
  assert.equal(draft.revision, '"r3"');
  assert.equal(draft.getField('label'), 'Second');
  assert.equal(draft.dirty, false);
});

test('discard during pending save is not overwritten by its response', () => {
  const draft = open();
  draft.setField('label', 'In flight');
  const snapshot = draft.prepareSave();
  draft.discard();
  draft.acknowledgeSave(snapshot, { revision: '"r2"' });
  assert.equal(draft.text, source);
  assert.equal(draft.baseText, snapshot.text);
  assert.equal(draft.dirty, true); // Server saved the other value; this draft preserves the user decision.
});

test('reset/import isolates old in-flight saves even when the profile ID is unchanged', () => {
  const draft = open();
  draft.setField('label', 'Old request');
  const snapshot = draft.prepareSave();
  const reloaded = source.replace('Cup placement', 'Reloaded');
  draft.reset({ text: reloaded, revision: '"r8"', id: 'cup_act' });
  assert.equal(draft.acknowledgeSave(snapshot, { revision: '"r2"' }).acknowledged, false);
  assert.equal(draft.text, reloaded);
  assert.equal(draft.revision, '"r8"');
});

test('conflict preserves local raw text and original precondition; resolution is explicit', () => {
  const draft = open();
  draft.setField('label', 'Local choice');
  const snapshot = draft.prepareSave();
  const local = draft.text;
  const remote = source.replace('Cup placement', 'Other operator');
  draft.markConflict({ status: 412, revision: '"r2"', text: remote }, snapshot);
  assert.equal(draft.text, local);
  assert.equal(draft.baseText, source);
  assert.equal(draft.revision, '"r1"');
  assert.equal(draft.conflict.text, remote);
  assert.throws(() => draft.prepareSave(), { code: 'CONFLICT' });
  draft.discard();
  assert.equal(draft.text, source);
  assert.ok(draft.conflict); // Discarding local edits does not pretend the server conflict disappeared.
  draft.setRaw(local);
  draft.rebase({ text: remote, revision: '"r2"' });
  assert.equal(draft.text, local);
  assert.equal(draft.baseText, remote);
  assert.equal(draft.conflict, null);
  assert.equal(draft.prepareSave().revision, '"r2"');
  draft.discard();
  assert.equal(draft.text, remote);
});

test('late conflict after a successful newer save cannot restore conflict state', () => {
  const draft = open();
  draft.setField('label', 'A');
  const a = draft.prepareSave();
  draft.setField('label', 'B');
  const b = draft.prepareSave();
  draft.acknowledgeSave(b, { revision: '"r2"' });
  assert.equal(draft.markConflict({ revision: '"r2"' }, a).reason, 'stale');
  assert.equal(draft.conflict, null);
});

test('new clones retain advanced fields/comments and require a create precondition', () => {
  const draft = open();
  const cloned = draft.cloneAs('cup_act_copy');
  assert.equal(draft.id, 'cup_act');
  assert.equal(draft.text, source);
  assert.equal(cloned.id, 'cup_act_copy');
  assert.equal(cloned.getField('id'), 'cup_act_copy');
  assert.equal(cloned.isNew, true);
  assert.equal(cloned.dirty, true);
  assert.equal(cloned.prepareSave().revision, null);
  assert.match(cloned.text, /# stable identity/);
  assert.deepEqual(cloned.getField('extension'), draft.getField('extension'));
  cloned.discard();
  assert.equal(cloned.dirty, true); // An unsaved clone still needs to be created.
  const snapshot = cloned.prepareSave();
  cloned.acknowledgeSave(snapshot, { revision: '"new1"' });
  assert.equal(cloned.isNew, false);
  assert.equal(cloned.dirty, false);
});

test('raw identity changes, foreign save snapshots and incomplete responses cannot corrupt the baseline', () => {
  const draft = open();
  draft.setField('id', 'different');
  assert.throws(() => draft.prepareSave(), { code: 'ID_CHANGED' });
  draft.discard();
  const snapshot = draft.prepareSave();
  assert.throws(() => open().acknowledgeSave(snapshot, { revision: 'x' }), { code: 'UNKNOWN_SAVE' });
  assert.throws(() => draft.acknowledgeSave(snapshot, {}), { code: 'MISSING_REVISION' });
  assert.throws(() => draft.acknowledgeSave(snapshot, { revision: 'x', id: 'other' }), { code: 'ID_MISMATCH' });
  assert.equal(draft.baseText, source);
  assert.equal(draft.revision, '"r1"');
});

test('deleting optional fields keeps unrelated unknown data and absent deletion is a no-op', () => {
  const draft = open();
  const version = draft.editVersion;
  draft.deleteField(['not_present']);
  assert.equal(draft.editVersion, version);
  assert.equal(draft.text, source);
  draft.deleteField(['cameras', 'observation.images.camera2']);
  assert.equal(draft.getField(['cameras', 'observation.images.camera2'], 'missing'), 'missing');
  assert.equal(draft.getField(['extension', 'nested', 'custom']), 7);
  assert.match(draft.text, /# Future plugin data/);
});
