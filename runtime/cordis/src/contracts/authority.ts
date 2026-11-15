import type { Input, Result } from './methods.js';

export const DOMAIN = 'seraph.authority.v1' as const;
export interface SeraphAuthority {
  resolve(input: Input<'authority.resolve'>): Promise<Result<'authority.resolve'>>;
}
declare module 'cordis' { interface Context { seraphAuthority: SeraphAuthority; } }
