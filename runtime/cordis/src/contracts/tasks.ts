import type { Input, Result } from './methods.js';

export const DOMAIN = 'seraph.tasks.v1' as const;
export interface SeraphTasks {
  admit(input: Input<'tasks.admit'>): Promise<Result<'tasks.admit'>>;
  inspect(input: Input<'tasks.inspect'>): Promise<Result<'tasks.inspect'>>;
  cancel(input: Input<'tasks.cancel'>): Promise<Result<'tasks.cancel'>>;
  checkpoint(input: Input<'tasks.checkpoint'>): Promise<Result<'tasks.checkpoint'>>;
  settle(input: Input<'tasks.settle'>): Promise<Result<'tasks.settle'>>;
}
declare module 'cordis' { interface Context { seraphTasks: SeraphTasks; } }
