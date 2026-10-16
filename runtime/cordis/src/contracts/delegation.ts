import type { Input, Result } from './methods.js';

export const DOMAIN = 'seraph.delegation.v1' as const;
export interface SeraphDelegation {
  startChild(input: Input<'delegation.startChild'>): Promise<Result<'delegation.startChild'>>;
}
declare module 'cordis' { interface Context { seraphDelegation: SeraphDelegation; } }
