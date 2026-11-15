/** Guardian sequencer (#1020): one decision for an unattended guardian turn.
 *
 * Scheduled guardian work (the strategist decision, the reports and memory
 * consolidation) runs with no operator present. Python makes exactly today's
 * tool-less model call; this sequencer then reads the step's sealed
 * projection and may stage only ``settlement``, and only for a reviewed
 * unattended preset with no tools and a single step. It sees no text and holds
 * no authority.
 */
import { Context, Service, type Plugin } from 'cordis';
import type { SeraphGuardian } from '../../contracts/guardian.js';
import { validateInput, validateResult, type Input, type Result } from '../../contracts/methods.js';
import type { ScopedRequestClient } from '../../contracts/client.js';

const blocked = (reason_code: string) => ({ status: 'blocked', reason_code, memory_status: 'no_learning' }) as const;
const KEYS = 'kind,max_steps,output_ref,preset_digest,preset_id,schema_version,step_index';

interface Projection { kind: string; step_index: number; max_steps: number; preset_id: string; preset_digest: string; }
function projection(content: string): Projection | undefined {
  try {
    const value = JSON.parse(content) as Record<string, unknown>;
    if (!value || typeof value !== 'object' || Object.keys(value).sort().join(',') !== KEYS || value.schema_version !== 1
        || typeof value.output_ref !== 'string' || typeof value.preset_id !== 'string' || typeof value.preset_digest !== 'string'
        || typeof value.kind !== 'string' || !Number.isSafeInteger(value.step_index) || !Number.isSafeInteger(value.max_steps)) return undefined;
    return value as unknown as Projection;
  } catch { return undefined; }
}

/** The only unattended transition: one tool-less step that settles. */
export function decide(step: Projection): string[] | undefined {
  return step.kind === 'final_answer' && step.step_index === 1 && step.max_steps === 1 ? ['settlement'] : undefined;
}

export class SeraphGuardianService extends Service implements SeraphGuardian {
  constructor(ctx: Context, _client: ScopedRequestClient) { super(ctx, 'seraphGuardian'); }
  async startTurn(input: Input<'guardian.startTurn'>): Promise<Result<'guardian.startTurn'>> {
    const { turn_ref } = validateInput('guardian.startTurn', input);
    const read = await this.ctx.seraphArtifacts.read({ artifact_ref: turn_ref, max_bytes: 65_536, offset: 0 });
    if (read.status !== 'succeeded') return blocked(read.reason_code);
    const step = projection(read.value.content);
    if (!step) return blocked('guardian_turn_step_invalid');
    const resolved = this.ctx.seraphAgentPresets.resolve(step.preset_id);
    if (!resolved) return blocked('agent_preset_unavailable');
    if (resolved.digest !== step.preset_digest) return blocked('agent_preset_changed');
    if (resolved.preset.kind !== 'unattended' || resolved.preset.tools !== 'none' || resolved.preset.max_steps !== 1) {
      return blocked('unattended_preset_tools_denied');
    }
    const decisions = decide(step);
    if (!decisions) return blocked('guardian_turn_transition_invalid');
    for (const checkpoint_ref of decisions) {
      const staged = await this.ctx.seraphTasks.checkpoint({ checkpoint_ref });
      if (staged.status !== 'succeeded') return blocked(staged.reason_code);
    }
    const job = await this.ctx.seraphTasks.inspect({ job_ref: turn_ref });
    if (job.status !== 'succeeded') return blocked(job.reason_code);
    return validateResult('guardian.startTurn', { status: 'succeeded', memory_status: 'no_learning',
      value: { job_ref: job.value.job_ref, revision: job.value.revision, state: job.value.state } });
  }
}
export function createPlugin(client: ScopedRequestClient): Plugin.Object<Record<string, never>> {
  return { name: 'seraph.guardian.v1', provide: ['seraphGuardian'], inject: ['seraphTasks', 'seraphArtifacts', 'seraphAgentPresets'],
    apply(ctx) { new SeraphGuardianService(ctx, client); } };
}
