import type { Input, Result } from './methods.js';

export const DOMAIN = 'seraph.goals.v1' as const;
export interface SeraphGoals {
  read(input: Input<'goals.read'>): Promise<Result<'goals.read'>>;
}
declare module 'cordis' { interface Context { seraphGoals: SeraphGoals; } }
