import { Context, type Plugin } from 'cordis';
import type { SeraphGoals } from '../../contracts/goals.js';
import type { Input, Result } from '../../contracts/methods.js';
import type { ScopedRequestClient } from '../../contracts/client.js';
import { NativeProxy } from '../proxy.js';

export class SeraphGoalsService extends NativeProxy implements SeraphGoals {
  constructor(ctx: Context, client: ScopedRequestClient) { super(ctx, 'seraphGoals', client); }
  read(input: Input<'goals.read'>): Promise<Result<'goals.read'>> { return this.forward('goals.read', input); }
}
/** Trusted composition injects the original invocation-scoped transport. */
export function createPlugin(client: ScopedRequestClient): Plugin.Object<Record<string, never>> {
  return { name: 'seraph.goals.v1', provide: ['seraphGoals'], apply(ctx) { new SeraphGoalsService(ctx, client); } };
}
