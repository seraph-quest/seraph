import { Context, Service, type Plugin } from 'cordis';
import { createHash } from 'node:crypto';
import type { SeraphSourceExtraction } from '../../contracts/source_extraction.js';
import { validateInput, validateResult, type Input, type Result } from '../../contracts/methods.js';
import type { ScopedRequestClient } from '../../contracts/client.js';
import { ProtocolError, type Json } from '../../protocol.js';
import { canonicalJson } from '../../contracts/schema.js';
import { normalizeSourceText } from './html.js';
import { FeedRejected, feedText } from './rss-atom.js';

/** Interchangeable format providers, selected only by reviewed profile config. */
export const FORMAT_PROVIDERS = Object.freeze({
  'plain-text': (body: string) => normalizeSourceText(body, false),
  'html-text': (body: string) => normalizeSourceText(body, true),
  'rss-atom': (body: string) => feedText(body),
});
export type FormatProvider = keyof typeof FORMAT_PROVIDERS;
export const SOURCE_MEDIA_TYPES = Object.freeze(['text/plain', 'text/html', 'application/rss+xml', 'application/atom+xml'] as const);
export interface SourceExtractionConfig { formats: Partial<Record<(typeof SOURCE_MEDIA_TYPES)[number], FormatProvider>>; }

export const MAX_NORMALIZED_BYTES = 65_536;
const EVIDENCE_CHUNK_BYTES = 4096;
const blocked = (reason_code: string) => ({ status: 'blocked', reason_code, memory_status: 'no_learning' }) as const;

/** Closed raw-source envelope Python staged after its own HTTPS/SSRF/consent checks. */
function envelope(content: string): { media_type: string; body: string; source_slot: number } {
  const value = JSON.parse(content) as Record<string, unknown>;
  if (!value || typeof value !== 'object' || Array.isArray(value)
      || Object.keys(value).sort().join(',') !== 'body,media_type,schema_version,source_slot'
      || value.schema_version !== 1 || typeof value.body !== 'string' || typeof value.media_type !== 'string'
      || typeof value.source_slot !== 'number' || !Number.isSafeInteger(value.source_slot)) throw new ProtocolError('raw source envelope changed');
  return value as { media_type: string; body: string; source_slot: number };
}

/** Split exact text into <=4 KiB UTF-8 chunks without separators. */
function chunks(text: string): string[] {
  const result: string[] = [];
  let current = '', size = 0;
  for (const char of text) {
    const bytes = Buffer.byteLength(char, 'utf8');
    if (size + bytes > EVIDENCE_CHUNK_BYTES) { result.push(current); current = ''; size = 0; }
    current += char; size += bytes;
  }
  if (current) result.push(current);
  return result;
}

export class SeraphSourceExtractionService extends Service implements SeraphSourceExtraction {
  readonly configDigest: string;
  constructor(ctx: Context, private readonly config: SourceExtractionConfig, private readonly providerDigest: string) {
    super(ctx, 'seraphSourceExtraction');
    this.configDigest = createHash('sha256').update(canonicalJson(config as unknown as Json)).digest('hex');
  }
  async extract(input: Input<'source-extraction.extract'>): Promise<Result<'source-extraction.extract'>> {
    const request = validateInput('source-extraction.extract', input);
    const raw = await this.ctx.seraphArtifacts.read({ artifact_ref: request.artifact_ref, max_bytes: 65_536 });
    if (raw.status !== 'succeeded') return blocked(raw.reason_code);
    let source;
    try { source = envelope(raw.value.content); } catch { return blocked('source_envelope_invalid'); }
    if (source.source_slot !== request.source_slot) return blocked('source_envelope_invalid');
    const name = (this.config.formats as Record<string, FormatProvider | undefined>)[source.media_type];
    // No fallback: an unmapped media type is an explicit operator-visible block.
    if (name === undefined || !Object.hasOwn(FORMAT_PROVIDERS, name)) return blocked('source_format_provider_unavailable');
    let text: string;
    try { text = FORMAT_PROVIDERS[name](source.body); }
    catch (error) { if (error instanceof FeedRejected) return blocked(error.reason); throw error; }
    if (!text) return blocked('source_normalized_empty');
    const lines = text.split('\n');
    if (Buffer.byteLength(text, 'utf8') > MAX_NORMALIZED_BYTES || lines.length > 4096) return blocked('source_output_oversize');
    const span = lines.slice(request.first_line - 1, request.last_line).join('\n');
    const evidence = chunks(span).map((value, index) => ({ source_ref: `source:${request.source_slot}:${index}`, text: value }));
    if (evidence.length > 64) return blocked('source_output_oversize');
    return validateResult('source-extraction.extract', { status: 'succeeded', memory_status: 'no_learning', value: {
      artifact_ref: request.artifact_ref, input_digest: raw.value.digest, provider_digest: this.providerDigest,
      config_digest: this.configDigest, evidence } });
  }
}

/** Trusted composition supplies reviewed config and this plugin's build digest. */
export function createPlugin(_client: ScopedRequestClient, digest = '0'.repeat(64)): Plugin.Object<Record<string, Json>> {
  return { name: 'seraph.source-extraction.v1', provide: ['seraphSourceExtraction'], inject: ['seraphArtifacts'],
    apply(ctx, config) {
      const formats = (config?.formats ?? {}) as SourceExtractionConfig['formats'];
      new SeraphSourceExtractionService(ctx, { formats: { ...formats } }, digest);
    } };
}
