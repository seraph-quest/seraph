import assert from 'node:assert/strict';
import test from 'node:test';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { Composition, validateProfile } from '../src/composition.js';
import { decodeJson } from '../src/protocol.js';
import { decide, MAX_TOOL_CALLS } from '../src/plugins/agent_loop/index.js';
import type { ScopedRequestClient } from '../src/contracts/client.js';

const profile = () => validateProfile(decodeJson(readFileSync(new URL('../../profile.json', import.meta.url))));
const ok = (value: unknown) => ({ status: 'succeeded', memory_status: 'no_learning', value });
const step = (kind: string, call_count = 0, step_index = 1, max_steps = 10) =>
  ({ schema_version: 1, kind, call_count, step_index, max_steps, output_ref: 'turn-output:' + 'a'.repeat(48) });

/** Fixed in-memory Python owner for one step job: serves the projection, records decisions. */
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

test('the transition table settles only on final_answer and keeps errors as observations', () => {
  assert.deepEqual(decide(step('final_answer') as never), ['settlement']);
  assert.deepEqual(decide(step('parse_error') as never), ['continue']);
  assert.deepEqual(decide(step('tool_calls', 3) as never), ['tool_intent:0', 'tool_intent:1', 'tool_intent:2']);
  assert.deepEqual(decide(step('max_steps', 0, 11, 10) as never), ['final_call']);
  assert.equal(decide(step('max_steps', 0, 10, 10) as never), undefined);
  assert.equal(decide(step('final_answer', 0, 11, 10) as never), undefined);
});

test('a step reads only its sealed projection, stages its decisions in order and reports the job', async () => {
  const composition = new Composition(profile()); await composition.start();
  try {
    const fixture = owner(step('tool_calls', 2));
    const done = await composition.invoke('agent-loop.startTurn', { turn_ref: 'cordis-svc:step' }, fixture.client);
    assert.deepEqual(done, ok({ job_ref: 'cordis-svc:step', revision: 5, state: 'running' }));
    assert.deepEqual(fixture.calls, [
      ['artifacts.read', { artifact_ref: 'cordis-svc:step', max_bytes: 65536, offset: 0 }],
      ['tasks.checkpoint', { checkpoint_ref: 'tool_intent:0' }], ['tasks.checkpoint', { checkpoint_ref: 'tool_intent:1' }],
      ['tasks.inspect', { job_ref: 'cordis-svc:step' }]]);
  } finally { await composition.dispose(); }
});

test('malformed, oversized or impossible projections block before any decision', async () => {
  const composition = new Composition(profile()); await composition.start();
  try {
    for (const [projection, reason] of [
        [{ ...step('tool_calls', 1), tool_name: 'write_file' }, 'agent_turn_step_invalid'],
        [step('tool_calls', MAX_TOOL_CALLS + 1), 'agent_turn_step_invalid'],
        [step('final_answer', 1), 'agent_turn_step_invalid'],
        [step('max_steps', 0, 3, 10), 'agent_turn_transition_invalid']] as const) {
      const fixture = owner(projection);
      assert.deepEqual(await composition.invoke('agent-loop.startTurn', { turn_ref: 'cordis-svc:step' }, fixture.client),
        { status: 'blocked', reason_code: reason, memory_status: 'no_learning' });
      assert.deepEqual(fixture.calls.map(([method]) => method), ['artifacts.read']);
    }
  } finally { await composition.dispose(); }
});
