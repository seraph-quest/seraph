import { Context, type Plugin } from 'cordis';
import type { SeraphCapabilities } from '../../contracts/capabilities.js';
import type { Input, Result } from '../../contracts/methods.js';
import type { ScopedRequestClient } from '../../contracts/client.js';
import { NativeProxy } from '../proxy.js';

export class SeraphCapabilitiesService extends NativeProxy implements SeraphCapabilities {
  constructor(ctx: Context, client: ScopedRequestClient) { super(ctx, 'seraphCapabilities', client); }
  list(input: Input<'capabilities.list'>): Promise<Result<'capabilities.list'>> { return this.forward('capabilities.list', input); }
  describe(input: Input<'capabilities.describe'>): Promise<Result<'capabilities.describe'>> { return this.forward('capabilities.describe', input); }
  invoke(input: Input<'capabilities.invoke'>): Promise<Result<'capabilities.invoke'>> { return this.forward('capabilities.invoke', input); }
}
/** Trusted composition injects the original invocation-scoped transport. */
export function createPlugin(client: ScopedRequestClient): Plugin.Object<Record<string, never>> {
  return { name: 'seraph.capabilities.v1', provide: ['seraphCapabilities'], apply(ctx) { new SeraphCapabilitiesService(ctx, client); } };
}
