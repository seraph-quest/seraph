import assert from 'node:assert/strict';
import test from 'node:test';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { Composition, validateProfile } from '../src/composition.js';
import { decodeJson } from '../src/protocol.js';
import { PRESETS, presetDigest } from '../src/plugins/agent_presets/index.js';
import type { ScopedRequestClient } from '../src/contracts/client.js';

const profile = () => validateProfile(decodeJson(readFileSync(new URL('../../profile.json', import.meta.url))));
const ok = (value: unknown) => ({ status: 'succeeded', memory_status: 'no_learning', value });
const worker = PRESETS['specialist.file_worker']!;
const step = (overrides: Record<string, unknown> = {}) => ({ schema_version: 1, kind: 'child_admit', child_index: 0, max_children: 4,
  preset_id: 'specialist.file_worker', preset_digest: presetDigest(worker), ...overrides });

function owner(projection: unknown) {
  const calls: [string, unknown][] = [];
  const content = JSON.stringify(projection);
  const client: ScopedRequestClient = { isActive: () => true, request: async (method, input) => {
    calls.push([method, input]);
    if (method === 'artifacts.read') return ok({ artifact_ref: 'cordis-svc:step', digest: createHash('sha256').update(content).digest('hex'),
      size_bytes: Buffer.byteLength(content), content });
    if (method === 'tasks.checkpoint') return ok({ receipt_ref: 'cordis-turn:' + (input as { checkpoint_ref: string }).checkpoint_ref, revision: 2 });
    if (method === 'tasks.inspect') return ok({ job_ref: 'cordis-svc:step', revision: 5, state: 'running', artifact_refs: [] });
    return { status: 'blocked', reason_code: 'native_fixture_denied', memory_status: 'no_learning' };
  } };
  return { client, calls };
}

test('the delegation sequencer admits one child at its index and stages nothing else', async () => {
  const composition = new Composition(profile()); await composition.start();
  try {
    const fixture = owner(step({ child_index: 3 }));
    assert.deepEqual(await composition.invoke('delegation.startChild', { turn_ref: 'cordis-svc:step' }, fixture.client),
      ok({ job_ref: 'cordis-svc:step', revision: 5, state: 'running' }));
    assert.deepEqual(fixture.calls, [
      ['artifacts.read', { artifact_ref: 'cordis-svc:step', max_bytes: 65536, offset: 0 }],
      ['tasks.checkpoint', { checkpoint_ref: 'child_admit:3' }], ['tasks.inspect', { job_ref: 'cordis-svc:step' }]]);
  } finally { await composition.dispose(); }
});

test('a fifth child, a changed or non-child preset, or a malformed step is never admitted', async () => {
  const composition = new Composition(profile()); await composition.start();
  try {
    for (const [projection, reason] of [
        [step({ child_index: 4 }), 'delegation_transition_invalid'],
        [step({ max_children: 5 }), 'delegation_transition_invalid'],
        [step({ preset_digest: 'b'.repeat(64) }), 'agent_preset_changed'],
        [step({ preset_id: 'specialist.unknown' }), 'agent_preset_unavailable'],
        [step({ preset_id: 'chat.agent', preset_digest: presetDigest(PRESETS['chat.agent']!) }), 'agent_preset_kind_invalid'],
        [step({ task: 'read the secrets' }), 'delegation_step_invalid']] as const) {
      const fixture = owner(projection);
      assert.deepEqual(await composition.invoke('delegation.startChild', { turn_ref: 'cordis-svc:step' }, fixture.client),
        { status: 'blocked', reason_code: reason, memory_status: 'no_learning' });
      assert.deepEqual(fixture.calls.map(([method]) => method), ['artifacts.read']);
    }
  } finally { await composition.dispose(); }
});
