import type { Input, Result } from './methods.js';

export const DOMAIN = 'seraph.connections.v1' as const;
export interface SeraphConnections {
  inspect(input: Input<'connections.inspect'>): Promise<Result<'connections.inspect'>>;
  invokeBoundAdapter(input: Input<'connections.invokeBoundAdapter'>): Promise<Result<'connections.invokeBoundAdapter'>>;
}
declare module 'cordis' { interface Context { seraphConnections: SeraphConnections; } }
