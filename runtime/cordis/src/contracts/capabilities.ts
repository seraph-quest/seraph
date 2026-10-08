import type { Input, Result } from './methods.js';

export const DOMAIN = 'seraph.capabilities.v1' as const;
export interface SeraphCapabilities {
  list(input: Input<'capabilities.list'>): Promise<Result<'capabilities.list'>>;
  describe(input: Input<'capabilities.describe'>): Promise<Result<'capabilities.describe'>>;
  invoke(input: Input<'capabilities.invoke'>): Promise<Result<'capabilities.invoke'>>;
}
declare module 'cordis' { interface Context { seraphCapabilities: SeraphCapabilities; } }
