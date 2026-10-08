import type { Input, Result } from './methods.js';

export const DOMAIN = 'seraph.artifacts.v1' as const;
export interface SeraphArtifacts {
  read(input: Input<'artifacts.read'>): Promise<Result<'artifacts.read'>>;
  stage(input: Input<'artifacts.stage'>): Promise<Result<'artifacts.stage'>>;
  adopt(input: Input<'artifacts.adopt'>): Promise<Result<'artifacts.adopt'>>;
}
declare module 'cordis' { interface Context { seraphArtifacts: SeraphArtifacts; } }
