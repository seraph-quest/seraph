import assert from 'node:assert/strict';
import test from 'node:test';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { Composition, validateProfile, type Profile } from '../src/composition.js';
import { decodeJson } from '../src/protocol.js';
import type { ScopedRequestClient } from '../src/contracts/client.js';

const sha = (value: string) => createHash('sha256').update(value).digest('hex');
const profile = () => validateProfile(decodeJson(readFileSync(new URL('../../profile.json', import.meta.url))));
const without = (id: string): Profile => validateProfile({ ...profile(), plugins: profile().plugins.filter(spec => spec.id !== id) } as never);
const ok = (value: unknown) => ({ status: 'succeeded', memory_status: 'no_learning', value });
const read = (artifact_ref: string, content: string) => ok({ artifact_ref, digest: sha(content), size_bytes: Buffer.byteLength(content), content });

const LIMITS = { max_queries: 3, max_results: 15, max_sources: 4, max_inference_requests: 4, max_wall_seconds: 300, max_search_seconds: 20,
  max_search_bytes: 524288, max_source_bytes: 262144, max_output_bytes: 65536, cost_limit_microusd: 500 };
const PLANNING = JSON.stringify({ schema_version: 1, plan_id: '0b5e5e6c-7f53-5d64-9a43-2c9b0e1f6a11', programme_id: 'a'.repeat(32), programme_revision: 1,
  goal_id: 'goal-1', goal_revision: 1, grant_id: 'a'.repeat(32), grant_revision: 1, public_brief_digest: 'b'.repeat(64),
  public_brief_ref: { artifact_id: 'art_brief', digest: 'b'.repeat(64), schema_version: 1 }, route_epoch: 1,
  strategy_binding: { status: 'none', reason: 'baseline' }, issued_at: '2026-10-15T00:00:00+00:00', deadline_at: '2026-10-15T00:05:00+00:00',
  idempotency_key: '0b5e5e6c-7f53-5d64-9a43-2c9b0e1f6a11', limits_ceiling: LIMITS,
  output_caps: { queries: 16384, manifest: 65536, selection: 8192, snapshots: 65536, brief: 65536 } });

/** Fixed in-memory native owner: records every crossing; no network. */
function nativeOwner(raw: Record<string, { media_type: string; body: string }>) {
  const calls: [string, unknown][] = [];
  const staged = new Map<string, string>();
  const sources = Object.keys(raw).map((artifact_ref, source_slot) => ({ source_slot, artifact_ref, acquisition_receipt_ref: `receipt-${source_slot}` }));
  const extraction = JSON.stringify({ schema_version: 1, job_ref: 'goal-discovery:job', sources });
  const client: ScopedRequestClient = { isActive: () => true, request: async (method, input) => {
    calls.push([method, input]);
    const value = input as Record<string, string>;
    if (method === 'artifacts.read') {
      if (value.artifact_ref === 'art_planning') return read('art_planning', PLANNING);
      if (value.artifact_ref === 'art_extraction') return read('art_extraction', extraction);
      if (staged.has(value.artifact_ref!)) return read(value.artifact_ref!, staged.get(value.artifact_ref!)!);
      const source = raw[value.artifact_ref!];
      const slot = Object.keys(raw).indexOf(value.artifact_ref!);
      if (source) return read(value.artifact_ref!, JSON.stringify({ schema_version: 1, source_slot: slot, ...source }));
    }
    if (method === 'artifacts.stage') {
      assert.equal(sha(value.content!), value.content_digest);
      const ref = `art_${value.request_ref!.replace(':', '_')}`;
      staged.set(ref, value.content!);
      return ok({ artifact_ref: ref, digest: sha(value.content!), size_bytes: Buffer.byteLength(value.content!) });
    }
    if (method === 'tasks.inspect') return ok({ job_ref: 'goal-discovery:job', revision: 3, state: 'running', artifact_refs: Object.keys(raw) });
    if (method === 'tasks.checkpoint') return ok({ receipt_ref: 'cordis-task:' + value.checkpoint_ref, revision: 4 });
    return { status: 'blocked', reason_code: 'native_fixture_denied', memory_status: 'no_learning' };
  } };
  return { client, calls, staged };
}

