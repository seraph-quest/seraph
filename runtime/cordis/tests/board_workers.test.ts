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
const mail = PRESETS['worker.mail_reply_draft']!;
const step = (overrides: Record<string, unknown> = {}) => ({ schema_version: 1, kind: 'final_answer', step_index: 1, max_steps: 1,
  output_ref: 'turn-output:' + 'a'.repeat(48), preset_id: 'worker.mail_reply_draft', preset_digest: presetDigest(mail), ...overrides });

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

test('both worker presets are tool-less single-step JSON drafts on the existing route', () => {
  for (const id of ['worker.mail_reply_draft', 'worker.meeting_prep']) {
    const value = PRESETS[id]!;
    assert.deepEqual([value.kind, value.tools, value.max_steps, value.runtime_path, value.temperature, value.max_tokens, value.output_contract],
      ['worker', 'none', 1, 'strategist_agent', '0.2', '2048', 'json_object']);
  }
});

test('a worker draft step settles once and stages nothing else', async () => {
  const composition = new Composition(profile()); await composition.start();
  try {
    for (const preset_id of ['worker.mail_reply_draft', 'worker.meeting_prep']) {
      const fixture = owner(step({ preset_id, preset_digest: presetDigest(PRESETS[preset_id]!) }));
      assert.deepEqual(await composition.invoke('board-workers.startTurn', { turn_ref: 'cordis-svc:step' }, fixture.client),
        ok({ job_ref: 'cordis-svc:step', revision: 5, state: 'running' }));
      assert.deepEqual(fixture.calls, [
        ['artifacts.read', { artifact_ref: 'cordis-svc:step', max_bytes: 65536, offset: 0 }],
        ['tasks.checkpoint', { checkpoint_ref: 'settlement' }], ['tasks.inspect', { job_ref: 'cordis-svc:step' }]]);
    }
  } finally { await composition.dispose(); }
});

test('an unattended, interactive, unknown, changed or multi-step preset never settles a worker draft', async () => {
  const composition = new Composition(profile()); await composition.start();
  try {
    for (const [projection, reason] of [
        [step({ preset_id: 'worker.unknown' }), 'agent_preset_unavailable'],
        [step({ preset_digest: 'b'.repeat(64) }), 'agent_preset_changed'],
        [step({ preset_id: 'guardian.strategist', preset_digest: presetDigest(PRESETS['guardian.strategist']!) }), 'worker_preset_tools_denied'],
        [step({ preset_id: 'chat.agent', preset_digest: presetDigest(PRESETS['chat.agent']!) }), 'worker_preset_tools_denied'],
        [step({ kind: 'tool_calls' }), 'board_worker_transition_invalid'],
        [step({ step_index: 2 }), 'board_worker_transition_invalid'],
        [step({ tool_name: 'write_file' }), 'board_worker_step_invalid']] as const) {
      const fixture = owner(projection);
      assert.deepEqual(await composition.invoke('board-workers.startTurn', { turn_ref: 'cordis-svc:step' }, fixture.client),
        { status: 'blocked', reason_code: reason, memory_status: 'no_learning' });
      assert.deepEqual(fixture.calls.map(([method]) => method), ['artifacts.read']);
    }
  } finally { await composition.dispose(); }
});
