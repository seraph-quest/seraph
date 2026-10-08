import type { Input, Result } from './methods.js';

export const DOMAIN = 'seraph.inference.v1' as const;
export interface SeraphInference {
  request(input: Input<'inference.request'>): Promise<Result<'inference.request'>>;
}
declare module 'cordis' { interface Context { seraphInference: SeraphInference; } }
