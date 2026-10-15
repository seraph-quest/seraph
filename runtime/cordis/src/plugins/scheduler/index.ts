import { Context, type Plugin } from 'cordis';
import type { SeraphScheduler } from '../../contracts/scheduler.js';
import type { Input, Result } from '../../contracts/methods.js';
import type { ScopedRequestClient } from '../../contracts/client.js';
import { NativeProxy } from '../proxy.js';

export class SeraphSchedulerService extends NativeProxy implements SeraphScheduler {
  constructor(ctx: Context, client: ScopedRequestClient) { super(ctx, 'seraphScheduler', client); }
  register(input: Input<'scheduler.register'>): Promise<Result<'scheduler.register'>> { return this.forward('scheduler.register', input); }
  disable(input: Input<'scheduler.disable'>): Promise<Result<'scheduler.disable'>> { return this.forward('scheduler.disable', input); }
  dispatchDue(input: Input<'scheduler.dispatchDue'>): Promise<Result<'scheduler.dispatchDue'>> { return this.forward('scheduler.dispatchDue', input); }
}
/** Trusted composition injects the original invocation-scoped transport. */
export function createPlugin(client: ScopedRequestClient): Plugin.Object<Record<string, never>> {
  return { name: 'seraph.scheduler.v1', provide: ['seraphScheduler'], apply(ctx) { new SeraphSchedulerService(ctx, client); } };
}
