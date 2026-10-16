import type { Input, Result } from './methods.js';
import type { Json } from '../protocol.js';

export const DOMAIN = 'seraph.research.v1' as const;
export interface SeraphResearch {
  buildPlan(input: Input<'research.buildPlan'>): Promise<Result<'research.buildPlan'>>;
  executeAccepted(input: Input<'research.executeAccepted'>): Promise<Result<'research.executeAccepted'>>;
}
export type Blocked = {status: 'blocked'; reason_code: string; memory_status: 'no_learning'};
/** Planning provider: reads one bounded staged request and returns an inert plan. */
export interface SeraphResearchPlanner {
  plan(input: Input<'research.buildPlan'>): Promise<{status: 'succeeded'; plan: Json} | Blocked>;
}
/** Guardian consumer: executes one accepted plan step through task/artifact services. */
export interface SeraphResearchConsumer {
  execute(input: Input<'research.executeAccepted'>): Promise<Result<'research.executeAccepted'>>;
}
declare module 'cordis' { interface Context {
  seraphResearch: SeraphResearch; seraphResearchPlanner: SeraphResearchPlanner; seraphResearchConsumer: SeraphResearchConsumer;
} }
