import { Context, Service, type Plugin } from 'cordis';
import { createHash } from 'node:crypto';
import type { SeraphResearch } from '../../contracts/research.js';
import { validateInput, validateResult, type Input, type Result } from '../../contracts/methods.js';
import type { ScopedRequestClient } from '../../contracts/client.js';
import type { Json } from '../../protocol.js';
import { canonicalJson } from '../../contracts/schema.js';

const blocked = (reason_code: string) => ({ status: 'blocked', reason_code, memory_status: 'no_learning' }) as const;
const STEPS = ['plan_queries', 'search_public', 'extract_sources', 'prepare_brief'];
const PLAN_KEYS = ['deadline_at', 'goal_id', 'goal_revision', 'grant_id', 'grant_revision', 'idempotency_key', 'issued_at', 'limits',
  'plan_id', 'programme_id', 'programme_revision', 'public_brief_digest', 'route_epoch', 'schema_version', 'steps', 'strategy_binding'];

/** Structural bound on provider output; Python's validator remains authoritative. */
function boundedPlan(plan: Json): string | undefined {
  if (!plan || typeof plan !== 'object' || Array.isArray(plan) || Object.keys(plan).sort().join(',') !== PLAN_KEYS.join(',')) return undefined;
  const steps = plan.steps;
  if (plan.schema_version !== 1 || !Array.isArray(steps) || steps.length !== 4
      || steps.some((step, index) => !step || typeof step !== 'object' || Array.isArray(step) || step.step_id !== STEPS[index])) return undefined;
  const content = canonicalJson(plan);
  return Buffer.byteLength(content, 'utf8') <= 65_536 ? content : undefined;
}

/** Research service: composes the injected planning provider and guardian consumer. */
export class SeraphResearchService extends Service implements SeraphResearch {
  constructor(ctx: Context) { super(ctx, 'seraphResearch'); }
  async buildPlan(input: Input<'research.buildPlan'>): Promise<Result<'research.buildPlan'>> {
    const request = validateInput('research.buildPlan', input);
    const planner = this.ctx.get('seraphResearchPlanner');
    // No built-in or Python fallback planner exists.
    if (!planner) return blocked('research_plan_provider_unavailable');
    const proposed = await planner.plan(request);
    if (proposed.status !== 'succeeded') return blocked(proposed.reason_code);
    const content = boundedPlan(proposed.plan);
    if (content === undefined) return blocked('research_plan_output_invalid');
    const staged = await this.ctx.seraphArtifacts.stage({ request_ref: 'research-plan', content,
      content_digest: createHash('sha256').update(content).digest('hex') });
    if (staged.status !== 'succeeded') return blocked(staged.reason_code);
    return validateResult('research.buildPlan', { status: 'succeeded', memory_status: 'no_learning',
      value: { plan_ref: staged.value.artifact_ref, revision: 1 } });
  }
  async executeAccepted(input: Input<'research.executeAccepted'>): Promise<Result<'research.executeAccepted'>> {
    const request = validateInput('research.executeAccepted', input);
    const consumer = this.ctx.get('seraphResearchConsumer');
    if (!consumer) return blocked('research_consumer_unavailable');
    return validateResult('research.executeAccepted', await consumer.execute(request));
  }
}
export function createPlugin(_client: ScopedRequestClient): Plugin.Object<Record<string, never>> {
  return { name: 'seraph.research.v1', provide: ['seraphResearch'], inject: ['seraphArtifacts'], apply(ctx) { new SeraphResearchService(ctx); } };
}
