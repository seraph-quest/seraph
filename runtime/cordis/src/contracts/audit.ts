import type { Input, Result } from './methods.js';

export const DOMAIN = 'seraph.audit.v1' as const;
export interface SeraphAudit {
  append(input: Input<'audit.append'>): Promise<Result<'audit.append'>>;
}
declare module 'cordis' { interface Context { seraphAudit: SeraphAudit; } }
