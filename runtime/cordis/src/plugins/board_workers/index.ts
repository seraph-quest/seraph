/** Board worker sequencer (#1020): one decision for a WorkBoard draft turn.
 *
 * The Mail reply and meeting-prep drafts are the dispatcher's only inline
 * model calls. Python makes exactly today's tool-less JSON call; this
 * sequencer then reads the step's sealed projection and may stage only
 * ``settlement``, and only for a reviewed worker preset with no tools and a
 * single step. It sees no text and holds no authority.
 */
import { Context, Service, type Plugin } from 'cordis';
import type { SeraphBoardWorkers } from '../../contracts/board_workers.js';
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

/** The only worker transition: one tool-less step that settles. */
export function decide(step: Projection): string[] | undefined {
  return step.kind === 'final_answer' && step.step_index === 1 && step.max_steps === 1 ? ['settlement'] : undefined;
}

export class SeraphBoardWorkersService extends Service implements SeraphBoardWorkers {
  constructor(ctx: Context, _client: ScopedRequestClient) { super(ctx, 'seraphBoardWorkers'); }
  async startTurn(input: Input<'board-workers.startTurn'>): Promise<Result<'board-workers.startTurn'>> {
    const { turn_ref } = validateInput('board-workers.startTurn', input);
    const read = await this.ctx.seraphArtifacts.read({ artifact_ref: turn_ref, max_bytes: 65_536, offset: 0 });
    if (read.status !== 'succeeded') return blocked(read.reason_code);
    const step = projection(read.value.content);
    if (!step) return blocked('board_worker_step_invalid');
    const resolved = this.ctx.seraphAgentPresets.resolve(step.preset_id);
    if (!resolved) return blocked('agent_preset_unavailable');
    if (resolved.digest !== step.preset_digest) return blocked('agent_preset_changed');
    if (resolved.preset.kind !== 'worker' || resolved.preset.tools !== 'none' || resolved.preset.max_steps !== 1) {
      return blocked('worker_preset_tools_denied');
    }
    const decisions = decide(step);
    if (!decisions) return blocked('board_worker_transition_invalid');
    for (const checkpoint_ref of decisions) {
      const staged = await this.ctx.seraphTasks.checkpoint({ checkpoint_ref });
      if (staged.status !== 'succeeded') return blocked(staged.reason_code);
    }
    const job = await this.ctx.seraphTasks.inspect({ job_ref: turn_ref });
    if (job.status !== 'succeeded') return blocked(job.reason_code);
    return validateResult('board-workers.startTurn', { status: 'succeeded', memory_status: 'no_learning',
      value: { job_ref: job.value.job_ref, revision: job.value.revision, state: job.value.state } });
  }
}
export function createPlugin(client: ScopedRequestClient): Plugin.Object<Record<string, never>> {
  return { name: 'seraph.board-workers.v1', provide: ['seraphBoardWorkers'], inject: ['seraphTasks', 'seraphArtifacts', 'seraphAgentPresets'],
    apply(ctx) { new SeraphBoardWorkersService(ctx, client); } };
}
