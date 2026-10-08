import type { Input, Result } from './methods.js';

export const DOMAIN = 'seraph.research.v1' as const;
export interface SeraphResearch {
  buildPlan(input: Input<'research.buildPlan'>): Promise<Result<'research.buildPlan'>>;
  executeAccepted(input: Input<'research.executeAccepted'>): Promise<Result<'research.executeAccepted'>>;
}
declare module 'cordis' { interface Context { seraphResearch: SeraphResearch; } }
