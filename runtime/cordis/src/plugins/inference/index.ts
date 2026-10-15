import { Context, type Plugin } from 'cordis';
import type { SeraphInference } from '../../contracts/inference.js';
import type { Input, Result } from '../../contracts/methods.js';
import type { ScopedRequestClient } from '../../contracts/client.js';
import { NativeProxy } from '../proxy.js';

export class SeraphInferenceService extends NativeProxy implements SeraphInference {
  constructor(ctx: Context, client: ScopedRequestClient) { super(ctx, 'seraphInference', client); }
  request(input: Input<'inference.request'>): Promise<Result<'inference.request'>> { return this.forward('inference.request', input); }
}
/** Trusted composition injects the original invocation-scoped transport. */
export function createPlugin(client: ScopedRequestClient): Plugin.Object<Record<string, never>> {
  return { name: 'seraph.inference.v1', provide: ['seraphInference'], apply(ctx) { new SeraphInferenceService(ctx, client); } };
}
