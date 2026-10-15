import { Context, type Plugin } from 'cordis';
import type { SeraphResearch } from '../../contracts/research.js';
import type { Input, Result } from '../../contracts/methods.js';
import type { ScopedRequestClient } from '../../contracts/client.js';
import { NativeProxy } from '../proxy.js';

export class SeraphResearchService extends NativeProxy implements SeraphResearch {
  constructor(ctx: Context, client: ScopedRequestClient) { super(ctx, 'seraphResearch', client); }
  buildPlan(input: Input<'research.buildPlan'>): Promise<Result<'research.buildPlan'>> { return this.forward('research.buildPlan', input); }
  executeAccepted(input: Input<'research.executeAccepted'>): Promise<Result<'research.executeAccepted'>> { return this.forward('research.executeAccepted', input); }
}
/** Trusted composition injects the original invocation-scoped transport. */
export function createPlugin(client: ScopedRequestClient): Plugin.Object<Record<string, never>> {
  return { name: 'seraph.research.v1', provide: ['seraphResearch'], apply(ctx) { new SeraphResearchService(ctx, client); } };
}
