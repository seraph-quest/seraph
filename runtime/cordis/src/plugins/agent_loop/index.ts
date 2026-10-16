/** Agent-loop sequencer (#1019): one short decision per chat-turn step.
 *
 * Python owns the turn: the model call, the parse of its sealed output, tool
 * execution, approvals and the transcript. The child reads the step's sealed
 * projection, applies the loop's transitions and step budget, and stages the
 * next step as finite checkpoint decisions. Python accepts the decisions only
 * if they are exactly its own parse; the child never sees text, a tool name or
 * an argument, and holds no authority.
 */
import { Context, Service, type Plugin } from 'cordis';
import type { SeraphAgentLoop } from '../../contracts/agent_loop.js';
import '../../contracts/agent_presets.js';
import { validateInput, validateResult, type Input, type Result } from '../../contracts/methods.js';
import type { ScopedRequestClient } from '../../contracts/client.js';
import { NativeProxy } from '../proxy.js';

const blocked = (reason_code: string) => ({ status: 'blocked', reason_code, memory_status: 'no_learning' }) as const;
export const MAX_TOOL_CALLS = 12;
const KINDS = ['tool_calls', 'final_answer', 'parse_error', 'max_steps'] as const;
type Kind = typeof KINDS[number];
interface Projection { kind: Kind; call_count: number; step_index: number; max_steps: number; preset_id: string; preset_digest: string; }

function projection(content: string): Projection | undefined {
  try {
    const value = JSON.parse(content) as Record<string, unknown>;
    if (!value || typeof value !== 'object'
        || Object.keys(value).sort().join(',') !== 'call_count,kind,max_steps,output_ref,preset_digest,preset_id,schema_version,step_index'
        || value.schema_version !== 1 || !KINDS.includes(value.kind as Kind) || typeof value.output_ref !== 'string'
        || typeof value.preset_id !== 'string' || typeof value.preset_digest !== 'string'
        || !Number.isSafeInteger(value.call_count) || !Number.isSafeInteger(value.step_index) || !Number.isSafeInteger(value.max_steps)) return undefined;
    const call_count = value.call_count as number, step_index = value.step_index as number, max_steps = value.max_steps as number;
    if (call_count < 0 || call_count > MAX_TOOL_CALLS || (value.kind === 'tool_calls') !== (call_count > 0)
        || max_steps < 1 || step_index < 1 || step_index > max_steps + 1) return undefined;
    return { kind: value.kind as Kind, call_count, step_index, max_steps, preset_id: value.preset_id, preset_digest: value.preset_digest };
  } catch { return undefined; }
}

/** The step must belong to a reviewed interactive preset whose bounds it keeps (#1020). */
function presetDenial(ctx: Context, step: Projection): string | undefined {
  const resolved = ctx.seraphAgentPresets.resolve(step.preset_id);
  if (!resolved) return 'agent_preset_unavailable';
  if (resolved.digest !== step.preset_digest) return 'agent_preset_changed';
  const { kind, max_steps, tools } = resolved.preset;
  if (kind !== 'interactive') return 'agent_preset_kind_invalid';
  if (typeof max_steps === 'number' && step.max_steps !== max_steps) return 'agent_preset_bound_exceeded';
  if (tools === 'none' && step.kind === 'tool_calls') return 'agent_preset_tools_denied';
  return undefined;
}

/** The loop's transition table: a step's sealed outcome -> the decisions that follow it. */
export function decide(step: Projection): string[] | undefined {
  if (step.kind === 'max_steps') return step.step_index > step.max_steps ? ['final_call'] : undefined;
  if (step.step_index > step.max_steps) return undefined;
  if (step.kind === 'final_answer') return ['settlement'];
  if (step.kind === 'parse_error') return ['continue'];
  return Array.from({ length: step.call_count }, (_, index) => `tool_intent:${index}`);
}

export class SeraphAgentLoopService extends NativeProxy implements SeraphAgentLoop {
  constructor(ctx: Context, client: ScopedRequestClient) { super(ctx, 'seraphAgentLoop', client); }
  async startTurn(input: Input<'agent-loop.startTurn'>): Promise<Result<'agent-loop.startTurn'>> {
    const { turn_ref } = validateInput('agent-loop.startTurn', input);
    const read = await this.ctx.seraphArtifacts.read({ artifact_ref: turn_ref, max_bytes: 65_536, offset: 0 });
    if (read.status !== 'succeeded') return blocked(read.reason_code);
    const step = projection(read.value.content);
    if (!step) return blocked('agent_turn_step_invalid');
    const denial = presetDenial(this.ctx, step);
    if (denial) return blocked(denial);
    const decisions = decide(step);
    if (!decisions) return blocked('agent_turn_transition_invalid');
    for (const checkpoint_ref of decisions) {
      const staged = await this.ctx.seraphTasks.checkpoint({ checkpoint_ref });
      if (staged.status !== 'succeeded') return blocked(staged.reason_code);
    }
    const job = await this.ctx.seraphTasks.inspect({ job_ref: turn_ref });
    if (job.status !== 'succeeded') return blocked(job.reason_code);
    return validateResult('agent-loop.startTurn', { status: 'succeeded', memory_status: 'no_learning',
      value: { job_ref: job.value.job_ref, revision: job.value.revision, state: job.value.state } });
  }
  // Python owns turn cancellation and inspection; these stay typed native forwards.
  cancelTurn(input: Input<'agent-loop.cancelTurn'>): Promise<Result<'agent-loop.cancelTurn'>> { return this.forward('agent-loop.cancelTurn', input); }
  inspectTurn(input: Input<'agent-loop.inspectTurn'>): Promise<Result<'agent-loop.inspectTurn'>> { return this.forward('agent-loop.inspectTurn', input); }
}
/** Trusted composition injects the original invocation-scoped transport. */
export function createPlugin(client: ScopedRequestClient): Plugin.Object<Record<string, never>> {
  return { name: 'seraph.agent-loop.v1', provide: ['seraphAgentLoop'], inject: ['seraphTasks', 'seraphArtifacts', 'seraphAgentPresets'],
    apply(ctx) { new SeraphAgentLoopService(ctx, client); } };
}
