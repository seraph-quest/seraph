import type { Input, Result } from './methods.js';

export const DOMAIN = 'seraph.source-extraction.v1' as const;
export interface SeraphSourceExtraction {
  extract(input: Input<'source-extraction.extract'>): Promise<Result<'source-extraction.extract'>>;
}
declare module 'cordis' { interface Context { seraphSourceExtraction: SeraphSourceExtraction; } }
