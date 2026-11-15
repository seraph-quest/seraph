export const DOMAIN = 'seraph.dispatch.v1' as const;
/** The WorkBoard dispatcher lifecycle facade (#1025). Python owns the mechanism
 * (the dispatcher loop, its live-worker registry and every store); this
 * provider only proves that the reviewed dispatch plugin is composed and
 * running in this boot. It has no methods, timers, queue or storage. */
export interface DispatchFacade {
  readonly domain: typeof DOMAIN;
  readonly timers: false;
  readonly storage: false;
  readonly executes: false;
}
export interface SeraphDispatch { describe(): DispatchFacade; }
declare module 'cordis' { interface Context { seraphDispatch: SeraphDispatch; } }