test('planning provider, research service and guardian consumer compose one accepted journey', async () => {
  const composition = new Composition(profile()); await composition.start();
  try {
    const owner = nativeOwner({ art_raw0: { media_type: 'text/html', body: '<h1>Release</h1><p>Shipped &amp; dated.</p>' },
      art_raw1: { media_type: 'application/atom+xml', body: '<feed><title>Log</title><entry><title>One</title></entry></feed>' } });
    const plan = await composition.invoke('research.buildPlan', { task_ref: 'art_planning' }, owner.client);
    assert.deepEqual(plan, { status: 'succeeded', memory_status: 'no_learning', value: { plan_ref: 'art_research-plan', revision: 1 } });
    const content = JSON.parse(owner.staged.get('art_research-plan')!);
    assert.deepEqual(content.steps.map((step: { step_id: string }) => step.step_id), ['plan_queries', 'search_public', 'extract_sources', 'prepare_brief']);
    assert.deepEqual(content.limits, LIMITS);
    assert.deepEqual(content.steps[0].input_refs, [{ artifact_id: 'art_brief', digest: 'b'.repeat(64), schema_version: 1 }]);
    const done = await composition.invoke('research.executeAccepted', { plan_ref: 'art_extraction' }, owner.client);
    assert.deepEqual(done, { status: 'succeeded', memory_status: 'no_learning', value: { job_ref: 'goal-discovery:job', revision: 3, state: 'running' } });
    assert.deepEqual(owner.calls.map(([method]) => method), ['artifacts.read', 'artifacts.stage', 'artifacts.read', 'tasks.inspect',
      'artifacts.read', 'artifacts.stage', 'artifacts.read', 'artifacts.read', 'artifacts.stage', 'artifacts.read', 'tasks.inspect']);
    const html = JSON.parse(owner.staged.get('art_snapshot_0')!);
    assert.equal(html.text, 'Release\nShipped & dated.');
    assert.equal(JSON.parse(owner.staged.get('art_snapshot_1')!).text, 'Feed: Log\nItem: One');
    assert.equal(html.provider_digest, composition.states().find(state => state.id === 'seraph.source-extraction.v1')!.digest);
  } finally { await composition.dispose(); }
});

test('rejected feed content is recorded per slot and never staged', async () => {
  const composition = new Composition(profile()); await composition.start();
  try {
    const owner = nativeOwner({ art_raw0: { media_type: 'application/rss+xml', body: '<!DOCTYPE r [<!ENTITY a "a">]><rss/>' } });
    const done = await composition.invoke('research.executeAccepted', { plan_ref: 'art_extraction' }, owner.client);
    assert.equal(done.status, 'succeeded');
    assert.deepEqual(owner.calls.find(([method]) => method === 'tasks.checkpoint'), ['tasks.checkpoint', { checkpoint_ref: 'source-rejected:0:source_feed_dtd_forbidden' }]);
    assert.equal(owner.staged.size, 0);
  } finally { await composition.dispose(); }
});

test('missing planner, consumer or source provider is an explicit block without fallback', async () => {
  for (const [id, method, input, reason] of [
    ['seraph.goal-plan.v1', 'research.buildPlan', { task_ref: 'art_planning' }, 'research_plan_provider_unavailable'],
    ['seraph.guardian-research.v1', 'research.executeAccepted', { plan_ref: 'art_extraction' }, 'research_consumer_unavailable'],
    ['seraph.source-extraction.v1', 'research.executeAccepted', { plan_ref: 'art_extraction' }, 'source_extraction_provider_unavailable'],
  ] as const) {
    const composition = new Composition(without(id)); await composition.start();
    try {
      const owner = nativeOwner({ art_raw0: { media_type: 'text/plain', body: 'x' } });
      assert.deepEqual(await composition.invoke(method, input as never, owner.client), { status: 'blocked', reason_code: reason, memory_status: 'no_learning' });
      assert.ok(!owner.calls.some(([name]) => name === 'artifacts.stage'), `${id} absence must stage nothing`);
    } finally { await composition.dispose(); }
  }
  // An unmapped format is a whole-step block, not a silently dropped source.
  const composition = new Composition(profile()); await composition.start();
  try {
    const owner = nativeOwner({ art_raw0: { media_type: 'application/pdf', body: '%PDF' } });
    assert.deepEqual(await composition.invoke('research.executeAccepted', { plan_ref: 'art_extraction' }, owner.client),
      { status: 'blocked', reason_code: 'source_format_provider_unavailable', memory_status: 'no_learning' });
  } finally { await composition.dispose(); }
});
