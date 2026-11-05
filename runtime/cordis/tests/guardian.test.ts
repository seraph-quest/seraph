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
const strategist = PRESETS['guardian.strategist']!;
const step = (overrides: Record<string, unknown> = {}) => ({ schema_version: 1, kind: 'final_answer', step_index: 1, max_steps: 1,
  output_ref: 'turn-output:' + 'a'.repeat(48), preset_id: 'guardian.strategist', preset_digest: presetDigest(strategist), ...overrides });

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

test('the preset table is the reviewed mirror of the Python table', () => {
  // Every unattended preset is tool-less and single-step; interactive presets keep their policies.
  for (const [id, value] of Object.entries(PRESETS)) {
    assert.equal(value.preset_id, id);
    if (value.kind === 'unattended') { assert.equal(value.tools, 'none'); assert.equal(value.max_steps, 1); }
  }
  assert.equal(presetDigest(strategist), createHash('sha256').update(JSON.stringify(Object.fromEntries(Object.entries(strategist).sort()))).digest('hex'));
});

test('an unattended step settles once and stages nothing else', async () => {
  const composition = new Composition(profile()); await composition.start();
  try {
    const fixture = owner(step());
    assert.deepEqual(await composition.invoke('guardian.startTurn', { turn_ref: 'cordis-svc:step' }, fixture.client),
      ok({ job_ref: 'cordis-svc:step', revision: 5, state: 'running' }));
    assert.deepEqual(fixture.calls, [
      ['artifacts.read', { artifact_ref: 'cordis-svc:step', max_bytes: 65536, offset: 0 }],
      ['tasks.checkpoint', { checkpoint_ref: 'settlement' }], ['tasks.inspect', { job_ref: 'cordis-svc:step' }]]);
  } finally { await composition.dispose(); }
});

test('an interactive, unknown, changed or multi-step preset never settles an unattended turn', async () => {
  const composition = new Composition(profile()); await composition.start();
  try {
    for (const [projection, reason] of [
        [step({ preset_id: 'guardian.unknown' }), 'agent_preset_unavailable'],
        [step({ preset_digest: 'b'.repeat(64) }), 'agent_preset_changed'],
        [step({ preset_id: 'chat.agent', preset_digest: presetDigest(PRESETS['chat.agent']!) }), 'unattended_preset_tools_denied'],
        [step({ kind: 'tool_calls' }), 'guardian_turn_transition_invalid'],
        [step({ step_index: 2 }), 'guardian_turn_transition_invalid'],
        [step({ tool_name: 'write_file' }), 'guardian_turn_step_invalid']] as const) {
      const fixture = owner(projection);
      assert.deepEqual(await composition.invoke('guardian.startTurn', { turn_ref: 'cordis-svc:step' }, fixture.client),
        { status: 'blocked', reason_code: reason, memory_status: 'no_learning' });
      assert.deepEqual(fixture.calls.map(([method]) => method), ['artifacts.read']);
    }
  } finally { await composition.dispose(); }
});
