import { Context, type Plugin } from 'cordis';
import type { SeraphAudit } from '../../contracts/audit.js';
import type { Input, Result } from '../../contracts/methods.js';
import type { ScopedRequestClient } from '../../contracts/client.js';
import { NativeProxy } from '../proxy.js';

export class SeraphAuditService extends NativeProxy implements SeraphAudit {
  constructor(ctx: Context, client: ScopedRequestClient) { super(ctx, 'seraphAudit', client); }
  append(input: Input<'audit.append'>): Promise<Result<'audit.append'>> { return this.forward('audit.append', input); }
}
/** Trusted composition injects the original invocation-scoped transport. */
export function createPlugin(client: ScopedRequestClient): Plugin.Object<Record<string, never>> {
  return { name: 'seraph.audit.v1', provide: ['seraphAudit'], apply(ctx) { new SeraphAuditService(ctx, client); } };
}
