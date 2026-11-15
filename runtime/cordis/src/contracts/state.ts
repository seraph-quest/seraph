/** Host-reported provider readiness (#1024). Python hosts and activates the
 * state, authority and audit providers; these facades only report. */
export const DOMAIN = 'seraph.state.v1' as const;
export const PROVIDER_DOMAINS = Object.freeze(['seraph.state.v1', 'seraph.authority.v1', 'seraph.audit.v1', 'seraph.tasks.v1',
  'seraph.goals.v1', 'seraph.memory.v1', 'seraph.artifacts.v1'] as const);
export type ProviderDomain = (typeof PROVIDER_DOMAINS)[number];
export type ProviderState = 'active' | 'degraded' | 'failed' | 'quiescing';
export interface ProviderReport { domain: ProviderDomain; state: ProviderState; epoch: number; composition_digest: string; reason_code: string | null; }
export interface SeraphState {
  /** The host's last report for one domain; undefined until the host reports. */
  readiness(domain: ProviderDomain): ProviderReport | undefined;
  reports(): ProviderReport[];
}
declare module 'cordis' { interface Context { seraphState: SeraphState; } }
