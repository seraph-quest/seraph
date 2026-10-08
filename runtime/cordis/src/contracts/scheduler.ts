import type { Input, Result } from './methods.js';

export const DOMAIN = 'seraph.scheduler.v1' as const;
export interface SeraphScheduler {
  register(input: Input<'scheduler.register'>): Promise<Result<'scheduler.register'>>;
  disable(input: Input<'scheduler.disable'>): Promise<Result<'scheduler.disable'>>;
  dispatchDue(input: Input<'scheduler.dispatchDue'>): Promise<Result<'scheduler.dispatchDue'>>;
}
declare module 'cordis' { interface Context { seraphScheduler: SeraphScheduler; } }
