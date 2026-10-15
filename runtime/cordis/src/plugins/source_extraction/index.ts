import { Context, type Plugin } from 'cordis';
import type { SeraphSourceExtraction } from '../../contracts/source_extraction.js';
import type { Input, Result } from '../../contracts/methods.js';
import type { ScopedRequestClient } from '../../contracts/client.js';
import { NativeProxy } from '../proxy.js';

export class SeraphSourceExtractionService extends NativeProxy implements SeraphSourceExtraction {
  constructor(ctx: Context, client: ScopedRequestClient) { super(ctx, 'seraphSourceExtraction', client); }
  extract(input: Input<'source-extraction.extract'>): Promise<Result<'source-extraction.extract'>> { return this.forward('source-extraction.extract', input); }
}
/** Trusted composition injects the original invocation-scoped transport. */
export function createPlugin(client: ScopedRequestClient): Plugin.Object<Record<string, never>> {
  return { name: 'seraph.source-extraction.v1', provide: ['seraphSourceExtraction'], apply(ctx) { new SeraphSourceExtractionService(ctx, client); } };
}
