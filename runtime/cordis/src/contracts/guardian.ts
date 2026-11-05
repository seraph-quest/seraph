import type { Input, Result } from './methods.js';

export const DOMAIN = 'seraph.guardian.v1' as const;
export interface SeraphGuardian {
  startTurn(input: Input<'guardian.startTurn'>): Promise<Result<'guardian.startTurn'>>;
}
declare module 'cordis' { interface Context { seraphGuardian: SeraphGuardian; } }
