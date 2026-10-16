/** Guardian goal-research planning provider (C2.2 GoalResearchPlanSpecV1).
 *
 * Reads exactly one Python-staged, bounded planning request and returns an
 * inert plan: no authority, effects, URLs or inference. Python validates it
 * against the current programme, goal, grant, route, strategy and limits.
 */
import { Context, Service, type Plugin } from 'cordis';
import type { SeraphResearchPlanner, Blocked } from '../contracts/research.js';
import { validateInput, type Input } from '../contracts/methods.js';
import type { ScopedRequestClient } from '../contracts/client.js';
import type { Json } from '../protocol.js';

const STAGES = [
  ['plan_queries', 'guardian.query-plan.v1', [['queries', 'QueryPlan.v1']]],
  ['search_public', 'guardian.public-search.v1', [['manifest', 'SearchManifest.v1'], ['selection', 'SourceSelection.v1']]],
  ['extract_sources', 'source.public-extract.v1', [['snapshots', 'PublicSnapshots.v1']]],
  ['prepare_brief', 'guardian.prepare-brief.v1', [['brief', 'DiscoveryBrief.v1']]],
] as const;
const REQUEST_KEYS = ['deadline_at', 'goal_id', 'goal_revision', 'grant_id', 'grant_revision', 'idempotency_key', 'issued_at',
  'limits_ceiling', 'output_caps', 'plan_id', 'programme_id', 'programme_revision', 'public_brief_digest', 'public_brief_ref',
  'route_epoch', 'schema_version', 'strategy_binding'];
const blocked = (reason_code: string): Blocked => ({ status: 'blocked', reason_code, memory_status: 'no_learning' });
type Record_ = Record<string, Json>;
const isRecord = (value: unknown): value is Record_ => !!value && typeof value === 'object' && !Array.isArray(value);

export class GoalPlanProvider extends Service implements SeraphResearchPlanner {
  constructor(ctx: Context) { super(ctx, 'seraphResearchPlanner'); }
  async plan(input: Input<'research.buildPlan'>): Promise<{status: 'succeeded'; plan: Json} | Blocked> {
    const { task_ref } = validateInput('research.buildPlan', input);
    const staged = await this.ctx.seraphArtifacts.read({ artifact_ref: task_ref, max_bytes: 65_536, offset: 0 });
    if (staged.status !== 'succeeded') return blocked(staged.reason_code);
    let request: Json;
    try { request = JSON.parse(staged.value.content) as Json; } catch { return blocked('research_plan_request_invalid'); }
    if (!isRecord(request) || Object.keys(request).sort().join(',') !== REQUEST_KEYS.join(',') || request.schema_version !== 1
        || !isRecord(request.limits_ceiling) || !isRecord(request.output_caps) || !isRecord(request.public_brief_ref)) {
      return blocked('research_plan_request_invalid');
    }
    const caps = request.output_caps;
    const refs: Json[][] = [[request.public_brief_ref],
      [{ producer_step_id: 'plan_queries', output_slot: 'queries', json_pointer: '' }],
      [{ producer_step_id: 'search_public', output_slot: 'manifest', json_pointer: '' }, { producer_step_id: 'search_public', output_slot: 'selection', json_pointer: '' }],
      [{ producer_step_id: 'extract_sources', output_slot: 'snapshots', json_pointer: '' }]];
    const steps = STAGES.map(([step_id, capability_id, outputs], index) => ({
      step_id, capability_id, capability_version: 1, input_refs: refs[index]!,
      output_slots: outputs.map(([slot, artifact_type]) => ({ slot, artifact_type, max_bytes: caps[slot] ?? null })),
    }));
    // This provider plans at the reviewed ceiling; a narrower provider may lower it.
    return { status: 'succeeded', plan: {
      schema_version: 1, plan_id: request.plan_id, programme_id: request.programme_id, programme_revision: request.programme_revision,
      goal_id: request.goal_id, goal_revision: request.goal_revision, grant_id: request.grant_id, grant_revision: request.grant_revision,
      public_brief_digest: request.public_brief_digest, route_epoch: request.route_epoch, strategy_binding: request.strategy_binding,
      issued_at: request.issued_at, deadline_at: request.deadline_at, idempotency_key: request.idempotency_key,
      limits: { ...request.limits_ceiling }, steps,
    } as Json };
  }
}
export function createPlugin(_client: ScopedRequestClient): Plugin.Object<Record<string, never>> {
  return { name: 'seraph.goal-plan.v1', provide: ['seraphResearchPlanner'], inject: ['seraphArtifacts'], apply(ctx) { new GoalPlanProvider(ctx); } };
}
