import { Context, type Plugin } from 'cordis';
import type { SeraphAuthority } from '../../contracts/authority.js';
import type { Input, Result } from '../../contracts/methods.js';
import type { ScopedRequestClient } from '../../contracts/client.js';
import { NativeProxy } from '../proxy.js';

export class SeraphAuthorityService extends NativeProxy implements SeraphAuthority {
  constructor(ctx: Context, client: ScopedRequestClient) { super(ctx, 'seraphAuthority', client); }
  resolve(input: Input<'authority.resolve'>): Promise<Result<'authority.resolve'>> { return this.forward('authority.resolve', input); }
}
/** Trusted composition injects the original invocation-scoped transport. */
export function createPlugin(client: ScopedRequestClient): Plugin.Object<Record<string, never>> {
  return { name: 'seraph.authority.v1', provide: ['seraphAuthority'], apply(ctx) { new SeraphAuthorityService(ctx, client); } };
}
