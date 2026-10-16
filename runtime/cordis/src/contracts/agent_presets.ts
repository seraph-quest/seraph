import type { Json } from '../protocol.js';

export const DOMAIN = 'seraph.agent-presets.v1' as const;
/** One reviewed preset document plus its canonical digest; grants nothing. */
export interface ResolvedPreset { preset: Readonly<Record<string, Json>>; digest: string; }
export interface SeraphAgentPresets {
  resolve(preset_id: string): ResolvedPreset | undefined;
}
declare module 'cordis' { interface Context { seraphAgentPresets: SeraphAgentPresets; } }
