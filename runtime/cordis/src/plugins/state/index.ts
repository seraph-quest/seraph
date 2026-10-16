import { Context, Service, type Plugin } from 'cordis';
import type { ProviderDomain, ProviderReport, SeraphState } from '../../contracts/state.js';
import type { ScopedRequestClient } from '../../contracts/client.js';

/** The child's record of what the Python host reported. It owns no store,
 * handle or activation; a ``provider.*`` frame can only update this record. */
export class HostProviderReports {
  private readonly entries = new Map<ProviderDomain, ProviderReport>();
  record(report: ProviderReport): void { this.entries.set(report.domain, Object.freeze({ ...report })); }
  get(domain: ProviderDomain): ProviderReport | undefined { return this.entries.get(domain); }
  list(): ProviderReport[] { return [...this.entries.values()]; }
}
export const hostProviderReports = new HostProviderReports();

export class SeraphStateService extends Service implements SeraphState {
  constructor(ctx: Context, private readonly source: HostProviderReports) { super(ctx, 'seraphState'); }
  readiness(domain: ProviderDomain): ProviderReport | undefined { return this.source.get(domain); }
  reports(): ProviderReport[] { return this.source.list(); }
}
/** Mandatory facade over the Python-hosted state provider; it never forwards a call. */
export function createPlugin(_client: ScopedRequestClient, source: HostProviderReports = hostProviderReports): Plugin.Object<Record<string, never>> {
  return { name: 'seraph.state.v1', provide: ['seraphState'], apply(ctx) { new SeraphStateService(ctx, source); } };
}
