import type { Input, Result } from './methods.js';

export const DOMAIN = 'seraph.memory.v1' as const;
export interface SeraphMemory {
  retrieve(input: Input<'memory.retrieve'>): Promise<Result<'memory.retrieve'>>;
  propose(input: Input<'memory.propose'>): Promise<Result<'memory.propose'>>;
  applyReviewed(input: Input<'memory.applyReviewed'>): Promise<Result<'memory.applyReviewed'>>;
  forget(input: Input<'memory.forget'>): Promise<Result<'memory.forget'>>;
}
declare module 'cordis' { interface Context { seraphMemory: SeraphMemory; } }
