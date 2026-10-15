import { Context, type Plugin } from 'cordis';
import type { SeraphArtifacts } from '../../contracts/artifacts.js';
import type { Input, Result } from '../../contracts/methods.js';
import type { ScopedRequestClient } from '../../contracts/client.js';
import { NativeProxy } from '../proxy.js';

export class SeraphArtifactsService extends NativeProxy implements SeraphArtifacts {
  constructor(ctx: Context, client: ScopedRequestClient) { super(ctx, 'seraphArtifacts', client); }
  read(input: Input<'artifacts.read'>): Promise<Result<'artifacts.read'>> { return this.forward('artifacts.read', input); }
  stage(input: Input<'artifacts.stage'>): Promise<Result<'artifacts.stage'>> { return this.forward('artifacts.stage', input); }
  adopt(input: Input<'artifacts.adopt'>): Promise<Result<'artifacts.adopt'>> { return this.forward('artifacts.adopt', input); }
}
/** Trusted composition injects the original invocation-scoped transport. */
export function createPlugin(client: ScopedRequestClient): Plugin.Object<Record<string, never>> {
  return { name: 'seraph.artifacts.v1', provide: ['seraphArtifacts'], apply(ctx) { new SeraphArtifactsService(ctx, client); } };
}
