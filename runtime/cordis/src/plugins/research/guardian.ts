/** Guardian research consumer: one accepted extraction step via existing services.
 *
 * Reads the Python-staged extraction request, extracts each staged public
 * snapshot through whichever source provider the profile composes, stages the
 * normalized output for Python's validation/adoption, reads it back, and
 * reports the canonical job. It holds no authority and makes no network call.
 */
import { Context, Service, type Plugin } from 'cordis';
import { createHash } from 'node:crypto';
import type { SeraphResearchConsumer } from '../../contracts/research.js';
import { validateInput, type Input, type Result } from '../../contracts/methods.js';
import type { ScopedRequestClient } from '../../contracts/client.js';
import { canonicalJson } from '../../contracts/schema.js';
import type { Json } from '../../protocol.js';

const blocked = (reason_code: string) => ({ status: 'blocked', reason_code, memory_status: 'no_learning' }) as const;
// Source-content rejections are recorded per slot; anything else blocks the step.
export const SOURCE_REJECTIONS = Object.freeze(['source_feed_malformed', 'source_feed_dtd_forbidden', 'source_feed_entity_forbidden',
  'source_output_oversize', 'source_normalized_empty', 'source_envelope_invalid']);

interface Source { source_slot: number; artifact_ref: string; acquisition_receipt_ref: string; }
function request(content: string): { job_ref: string; sources: Source[] } | undefined {
  try {
    const value = JSON.parse(content) as Record<string, unknown>;
    // ``pins`` records Python's per-job provider/config digests; the consumer never relies on it.
    if (!value || typeof value !== 'object' || Object.keys(value).sort().join(',') !== 'job_ref,pins,schema_version,sources'
        || value.schema_version !== 1 || typeof value.job_ref !== 'string' || !Array.isArray(value.sources) || value.sources.length > 4) return undefined;
    for (const item of value.sources as Record<string, unknown>[]) {
      if (!item || Object.keys(item).sort().join(',') !== 'acquisition_receipt_ref,artifact_ref,source_slot'
          || typeof item.artifact_ref !== 'string' || typeof item.acquisition_receipt_ref !== 'string'
          || !Number.isSafeInteger(item.source_slot)) return undefined;
    }
    return value as unknown as { job_ref: string; sources: Source[] };
  } catch { return undefined; }
}

export class GuardianResearchConsumer extends Service implements SeraphResearchConsumer {
  constructor(ctx: Context) { super(ctx, 'seraphResearchConsumer'); }
  async execute(input: Input<'research.executeAccepted'>): Promise<Result<'research.executeAccepted'>> {
    const { plan_ref } = validateInput('research.executeAccepted', input);
    const staged = await this.ctx.seraphArtifacts.read({ artifact_ref: plan_ref, max_bytes: 65_536 });
    if (staged.status !== 'succeeded') return blocked(staged.reason_code);
    const accepted = request(staged.value.content);
    if (!accepted) return blocked('research_extraction_request_invalid');
    const job = await this.ctx.seraphTasks.inspect({ job_ref: accepted.job_ref });
    if (job.status !== 'succeeded') return blocked(job.reason_code);
    if (job.value.state !== 'running' || accepted.sources.some(source => !job.value.artifact_refs.includes(source.artifact_ref))) {
      return blocked('research_job_not_executable');
    }
    const extraction = this.ctx.get('seraphSourceExtraction');
    if (!extraction) return blocked('source_extraction_provider_unavailable');
    for (const source of accepted.sources) {
      const extracted = await extraction.extract({ artifact_ref: source.artifact_ref, acquisition_receipt_ref: source.acquisition_receipt_ref,
        source_slot: source.source_slot, first_line: 1, last_line: 4096 });
      if (extracted.status !== 'succeeded') {
        if (!SOURCE_REJECTIONS.includes(extracted.reason_code)) return blocked(extracted.reason_code);
        // Rejected content is recorded per slot and never adopted.
        const recorded = await this.ctx.seraphTasks.checkpoint({ checkpoint_ref: `source-rejected:${source.source_slot}:${extracted.reason_code}` });
        if (recorded.status !== 'succeeded') return blocked(recorded.reason_code);
        continue;
      }
      const { input_digest, provider_digest, config_digest, evidence } = extracted.value;
      const content = canonicalJson({ schema_version: 1, source_slot: source.source_slot, input_digest, provider_digest, config_digest,
        text: evidence.map(item => item.text).join('') } as Json);
      const digest = createHash('sha256').update(content).digest('hex');
      const snapshot = await this.ctx.seraphArtifacts.stage({ request_ref: `snapshot:${source.source_slot}`, content, content_digest: digest });
      if (snapshot.status !== 'succeeded') return blocked(snapshot.reason_code);
      const readback = await this.ctx.seraphArtifacts.read({ artifact_ref: snapshot.value.artifact_ref, max_bytes: 65_536 });
      if (readback.status !== 'succeeded') return blocked(readback.reason_code);
      if (readback.value.digest !== snapshot.value.digest) return blocked('research_snapshot_readback_changed');
    }
    const settled = await this.ctx.seraphTasks.inspect({ job_ref: accepted.job_ref });
    if (settled.status !== 'succeeded') return blocked(settled.reason_code);
    return { status: 'succeeded', memory_status: 'no_learning', value: { job_ref: settled.value.job_ref, revision: settled.value.revision, state: settled.value.state } };
  }
}
export function createPlugin(_client: ScopedRequestClient): Plugin.Object<Record<string, never>> {
  return { name: 'seraph.guardian-research.v1', provide: ['seraphResearchConsumer'], inject: ['seraphTasks', 'seraphArtifacts'],
    apply(ctx) { new GuardianResearchConsumer(ctx); } };
}
