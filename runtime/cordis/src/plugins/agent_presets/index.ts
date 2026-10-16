/** Reviewed agent presets (#1020): prompt sections, context purpose, model
 * parameters, step bounds and tool policy for each kind of turn.
 *
 * A preset is data. It never grants a tool or an authority: Python intersects
 * the declared tool policy with the turn's original authority, and holds its
 * own identical table. Each step projection names the preset and its digest,
 * and the sequencers refuse a step whose preset is unknown, of the wrong kind
 * or differs from this reviewed copy.
 */
import { createHash } from 'node:crypto';
import { Context, Service, type Plugin } from 'cordis';
import type { ResolvedPreset, SeraphAgentPresets } from '../../contracts/agent_presets.js';
import type { ScopedRequestClient } from '../../contracts/client.js';
import type { Json } from '../../protocol.js';

type Preset = Record<string, Json>;
const chatSections = ['base_instructions', 'guardian_state', 'observer_context', 'user_identity', 'relevant_memories', 'active_skills', 'conversation_history'];
const preset = (preset_id: string, kind: string, context_purpose: string, runtime_path: string, temperature: string,
                max_tokens: string, max_steps: Json, tools: string, prompt_sections: string[], output_contract: string): Preset =>
  ({ schema_version: 1, preset_id, kind, context_purpose, runtime_path, temperature, max_tokens, max_steps, tools,
     prompt_sections, output_contract });
const guardian = (preset_id: string, runtime_path: string, temperature: string, max_tokens: string, context_purpose = 'guardian_report') =>
  preset(preset_id, 'unattended', context_purpose, runtime_path, temperature, max_tokens, 1, 'none', ['job_prompt'], 'text');

/** Mirror of backend/src/agent/presets.py; the two tables must be identical. */
export const PRESETS: Readonly<Record<string, Preset>> = Object.freeze({
  'chat.agent': preset('chat.agent', 'interactive', 'chat', 'chat_agent', 'settings', 'settings', 'settings.agent_max_steps',
    'chat_surface', chatSections, 'final_answer'),
  'chat.direct': preset('chat.direct', 'interactive', 'direct', 'chat_agent', 'settings', 'min(settings,512)', 1, 'none',
    ['direct_system', 'task_continuity'], 'text'),
  'chat.onboarding': preset('chat.onboarding', 'interactive', 'onboarding', 'onboarding_agent', '0.8', 'settings',
    'settings.agent_max_steps', 'onboarding_surface', ['onboarding_instructions', 'explicit_url_scope'], 'final_answer'),
  'guardian.strategist': guardian('guardian.strategist', 'strategist_agent', '0.2', '512', 'guardian_decision'),
  'guardian.daily_briefing': guardian('guardian.daily_briefing', 'daily_briefing', '0.6', '512'),
  'guardian.evening_review': guardian('guardian.evening_review', 'evening_review', '0.6', '512'),
  'guardian.activity_digest': guardian('guardian.activity_digest', 'activity_digest', '0.6', '768'),
  'guardian.weekly_activity_review': guardian('guardian.weekly_activity_review', 'weekly_activity_review', '0.6', '1024'),
  'guardian.end_of_day_goal_report': guardian('guardian.end_of_day_goal_report', 'end_of_day_goal_report', '0.4', '900'),
  'guardian.screenshot_observation_digest': guardian('guardian.screenshot_observation_digest', 'screenshot_observation_digest', '0.2', '800'),
  'guardian.memory_consolidation': guardian('guardian.memory_consolidation', 'session_consolidation', '0.3', '1024', 'memory'),
});

/** Python's ``json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)``; kept local so the
 * preset table imports no service schema (it is read standalone by Python's parity check). */
export function canonicalJson(value: Json): string {
  if (Array.isArray(value)) return '[' + value.map(canonicalJson).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + canonicalJson(value[key]!)).join(',') + '}';
  return JSON.stringify(value);
}
export function presetDigest(value: Preset): string { return createHash('sha256').update(canonicalJson(value)).digest('hex'); }

export class SeraphAgentPresetsService extends Service implements SeraphAgentPresets {
  constructor(ctx: Context) { super(ctx, 'seraphAgentPresets'); }
  resolve(preset_id: string): ResolvedPreset | undefined {
    if (typeof preset_id !== 'string' || !Object.hasOwn(PRESETS, preset_id)) return undefined;
    const value = PRESETS[preset_id]!;
    return { preset: value, digest: presetDigest(value) };
  }
}
export function createPlugin(_client: ScopedRequestClient): Plugin.Object<Record<string, never>> {
  return { name: 'seraph.agent-presets.v1', provide: ['seraphAgentPresets'], apply(ctx) { new SeraphAgentPresetsService(ctx); } };
}
