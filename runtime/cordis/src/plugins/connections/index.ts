import { Context, type Plugin } from 'cordis';
import type { SeraphConnections } from '../../contracts/connections.js';
import type { Input, Result } from '../../contracts/methods.js';
import type { ScopedRequestClient } from '../../contracts/client.js';
import { NativeProxy } from '../proxy.js';

export class SeraphConnectionsService extends NativeProxy implements SeraphConnections {
  constructor(ctx: Context, client: ScopedRequestClient) { super(ctx, 'seraphConnections', client); }
  inspect(input: Input<'connections.inspect'>): Promise<Result<'connections.inspect'>> { return this.forward('connections.inspect', input); }
  invokeBoundAdapter(input: Input<'connections.invokeBoundAdapter'>): Promise<Result<'connections.invokeBoundAdapter'>> { return this.forward('connections.invokeBoundAdapter', input); }
}
/** Trusted composition injects the original invocation-scoped transport. */
export function createPlugin(client: ScopedRequestClient): Plugin.Object<Record<string, never>> {
  return { name: 'seraph.connections.v1', provide: ['seraphConnections'], apply(ctx) { new SeraphConnectionsService(ctx, client); } };
}
