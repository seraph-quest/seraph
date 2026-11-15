import { Context, Service, type Plugin } from 'cordis';
import { DOMAIN, type DispatchFacade, type SeraphDispatch } from '../../contracts/dispatch.js';
import type { ScopedRequestClient } from '../../contracts/client.js';

const FACADE: DispatchFacade = Object.freeze({ domain: DOMAIN, timers: false, storage: false, executes: false });

/** Readiness-only facade over the Python-hosted WorkBoard dispatcher lifecycle
 * (#1025): it never forwards a call, starts a timer or executes work. */
export class SeraphDispatchService extends Service implements SeraphDispatch {
  constructor(ctx: Context) { super(ctx, 'seraphDispatch'); }
  describe(): DispatchFacade { return FACADE; }
}
export function createPlugin(_client: ScopedRequestClient): Plugin.Object<Record<string, never>> {
  return { name: 'seraph.dispatch.v1', provide: ['seraphDispatch'], apply(ctx) { new SeraphDispatchService(ctx); } };
}
