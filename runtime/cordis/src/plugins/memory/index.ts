import { Context, type Plugin } from 'cordis';
import type { SeraphMemory } from '../../contracts/memory.js';
import type { Input, Result } from '../../contracts/methods.js';
import type { ScopedRequestClient } from '../../contracts/client.js';
import { NativeProxy } from '../proxy.js';

export class SeraphMemoryService extends NativeProxy implements SeraphMemory {
  constructor(ctx: Context, client: ScopedRequestClient) { super(ctx, 'seraphMemory', client); }
  retrieve(input: Input<'memory.retrieve'>): Promise<Result<'memory.retrieve'>> { return this.forward('memory.retrieve', input); }
  propose(input: Input<'memory.propose'>): Promise<Result<'memory.propose'>> { return this.forward('memory.propose', input); }
  applyReviewed(input: Input<'memory.applyReviewed'>): Promise<Result<'memory.applyReviewed'>> { return this.forward('memory.applyReviewed', input); }
  forget(input: Input<'memory.forget'>): Promise<Result<'memory.forget'>> { return this.forward('memory.forget', input); }
}
/** Trusted composition injects the original invocation-scoped transport. */
export function createPlugin(client: ScopedRequestClient): Plugin.Object<Record<string, never>> {
  return { name: 'seraph.memory.v1', provide: ['seraphMemory'], apply(ctx) { new SeraphMemoryService(ctx, client); } };
}
