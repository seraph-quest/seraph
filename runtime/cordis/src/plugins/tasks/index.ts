import { Context, type Plugin } from 'cordis';
import type { SeraphTasks } from '../../contracts/tasks.js';
import type { Input, Result } from '../../contracts/methods.js';
import type { ScopedRequestClient } from '../../contracts/client.js';
import { NativeProxy } from '../proxy.js';

export class SeraphTasksService extends NativeProxy implements SeraphTasks {
  constructor(ctx: Context, client: ScopedRequestClient) { super(ctx, 'seraphTasks', client); }
  admit(input: Input<'tasks.admit'>): Promise<Result<'tasks.admit'>> { return this.forward('tasks.admit', input); }
  inspect(input: Input<'tasks.inspect'>): Promise<Result<'tasks.inspect'>> { return this.forward('tasks.inspect', input); }
  cancel(input: Input<'tasks.cancel'>): Promise<Result<'tasks.cancel'>> { return this.forward('tasks.cancel', input); }
  checkpoint(input: Input<'tasks.checkpoint'>): Promise<Result<'tasks.checkpoint'>> { return this.forward('tasks.checkpoint', input); }
  settle(input: Input<'tasks.settle'>): Promise<Result<'tasks.settle'>> { return this.forward('tasks.settle', input); }
}
/** Trusted composition injects the original invocation-scoped transport. */
export function createPlugin(client: ScopedRequestClient): Plugin.Object<Record<string, never>> {
  return { name: 'seraph.tasks.v1', provide: ['seraphTasks'], apply(ctx) { new SeraphTasksService(ctx, client); } };
}
