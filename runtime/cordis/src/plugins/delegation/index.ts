/** Delegation sequencer (#1020 PR2): admits one delegated child of a chat turn.
 *
 * Python parses the parent's managed-agent call, checks depth, the per-turn cap
 * and the deadline, and only then asks this sequencer. It reads the step's
 * sealed projection (child index, cap, the child's reviewed preset) and may
 * stage only ``child_admit:<index>``. It sees no task text, tool name or
 * argument and holds no authority; the child's own steps then run on the
 * agent-loop sequencer.
 */
import { Context, Service, type Plugin } from 'cordis';
import type { SeraphDelegation } from '../../contracts/delegation.js';
import '../../contracts/agent_presets.js';
import { validateInput, validateResult, type Input, type Result } from '../../contracts/methods.js';
import type { ScopedRequestClient } from '../../contracts/client.js';

const blocked = (reason_code: string) => ({ status: 'blocked', reason_code, memory_status: 'no_learning' }) as const;
const KEYS = 'child_index,kind,max_children,preset_digest,preset_id,schema_version';
/** The per-turn cap; mirrored by Python's MAX_CHILDREN. */
export const MAX_CHILDREN = 4;

interface Projection { kind: string; child_index: number; max_children: number; preset_id: string; preset_digest: string; }
function projection(content: string): Projection | undefined {
  try {
    const value = JSON.parse(content) as Record<string, unknown>;
    if (!value || typeof value !== 'object' || Object.keys(value).sort().join(',') !== KEYS || value.schema_version !== 1
        || typeof value.preset_id !== 'string' || typeof value.preset_digest !== 'string' || typeof value.kind !== 'string'
        || !Number.isSafeInteger(value.child_index) || !Number.isSafeInteger(value.max_children)) return undefined;
    return value as unknown as Projection;
  } catch { return undefined; }
}

/** The only transition: admit the child at this index, within the cap. */
export function decide(step: Projection): string[] | undefined {
  return step.kind === 'child_admit' && step.max_children === MAX_CHILDREN && step.child_index >= 0
    && step.child_index < MAX_CHILDREN ? [`child_admit:${step.child_index}`] : undefined;
}

export class SeraphDelegationService extends Service implements SeraphDelegation {
  constructor(ctx: Context, _client: ScopedRequestClient) { super(ctx, 'seraphDelegation'); }
  async startChild(input: Input<'delegation.startChild'>): Promise<Result<'delegation.startChild'>> {
    const { turn_ref } = validateInput('delegation.startChild', input);
    const read = await this.ctx.seraphArtifacts.read({ artifact_ref: turn_ref, max_bytes: 65_536, offset: 0 });
    if (read.status !== 'succeeded') return blocked(read.reason_code);
    const step = projection(read.value.content);
    if (!step) return blocked('delegation_step_invalid');
    const resolved = this.ctx.seraphAgentPresets.resolve(step.preset_id);
    if (!resolved) return blocked('agent_preset_unavailable');
    if (resolved.digest !== step.preset_digest) return blocked('agent_preset_changed');
    if (resolved.preset.kind !== 'child') return blocked('agent_preset_kind_invalid');
    const decisions = decide(step);
    if (!decisions) return blocked('delegation_transition_invalid');
    for (const checkpoint_ref of decisions) {
      const staged = await this.ctx.seraphTasks.checkpoint({ checkpoint_ref });
      if (staged.status !== 'succeeded') return blocked(staged.reason_code);
    }
    const job = await this.ctx.seraphTasks.inspect({ job_ref: turn_ref });
    if (job.status !== 'succeeded') return blocked(job.reason_code);
    return validateResult('delegation.startChild', { status: 'succeeded', memory_status: 'no_learning',
      value: { job_ref: job.value.job_ref, revision: job.value.revision, state: job.value.state } });
  }
}
export function createPlugin(client: ScopedRequestClient): Plugin.Object<Record<string, never>> {
  return { name: 'seraph.delegation.v1', provide: ['seraphDelegation'], inject: ['seraphTasks', 'seraphArtifacts', 'seraphAgentPresets'],
    apply(ctx) { new SeraphDelegationService(ctx, client); } };
}
