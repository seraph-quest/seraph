export interface ModelFabricProfileStatus {
  id: string;
  provider_kind: string;
  model: string;
  api_base: string;
  enabled: boolean;
  secret_configured: boolean;
  missing_secret: boolean;
  capabilities: string[];
  transport_adapter: string;
  cost_source: string | null;
  cost_source_updated_at: number | null;
  model_fabric_eligible: boolean;
  model_fabric_exclusion_reason: string | null;
  routable: boolean;
  non_routable_reasons: string[];
  canary_timeout_seconds: number;
}

export interface ModelFabricAttempt {
  profile_id: string;
  model: string;
  adapter: string;
  destination_class: string;
  outcome: string;
  latency_ms: number;
}

export interface ModelFabricSuccess {
  profile_id: string;
  model: string;
  adapter: string;
  receipt_id: string;
  finished_at: string;
}

export interface ModelFabricWorkloadStatus {
  selected: ModelFabricAttempt | null;
  attempted: ModelFabricAttempt | null;
  attempt_count: number;
  last_outcome: string | null;
  fallback_used: boolean | null;
  fallback_reason_code: string | null;
  degradation_codes: string[];
  succeeded: ModelFabricSuccess | null;
  persistence: string;
  persistence_error_code: string | null;
  receipt_persistence_degraded: boolean | null;
}

export interface ModelFabricProofStatus {
  profile_id: string;
  capability: string;
  status: string;
  outcome: string | null;
  checked_at: string | null;
  expires_at: string | null;
}

export interface ModelFabricRuntimeStatus {
  status: string;
  configuration_status: string;
  configuration_error: string | null;
  configured_chat_profile: string;
  profiles: ModelFabricProfileStatus[];
  excluded_profiles: ModelFabricProfileStatus[];
  proofs: ModelFabricProofStatus[];
  topology: { text: string[]; vlm: string[] };
  runtime_paths: Record<string, ModelFabricWorkloadStatus>;
  workloads: Record<string, ModelFabricWorkloadStatus>;
  openrouter_setup?: OpenRouterSetupStatus | null;
}

export type OpenRouterPurpose = "text" | "vision" | "embedding";
export type OpenRouterOptionalPurpose = OpenRouterPurpose | "audio";

// Values accepted by PUT exclude the response's readiness and proof metadata.
export interface OpenRouterRouteValue {
  model_id: string;
  enabled: boolean;
  capabilities: string[];
  allowed_upstreams: string[];
  temperature: number;
  max_output_tokens: number;
  timeout_seconds: number;
  zero_data_retention: boolean;
  request_cost_bound_microusd: number;
  /** Prompt window, independent of max_output_tokens; null uses the backend default. */
  context_window_tokens?: number | null;
}

export interface OpenRouterSlotStatus {
  status: "configuration_required" | "blocked" | "ready" | "retired";
  error_code: string | null;
  proof_expires_at: string | null;
  /** First capability to (re-)prove, e.g. proof_expired:health. */
  proof_reason?: string | null;
}

export interface OpenRouterRouteStatus extends OpenRouterRouteValue, OpenRouterSlotStatus {}

export interface OpenRouterSetupValue {
  schema_version: "seraph.openrouter.setup.v2" | "seraph.openrouter.setup.v3";
  routes: Record<OpenRouterPurpose, OpenRouterRouteValue | null> & { audio?: OpenRouterRouteValue | null };
  api_key?: string;
  egress_class: string;
  cloud_egress_acknowledged: true;
  vision_egress_acknowledged?: true;
  embedding_egress_acknowledged?: true;
  audio_egress_acknowledged?: true;
  spend_ceiling_microusd: number;
  max_queued: number;
  max_inflight: 1;
  max_outstanding_per_owner: number;
  max_retries: number;
  data_collection: "deny";
  data_retention_policy: "deny";
  allow_fallbacks: false;
  require_parameters: true;
}

export interface OpenRouterSetupStatus {
  schema_version: "seraph.openrouter.setup.v2" | "seraph.openrouter.setup.v3";
  profile_id: string;
  api_base: string;
  provider_kind: string;
  routes: Record<OpenRouterPurpose, OpenRouterRouteStatus | null> & { audio?: OpenRouterRouteStatus | null };
  slot_statuses: Record<OpenRouterPurpose, OpenRouterSlotStatus> & { audio?: OpenRouterSlotStatus };
  allow_fallbacks: boolean;
  require_parameters: boolean;
  data_collection: string;
  data_retention_policy: string;
  egress_class: string;
  cloud_egress_acknowledged: boolean;
  spend_ceiling_microusd: number | null;
  max_queued: number;
  max_inflight: number;
  max_outstanding_per_owner: number;
  max_retries: number;
  credential_ref: string;
  credential_fingerprint: string | null;
  credential_configured: boolean;
  status: string;
  error_code: string | null;
  provider_calls: string;
  /** ADR-035: retired by policy; retained state is readable, never activatable. */
  retired: boolean;
  retirement_label: string | null;
  settlement_available: boolean;
}

export const OPENROUTER_RETIRED_LABEL = "Retired (ADR-035) — no new requests; existing costs can still be settled";

/** Provider-neutral shared-lane limits and deployment ceiling (ADR-035). */
export interface InferenceLaneValue {
  spend_ceiling_microusd: number;
  max_queued: number;
  max_inflight: 1;
  max_outstanding_per_owner: number;
  max_retries: number;
  /** Optional explicit reserve review that reviews an unreviewed overrun up to this bound. */
  request_reserve_review_microusd?: number | null;
}

export interface InferenceLaneStatus extends InferenceLaneValue {
  schema_version: "seraph.inference.lane.v1";
  /** False until the migrated lane has been published by an explicit save. */
  published: boolean;
  status: string;
}

export function normalizeInferenceLane(value: unknown): InferenceLaneStatus | null {
  const record = recordOf(value);
  if (!record || record.schema_version !== "seraph.inference.lane.v1") return null;
  const integer = (field: string, min: number, max: number) => {
    const item = record[field];
    return typeof item === "number" && Number.isSafeInteger(item) && item >= min && item <= max ? item : null;
  };
  const ceiling = integer("spend_ceiling_microusd", 1, 1_000_000_000);
  const queued = integer("max_queued", 1, 64);
  const owner = integer("max_outstanding_per_owner", 1, 16);
  const retries = integer("max_retries", 0, 2);
  if (ceiling === null || queued === null || owner === null || retries === null || record.max_inflight !== 1) return null;
  const review = record.request_reserve_review_microusd;
  if (review != null && !(typeof review === "number" && Number.isSafeInteger(review) && review >= 1 && review <= ceiling)) return null;
  return {
    schema_version: "seraph.inference.lane.v1",
    spend_ceiling_microusd: ceiling,
    max_queued: queued,
    max_inflight: 1,
    max_outstanding_per_owner: owner,
    max_retries: retries,
    request_reserve_review_microusd: typeof review === "number" ? review : null,
    published: record.published === true,
    status: typeof record.status === "string" ? record.status : "unknown",
  };
}

export const NEAR_TEXT_PROFILE = "near.text";
export const NEAR_TEXT_MODEL = "z-ai/glm-5.3-flash";
export const NEAR_TEXT_API_BASE = "https://cloud-api.near.ai/v1";
export const NEAR_TEXT_DISCLOSURE = "NEAR receives the question in plaintext over HTTPS.";

export interface NearTextSetupInput {
  schema_version: "seraph.near.text.v1";
  enabled: boolean;
  profile_id: typeof NEAR_TEXT_PROFILE;
  model_id: typeof NEAR_TEXT_MODEL;
  api_base: typeof NEAR_TEXT_API_BASE;
  max_output_tokens: number;
  timeout_seconds: number;
  request_cost_bound_microusd: number;
  spend_ceiling_microusd: number;
  plaintext_provider_egress_acknowledged: boolean;
  api_key?: string;
}

export interface NearTextSetupStatus extends Omit<NearTextSetupInput, "api_key" | "plaintext_provider_egress_acknowledged"> {
  credential_ref: "vault:near_text_api_key";
  credential_fingerprint: string | null;
  plaintext_egress_consent_revision: number | null;
  key_present: boolean;
  consent_current: boolean;
  status: "disabled" | "configuration_required" | "blocked" | "configured";
  reason_code: string | null;
  tls_transport: true;
  tee_verified: false;
  e2ee: false;
  provider_plaintext_disclosure: typeof NEAR_TEXT_DISCLOSURE;
}

export const NEAR_TEE_API_BASE = "https://cloud-api.near.ai/v1";
/** ADR-035 closed text-slot allowlist (catalog read 2026-11-13). */
export const NEAR_TEE_TEXT_MODELS = [
  "z-ai/glm-5.3-flash",
  "Qwen/Qwen3.8-27B",
  "deepseek-ai/DeepSeek-V4.1-Flash-1017",
  "Qwen/Qwen3.6-35B-A3B-FP8",
] as const;
/** ADR-035 closed vision-slot allowlist (text+image input, catalog read 2026-11-13). */
export const NEAR_TEE_VISION_MODELS = [
  "z-ai/glm-5.3-flash",
  "Qwen/Qwen3.8-27B",
  "Qwen/Qwen3-VL-30B-A3B-Instruct",
  "deepseek-ai/DeepSeek-V4.1-Flash-1017",
] as const;
export const NEAR_TEE_SCREENSHOT_DISCLOSURE = "Screenshots are sent to the NEAR gateway in plaintext for analysis. TEE-hosted per NEAR catalog, not verified by Seraph; no end-to-end encryption.";
export const NEAR_TEE_DISCLOSURE = "TEE-hosted per NEAR catalog, not verified by Seraph; NEAR receives requests in plaintext; no end-to-end encryption.";

export interface NearTeeSlotInput {
  model_id: string;
  enabled: boolean;
  max_input_tokens: number;
  max_output_tokens: number;
  timeout_seconds: number;
  temperature?: number | null;
  request_cost_bound_microusd: number;
  catalog_ttl_hours: number;
  plaintext_provider_egress_acknowledged: boolean;
  /** Vision slot only: the separate screenshot purpose consent. */
  screenshot_plaintext_egress_acknowledged?: boolean;
}

export type NearTeeSlotName = "text" | "vision";

export interface NearTeeSetupInput {
  schema_version: "seraph.near.tee.setup.v1";
  api_base: typeof NEAR_TEE_API_BASE;
  /** An omitted slot keeps its stored value. */
  slots: { text?: NearTeeSlotInput | null; vision?: NearTeeSlotInput | null };
  api_key?: string;
}

export interface NearTeeCatalogReceipt {
  model_id: string | null;
  checked_at_iso: string | null;
  expires_at: string | null;
  outcome: "passed" | "failed" | string;
  reason_code: string | null;
  required_cost_microusd: number | null;
  structured_outputs?: boolean;
}

export interface NearTeeSlotStatus extends Omit<NearTeeSlotInput, "plaintext_provider_egress_acknowledged" | "screenshot_plaintext_egress_acknowledged"> {
  profile_id: string;
  consent_current: boolean;
  status: "disabled" | "configuration_required" | "blocked" | "ready";
  reason_code: string | null;
  catalog_receipt: NearTeeCatalogReceipt | null;
  catalog_receipt_current: boolean;
  tee_hosted_per_catalog: boolean;
  tee_hosting_label: string | null;
  capability_check_timeout_seconds: number;
}

export interface NearTeeSetupStatus {
  configured: boolean;
  status: string;
  reason_code: string | null;
  key_present: boolean;
  credential_fingerprint: string | null;
  allowlist: Record<string, string[]>;
  disclosure: string;
  tee_verified: false;
  e2ee: false;
  /** Largest vision timeout the background analysis job can carry (job budget minus billing window and margin). */
  vision_max_timeout_seconds?: number;
  slots: { text: NearTeeSlotStatus | null; vision?: NearTeeSlotStatus | null };
}

export interface ModelFabricSettingsStatus {
  schema_version: string;
  status: string;
  error_code: string | null;
  updated_at: string | null;
  profiles: ModelFabricProfileStatus[];
  excluded_profiles: ModelFabricProfileStatus[];
  persisted_profile_ids: string[];
  workload_policies: Array<{
    runtime_path: string;
    egress_class: string;
    cloud_egress_acknowledged: boolean;
    allowed_profile_ids: string[];
    allowed_provider_kinds: string[];
    fallback_allowed: boolean;
    max_cost_microusd: number | null;
  }>;
  defaults: { egress_class: string; fallback_allowed: boolean };
  canary_endpoint: string;
  /** Selectable canary proof lifetime; the backend rejects values above max_seconds. */
  canary_proof_ttl: { default_seconds: number; max_seconds: number };
  openrouter_setup?: OpenRouterSetupStatus | null;
  near_text?: NearTextSetupStatus | null;
  /** Local metadata failure marker; unrelated settings remain usable. */
  near_text_metadata_unavailable?: boolean;
  near_tee?: NearTeeSetupStatus | null;
  near_tee_metadata_unavailable?: boolean;
  active_inference_provider?: string | null;
  inference_accounting?: InferenceAccountingStatus | null;
  egress_revision?: number;
  egress_revoked?: boolean;
  /** Provider-neutral lane; null when no governed provider was ever configured. */
  inference_lane?: InferenceLaneStatus | null;
  /** Reason the backend could not project a lane (e.g. inference_lane_unreadable). */
  inference_lane_reason?: string | null;
  /** NEAR text readiness, independent of the blocked canonical routes. */
  near_text_ready?: boolean;
  /** ADR-035: true unless the backend reports OpenRouter as active. */
  openrouter_retired?: boolean;
  /** Why inference routes are blocked, e.g. openrouter_retired / no_active_provider. */
  inference_block_reason?: string | null;
}

export interface InferenceAccountingStatus {
  status: string;
  reason_code?: string;
  accounting_continuity_verified?: boolean;
  authorized_period?: string;
  period_high_water?: string;
  revision?: number;
  period_review?: { endpoint: string; method: string; period_id: string; expected_revision: number } | null;
  period_id?: string;
  settings_revision?: number;
  ceiling_microusd?: number;
  committed_microusd?: number;
  reserved_microusd?: number;
  unknown_microusd?: number;
  remaining_microusd?: number | null;
  operations_truncated?: boolean;
  operation_count?: number;
  /** Largest unreviewed settled charge; a saved request bound must cover it. */
  overrun_max_cost_microusd?: number;
  unreviewed_overruns?: Array<{
    operation_id: string; job_id: string; runtime_path: string; period_id: string;
    bound_microusd: number; actual_cost_microusd: number;
  }>;
  operations: Array<{
    operation_id: string; job_id: string; owner_id: string; runtime_path: string;
    state: string; revision: number; bound_microusd: number; period_id: string;
    recovery_reason?: string | null;
    controls?: Array<{ action: string; endpoint: string; method: string; expected_revision: number; job_id: string; operation_id: string }>;
  }>;
}

export function normalizeInferenceAccounting(value: unknown): InferenceAccountingStatus | null {
  const record = recordOf(value);
  if (!record || typeof record.status !== "string") return null;
  const operations = Array.isArray(record.operations) ? record.operations : [];
  const periodReview = recordOf(record.period_review);
  return {
    status: record.status,
    reason_code: typeof record.reason_code === "string" ? record.reason_code : undefined,
    accounting_continuity_verified: record.accounting_continuity_verified === true,
    authorized_period: typeof record.authorized_period === "string" ? record.authorized_period : undefined,
    period_high_water: typeof record.period_high_water === "string" ? record.period_high_water : undefined,
    revision: typeof record.revision === "number" ? record.revision : undefined,
    period_review: periodReview && periodReview.endpoint === "/api/settings/model-fabric/accounting/period"
      && periodReview.method === "POST" && periodReview.period_id === record.period_id
      && periodReview.expected_revision === record.revision && typeof periodReview.period_id === "string"
      && /^[0-9]{4}-(0[1-9]|1[0-2])$/.test(periodReview.period_id) && typeof periodReview.expected_revision === "number"
      && Number.isSafeInteger(periodReview.expected_revision) && periodReview.expected_revision >= 1
      ? { endpoint: periodReview.endpoint, method: "POST", period_id: periodReview.period_id, expected_revision: periodReview.expected_revision } : null,
    period_id: typeof record.period_id === "string" ? record.period_id : undefined,
    settings_revision: typeof record.settings_revision === "number" ? record.settings_revision : undefined,
    ceiling_microusd: typeof record.ceiling_microusd === "number" ? record.ceiling_microusd : undefined,
    committed_microusd: typeof record.committed_microusd === "number" ? record.committed_microusd : undefined,
    reserved_microusd: typeof record.reserved_microusd === "number" ? record.reserved_microusd : undefined,
    unknown_microusd: typeof record.unknown_microusd === "number" ? record.unknown_microusd : undefined,
    remaining_microusd: typeof record.remaining_microusd === "number" ? record.remaining_microusd : null,
    operations_truncated: record.operations_truncated === true,
    operation_count: typeof record.operation_count === "number" ? record.operation_count : undefined,
    overrun_max_cost_microusd: typeof record.overrun_max_cost_microusd === "number" && Number.isSafeInteger(record.overrun_max_cost_microusd)
      && record.overrun_max_cost_microusd >= 0 ? record.overrun_max_cost_microusd : undefined,
    unreviewed_overruns: Array.isArray(record.unreviewed_overruns) ? record.unreviewed_overruns.slice(0, 16).flatMap((item) => {
      const row = recordOf(item);
      if (!row || typeof row.operation_id !== "string" || typeof row.job_id !== "string"
        || !Number.isSafeInteger(row.bound_microusd) || !Number.isSafeInteger(row.actual_cost_microusd)) return [];
      return [{ operation_id: row.operation_id, job_id: row.job_id, runtime_path: String(row.runtime_path ?? ""),
        period_id: String(row.period_id ?? ""), bound_microusd: row.bound_microusd as number,
        actual_cost_microusd: row.actual_cost_microusd as number }];
    }) : [],
    operations: operations.flatMap((item) => {
      const row = recordOf(item);
      if (!row || typeof row.operation_id !== "string" || typeof row.job_id !== "string" || typeof row.revision !== "number" || typeof row.bound_microusd !== "number") return [];
      const operationId = row.operation_id;
      const jobId = row.job_id;
      const revision = row.revision;
      return [{ operation_id: row.operation_id, job_id: row.job_id, owner_id: String(row.owner_id ?? ""),
        runtime_path: String(row.runtime_path ?? ""), state: String(row.state ?? "unknown"), revision: row.revision,
        bound_microusd: row.bound_microusd, period_id: String(row.period_id ?? ""),
        recovery_reason: typeof row.recovery_reason === "string" ? row.recovery_reason : null,
        controls: Array.isArray(row.controls) ? row.controls.flatMap((entry) => {
          const control = recordOf(entry);
          if (!control || control.action !== "settle" || control.endpoint !== "/api/settings/model-fabric/accounting/settle" || control.method !== "POST" || control.operation_id !== row.operation_id || control.job_id !== row.job_id || control.expected_revision !== row.revision) return [];
          return [{ action: "settle", endpoint: control.endpoint, method: "POST", expected_revision: revision,
            job_id: jobId, operation_id: operationId }];
        }) : [],
      }];
    }),
  };
}

export interface ModelFabricCanaryResult {
  profile_id: string;
  capability: string;
  outcome: string;
  error_code: string | null;
  proof: {
    proof_hash: string;
    profile_id: string;
    model: string;
    adapter: string;
    capability: string;
    outcome: string;
    checked_at: string;
    expires_at: string;
    proven_value: string | number;
  } | null;
  receipt_persistence: string;
  proof_persistence: string;
}

const MODEL_FABRIC_STORAGE_KEY = "seraph.settings.modelFabric.v1";

export function isSuccessfulModelFabricOutcome(value: string | null | undefined): boolean {
  return value === "succeeded" || value === "passed";
}

export function isGreenModelFabricCanary(value: ModelFabricCanaryResult | null): boolean {
  return Boolean(
    value
    && value.outcome === "passed"
    && value.proof
    && value.receipt_persistence === "persisted"
    && value.proof_persistence === "persisted",
  );
}

function recordOf(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

export function normalizeNearTextSetup(value: unknown): NearTextSetupStatus | null {
  const row = recordOf(value);
  const integer = (field: string, min: number, max: number) => typeof row?.[field] === "number"
    && Number.isSafeInteger(row[field]) && (row[field] as number) >= min && (row[field] as number) <= max;
  if (!row || row.schema_version !== "seraph.near.text.v1" || row.profile_id !== NEAR_TEXT_PROFILE
    || row.model_id !== NEAR_TEXT_MODEL || row.api_base !== NEAR_TEXT_API_BASE
    || typeof row.enabled !== "boolean" || !integer("max_output_tokens", 1, 1024)
    || typeof row.timeout_seconds !== "number" || !Number.isFinite(row.timeout_seconds)
    || row.timeout_seconds < 1 || row.timeout_seconds > 45
    || !integer("request_cost_bound_microusd", 1, 1_000_000_000)
    || !integer("spend_ceiling_microusd", 1, 1_000_000_000)
    || (row.request_cost_bound_microusd as number) > (row.spend_ceiling_microusd as number)
    || row.credential_ref !== "vault:near_text_api_key" || typeof row.key_present !== "boolean"
    || typeof row.consent_current !== "boolean"
    || !["disabled", "configuration_required", "blocked", "configured"].includes(String(row.status))
    || !(row.credential_fingerprint === null || typeof row.credential_fingerprint === "string")
    || !(row.plaintext_egress_consent_revision === null || integer("plaintext_egress_consent_revision", 1, Number.MAX_SAFE_INTEGER))
    || !(row.reason_code === null || typeof row.reason_code === "string")
    || row.tls_transport !== true || row.tee_verified !== false || row.e2ee !== false
    || row.provider_plaintext_disclosure !== NEAR_TEXT_DISCLOSURE) return null;
  // Whitelist read-only metadata so a secret accidentally returned by GET cannot
  // enter the retained settings cache or become a subsequent PUT field.
  return {
    schema_version: "seraph.near.text.v1", enabled: row.enabled,
    profile_id: NEAR_TEXT_PROFILE, model_id: NEAR_TEXT_MODEL, api_base: NEAR_TEXT_API_BASE,
    max_output_tokens: row.max_output_tokens as number, timeout_seconds: row.timeout_seconds,
    request_cost_bound_microusd: row.request_cost_bound_microusd as number,
    spend_ceiling_microusd: row.spend_ceiling_microusd as number,
    credential_ref: "vault:near_text_api_key",
    credential_fingerprint: row.credential_fingerprint as string | null,
    plaintext_egress_consent_revision: row.plaintext_egress_consent_revision as number | null,
    key_present: row.key_present, consent_current: row.consent_current,
    status: row.status as NearTextSetupStatus["status"], reason_code: row.reason_code as string | null,
    tls_transport: true, tee_verified: false, e2ee: false, provider_plaintext_disclosure: NEAR_TEXT_DISCLOSURE,
  };
}

function normalizeNearTeeSlot(value: unknown, slotName: NearTeeSlotName = "text"): NearTeeSlotStatus | null {
  const row = recordOf(value);
  const integer = (field: string, min: number, max: number) => typeof row?.[field] === "number"
    && Number.isSafeInteger(row[field]) && (row[field] as number) >= min && (row[field] as number) <= max;
  if (!row || typeof row.model_id !== "string" || typeof row.enabled !== "boolean"
    || !integer("max_input_tokens", 1, 1_048_576) || !integer("max_output_tokens", 1, 65_536)
    || typeof row.timeout_seconds !== "number" || !integer("request_cost_bound_microusd", 1, 1_000_000_000)
    || !integer("catalog_ttl_hours", 1, 24) || typeof row.consent_current !== "boolean"
    || !["disabled", "configuration_required", "blocked", "ready"].includes(String(row.status))) return null;
  const receipt = recordOf(row.catalog_receipt);
  const text = (field: unknown) => (typeof field === "string" ? field : null);
  return {
    model_id: row.model_id, enabled: row.enabled, max_input_tokens: row.max_input_tokens as number,
    max_output_tokens: row.max_output_tokens as number, timeout_seconds: row.timeout_seconds,
    temperature: typeof row.temperature === "number" ? row.temperature : null,
    request_cost_bound_microusd: row.request_cost_bound_microusd as number,
    catalog_ttl_hours: row.catalog_ttl_hours as number, profile_id: "near_tee." + slotName,
    consent_current: row.consent_current, status: row.status as NearTeeSlotStatus["status"],
    reason_code: text(row.reason_code),
    catalog_receipt: receipt ? { model_id: text(receipt.model_id), checked_at_iso: text(receipt.checked_at_iso),
      expires_at: text(receipt.expires_at), outcome: String(receipt.outcome ?? "unknown"),
      reason_code: text(receipt.reason_code),
      required_cost_microusd: typeof receipt.required_cost_microusd === "number" ? receipt.required_cost_microusd : null,
      structured_outputs: receipt.structured_outputs === true } : null,
    catalog_receipt_current: row.catalog_receipt_current === true,
    tee_hosted_per_catalog: row.tee_hosted_per_catalog === true,
    tee_hosting_label: text(row.tee_hosting_label),
    capability_check_timeout_seconds: integer("capability_check_timeout_seconds", 1, 180)
      ? row.capability_check_timeout_seconds as number : 46,
  };
}

export function normalizeNearTeeSetup(value: unknown): NearTeeSetupStatus | null {
  const row = recordOf(value);
  if (!row || row.tee_verified !== false || row.e2ee !== false || typeof row.key_present !== "boolean"
    || typeof row.status !== "string") return null;
  const slots = recordOf(row.slots);
  const allowlist = recordOf(row.allowlist);
  // Whitelist read-only metadata; credential values never enter the cache.
  return {
    configured: row.configured === true, status: row.status,
    reason_code: typeof row.reason_code === "string" ? row.reason_code : null,
    key_present: row.key_present,
    credential_fingerprint: typeof row.credential_fingerprint === "string" ? row.credential_fingerprint : null,
    allowlist: { text: stringArray(allowlist?.text).filter(model => (NEAR_TEE_TEXT_MODELS as readonly string[]).includes(model)),
      vision: stringArray(allowlist?.vision).filter(model => (NEAR_TEE_VISION_MODELS as readonly string[]).includes(model)) },
    disclosure: NEAR_TEE_DISCLOSURE, tee_verified: false, e2ee: false,
    vision_max_timeout_seconds: typeof row.vision_max_timeout_seconds === "number" && Number.isFinite(row.vision_max_timeout_seconds)
      && row.vision_max_timeout_seconds >= 1 && row.vision_max_timeout_seconds <= 120 ? row.vision_max_timeout_seconds : undefined,
    slots: { text: slots?.text == null ? null : normalizeNearTeeSlot(slots.text, "text"),
      vision: slots?.vision == null ? null : normalizeNearTeeSlot(slots.vision, "vision") },
  };
}

function normalizeOpenRouterSetup(value: unknown): OpenRouterSetupStatus | null {
  const record = recordOf(value);
  // Legacy migration and purpose consent belong to the backend. Never infer a
  // new purpose from cached v1 metadata or a partially returned route.
  const rawRoutes = recordOf(record?.routes);
  if (!record || !["seraph.openrouter.setup.v2", "seraph.openrouter.setup.v3"].includes(String(record.schema_version)) || !rawRoutes) return null;
  const rawStatuses = recordOf(record.slot_statuses);
  const routes = {} as OpenRouterSetupStatus["routes"];
  const slot_statuses = {} as OpenRouterSetupStatus["slot_statuses"];
  const slots: OpenRouterOptionalPurpose[] = record.schema_version === "seraph.openrouter.setup.v3" ? ["text", "vision", "embedding", "audio"] : ["text", "vision", "embedding"];
  for (const slot of slots) {
    const route = recordOf(rawRoutes[slot]);
    if (rawRoutes[slot] != null && (!route || typeof route.model_id !== "string"
      || typeof route.enabled !== "boolean" || !Array.isArray(route.capabilities)
      || !Array.isArray(route.allowed_upstreams)
      || !["temperature", "max_output_tokens", "timeout_seconds", "request_cost_bound_microusd"].every(
        (field) => typeof route[field] === "number" && Number.isFinite(route[field]),
      ) || typeof route.zero_data_retention !== "boolean")) return null;
    const rawState = recordOf(rawStatuses?.[slot]) ?? route;
    const state: OpenRouterSlotStatus = {
      status: rawState?.status === "ready" || rawState?.status === "configuration_required" || rawState?.status === "retired" ? rawState.status : "blocked",
      error_code: typeof rawState?.error_code === "string" ? rawState.error_code : rawState ? null : "slot_metadata_unavailable",
      proof_expires_at: typeof rawState?.proof_expires_at === "string" ? rawState.proof_expires_at : null,
      proof_reason: typeof rawState?.proof_reason === "string" ? rawState.proof_reason : null,
    };
    slot_statuses[slot] = state;
    routes[slot] = route ? {
      model_id: route.model_id as string,
      enabled: route.enabled as boolean,
      capabilities: stringArray(route.capabilities),
      allowed_upstreams: stringArray(route.allowed_upstreams),
      temperature: route.temperature as number,
      max_output_tokens: route.max_output_tokens as number,
      timeout_seconds: route.timeout_seconds as number,
      zero_data_retention: route.zero_data_retention as boolean,
      request_cost_bound_microusd: route.request_cost_bound_microusd as number,
      context_window_tokens: typeof route.context_window_tokens === "number" && Number.isSafeInteger(route.context_window_tokens)
        ? route.context_window_tokens
        : null,
      ...state,
    } : null;
  }
  return {
    schema_version: record.schema_version as OpenRouterSetupStatus["schema_version"],
    profile_id: typeof record.profile_id === "string" ? record.profile_id : "openrouter",
    api_base: typeof record.api_base === "string" ? record.api_base : "",
    provider_kind: typeof record.provider_kind === "string" ? record.provider_kind : "openrouter",
    routes,
    slot_statuses,
    allow_fallbacks: record.allow_fallbacks === true,
    require_parameters: record.require_parameters !== false,
    data_collection: typeof record.data_collection === "string" ? record.data_collection : "unknown",
    data_retention_policy: typeof record.data_retention_policy === "string" ? record.data_retention_policy : "unknown",
    egress_class: typeof record.egress_class === "string" ? record.egress_class : "unknown",
    cloud_egress_acknowledged: record.cloud_egress_acknowledged === true,
    spend_ceiling_microusd: typeof record.spend_ceiling_microusd === "number"
      ? record.spend_ceiling_microusd
      : null,
    max_queued: typeof record.max_queued === "number" ? record.max_queued : 64,
    max_inflight: typeof record.max_inflight === "number" ? record.max_inflight : 1,
    max_outstanding_per_owner: typeof record.max_outstanding_per_owner === "number"
      ? record.max_outstanding_per_owner
      : 16,
    max_retries: typeof record.max_retries === "number" ? record.max_retries : 2,
    credential_ref: typeof record.credential_ref === "string" ? record.credential_ref : "vault:openrouter_api_key",
    credential_fingerprint: typeof record.credential_fingerprint === "string"
      ? record.credential_fingerprint
      : null,
    credential_configured: record.credential_configured === true,
    status: typeof record.status === "string" ? record.status : "configuration_required",
    error_code: typeof record.error_code === "string" ? record.error_code : null,
    provider_calls: typeof record.provider_calls === "string" ? record.provider_calls : "manual_canary_only",
    retired: record.retired === true || record.status === "retired",
    retirement_label: typeof record.retirement_label === "string" ? record.retirement_label : null,
    settlement_available: record.settlement_available !== false,
  };
}

function normalizeProfile(value: unknown): ModelFabricProfileStatus | null {
  const record = recordOf(value);
  if (!record || typeof record.id !== "string" || typeof record.model !== "string") return null;
  return {
    id: record.id,
    provider_kind: typeof record.provider_kind === "string" ? record.provider_kind : "unknown",
    model: record.model,
    api_base: typeof record.api_base === "string" ? record.api_base : "",
    enabled: record.enabled === true,
    secret_configured: record.secret_configured === true,
    missing_secret: record.missing_secret === true,
    capabilities: stringArray(record.capabilities),
    transport_adapter: typeof record.transport_adapter === "string" ? record.transport_adapter : "unknown",
    cost_source: typeof record.cost_source === "string" ? record.cost_source : null,
    cost_source_updated_at: typeof record.cost_source_updated_at === "number" ? record.cost_source_updated_at : null,
    model_fabric_eligible: record.model_fabric_eligible === true,
    model_fabric_exclusion_reason: typeof record.model_fabric_exclusion_reason === "string"
      ? record.model_fabric_exclusion_reason
      : null,
    routable: record.routable === true,
    non_routable_reasons: stringArray(record.non_routable_reasons),
    canary_timeout_seconds: typeof record.canary_timeout_seconds === "number"
      ? record.canary_timeout_seconds
      : 10,
  };
}

function normalizeAttempt(value: unknown): ModelFabricAttempt | null {
  const record = recordOf(value);
  if (!record || typeof record.profile_id !== "string" || typeof record.model !== "string") return null;
  return {
    profile_id: record.profile_id,
    model: record.model,
    adapter: typeof record.adapter === "string" ? record.adapter : "unknown",
    destination_class: typeof record.destination_class === "string" ? record.destination_class : "unknown",
    outcome: typeof record.outcome === "string" ? record.outcome : "unknown",
    latency_ms: typeof record.latency_ms === "number" ? record.latency_ms : 0,
  };
}

function normalizeSuccess(value: unknown): ModelFabricSuccess | null {
  const record = recordOf(value);
  if (!record || typeof record.profile_id !== "string" || typeof record.model !== "string") return null;
  return {
    profile_id: record.profile_id,
    model: record.model,
    adapter: typeof record.adapter === "string" ? record.adapter : "unknown",
    receipt_id: typeof record.receipt_id === "string" ? record.receipt_id : "",
    finished_at: typeof record.finished_at === "string" ? record.finished_at : "",
  };
}

function normalizeWorkload(value: unknown): ModelFabricWorkloadStatus | null {
  const record = recordOf(value);
  if (!record) return null;
  return {
    selected: normalizeAttempt(record.selected),
    attempted: normalizeAttempt(record.attempted),
    attempt_count: typeof record.attempt_count === "number" ? record.attempt_count : 0,
    last_outcome: typeof record.last_outcome === "string" ? record.last_outcome : null,
    fallback_used: typeof record.fallback_used === "boolean" ? record.fallback_used : null,
    fallback_reason_code: typeof record.fallback_reason_code === "string" ? record.fallback_reason_code : null,
    degradation_codes: stringArray(record.degradation_codes),
    succeeded: normalizeSuccess(record.succeeded),
    persistence: typeof record.persistence === "string" ? record.persistence : "unknown",
    persistence_error_code: typeof record.persistence_error_code === "string" ? record.persistence_error_code : null,
    receipt_persistence_degraded: typeof record.receipt_persistence_degraded === "boolean"
      ? record.receipt_persistence_degraded
      : null,
  };
}

export function normalizeModelFabricRuntime(value: unknown): ModelFabricRuntimeStatus | null {
  const record = recordOf(value);
  if (!record || typeof record.status !== "string") return null;
  const topology = recordOf(record.topology);
  const workloadRecord = recordOf(record.workloads) ?? {};
  const runtimePathRecord = recordOf(record.runtime_paths) ?? {};
  const workloads: Record<string, ModelFabricWorkloadStatus> = {};
  const runtime_paths: Record<string, ModelFabricWorkloadStatus> = {};
  Object.entries(workloadRecord).forEach(([key, item]) => {
    const workload = normalizeWorkload(item);
    if (workload) workloads[key] = workload;
  });
  Object.entries(runtimePathRecord).forEach(([key, item]) => {
    const runtimePath = normalizeWorkload(item);
    if (runtimePath) runtime_paths[key] = runtimePath;
  });
  return {
    status: record.status,
    configuration_status: typeof record.configuration_status === "string" ? record.configuration_status : "unknown",
    configuration_error: typeof record.configuration_error === "string" ? record.configuration_error : null,
    configured_chat_profile: typeof record.configured_chat_profile === "string" ? record.configured_chat_profile : "",
    profiles: Array.isArray(record.profiles)
      ? record.profiles.flatMap((item) => normalizeProfile(item) ?? [])
      : [],
    excluded_profiles: Array.isArray(record.excluded_profiles)
      ? record.excluded_profiles.flatMap((item) => normalizeProfile(item) ?? [])
      : [],
    proofs: Array.isArray(record.proofs) ? record.proofs.flatMap((item) => {
      const proof = recordOf(item);
      if (!proof || typeof proof.profile_id !== "string" || typeof proof.capability !== "string") return [];
      return [{
        profile_id: proof.profile_id,
        capability: proof.capability,
        status: typeof proof.status === "string" ? proof.status : "unknown",
        outcome: typeof proof.outcome === "string" ? proof.outcome : null,
        checked_at: typeof proof.checked_at === "string" ? proof.checked_at : null,
        expires_at: typeof proof.expires_at === "string" ? proof.expires_at : null,
      }];
    }) : [],
    topology: {
      text: stringArray(topology?.text),
      vlm: stringArray(topology?.vlm),
    },
    runtime_paths,
    workloads,
    openrouter_setup: normalizeOpenRouterSetup(record.openrouter_setup),
  };
}

export function normalizeModelFabricSettings(value: unknown): ModelFabricSettingsStatus | null {
  const record = recordOf(value);
  if (!record || typeof record.schema_version !== "string" || typeof record.status !== "string") return null;
  const defaults = recordOf(record.defaults);
  const policies = Array.isArray(record.workload_policies) ? record.workload_policies : [];
  const nearText = normalizeNearTextSetup(record.near_text);
  const nearTee = normalizeNearTeeSetup(record.near_tee);
  return {
    schema_version: record.schema_version,
    status: record.status,
    error_code: typeof record.error_code === "string" ? record.error_code : null,
    updated_at: typeof record.updated_at === "string" ? record.updated_at : null,
    profiles: Array.isArray(record.profiles)
      ? record.profiles.flatMap((item) => normalizeProfile(item) ?? [])
      : [],
    excluded_profiles: Array.isArray(record.excluded_profiles)
      ? record.excluded_profiles.flatMap((item) => normalizeProfile(item) ?? [])
      : [],
    persisted_profile_ids: stringArray(record.persisted_profile_ids),
    workload_policies: policies.flatMap((item) => {
      const policy = recordOf(item);
      if (!policy || typeof policy.runtime_path !== "string") return [];
      return [{
        runtime_path: policy.runtime_path,
        egress_class: typeof policy.egress_class === "string" ? policy.egress_class : "unknown",
        cloud_egress_acknowledged: policy.cloud_egress_acknowledged === true,
        allowed_profile_ids: stringArray(policy.allowed_profile_ids),
        allowed_provider_kinds: stringArray(policy.allowed_provider_kinds),
        fallback_allowed: policy.fallback_allowed === true,
        max_cost_microusd: typeof policy.max_cost_microusd === "number" ? policy.max_cost_microusd : null,
      }];
    }),
    defaults: {
      egress_class: typeof defaults?.egress_class === "string" ? defaults.egress_class : "unknown",
      fallback_allowed: defaults?.fallback_allowed === true,
    },
    canary_endpoint: typeof record.canary_endpoint === "string"
      ? record.canary_endpoint
      : "/api/settings/model-fabric/canary",
    canary_proof_ttl: normalizeProofTtl(record.canary_proof_ttl),
    openrouter_setup: normalizeOpenRouterSetup(record.openrouter_setup),
    near_text: nearText,
    near_text_metadata_unavailable: record.near_text_metadata_unavailable === true || (record.near_text != null && !nearText),
    near_tee: nearTee,
    near_tee_metadata_unavailable: record.near_tee != null && !nearTee,
    active_inference_provider: typeof record.active_inference_provider === "string" ? record.active_inference_provider : null,
    inference_accounting: normalizeInferenceAccounting(record.inference_accounting),
    egress_revision: typeof record.egress_revision === "number" ? record.egress_revision : undefined,
    egress_revoked: record.egress_revoked === true,
    inference_lane: normalizeInferenceLane(record.inference_lane),
    inference_lane_reason: (() => {
      const raw = recordOf(record.inference_lane);
      if (!raw) return null;
      if (typeof raw.reason_code === "string") return raw.reason_code;
      return normalizeInferenceLane(raw) ? null : "inference_lane_unreadable";
    })(),
    near_text_ready: record.near_text_ready === true,
    // Fail closed: only an explicit "active" report un-retires the UI.
    openrouter_retired: recordOf(recordOf(record.provider_retirement)?.openrouter)?.state !== "active",
    inference_block_reason: typeof record.inference_block_reason === "string" ? record.inference_block_reason : null,
  };
}

/**
 * Compact chat-route proof posture for the composer meta line, e.g.
 * "PROOFS 5H LEFT", "PROOF EXPIRED: HEALTH" or "PROOF FAILED: TOOL USE". Null when no text-route proof
 * metadata exists (legacy or unconfigured deployments).
 */
export function textRouteProofLabel(runtime: ModelFabricRuntimeStatus | null | undefined, now = Date.now()): string | null {
  const proofs = runtime?.proofs.filter((proof) => proof.profile_id === "openrouter.text") ?? [];
  if (!proofs.length) return null;
  const capability = (value: string) => value.replace(/_/g, " ").toUpperCase();
  // A failed canary needs a different fix than a missing one (check the
  // model, upstream or timeout before proving again), so name it.
  const failed = proofs.find((proof) => proof.status === "failed");
  if (failed) return "PROOF FAILED: " + capability(failed.capability);
  const expired = proofs.find((proof) => proof.status === "stale");
  if (expired) return "PROOF EXPIRED: " + capability(expired.capability);
  const missing = proofs.find((proof) => proof.status !== "fresh");
  if (missing) return "PROOF MISSING: " + capability(missing.capability);
  const expiries = proofs.map((proof) => (proof.expires_at ? Date.parse(proof.expires_at) : Number.NaN)).filter(Number.isFinite);
  if (!expiries.length) return null;
  const remainingMs = Math.min(...expiries) - now;
  if (remainingMs <= 0) return "PROOF EXPIRED";
  const minutes = Math.floor(remainingMs / 60_000);
  return "PROOFS " + (minutes >= 60 ? Math.floor(minutes / 60) + "H" : Math.max(minutes, 1) + "M") + " LEFT";
}

export const DEFAULT_PROOF_TTL_SECONDS = 3600;
export const MAX_PROOF_TTL_SECONDS = 86_400;

function normalizeProofTtl(value: unknown): { default_seconds: number; max_seconds: number } {
  const record = recordOf(value);
  const max = typeof record?.max_seconds === "number" && Number.isSafeInteger(record.max_seconds)
    && record.max_seconds >= 60 && record.max_seconds <= MAX_PROOF_TTL_SECONDS ? record.max_seconds : DEFAULT_PROOF_TTL_SECONDS;
  const fallback = Math.min(DEFAULT_PROOF_TTL_SECONDS, max);
  const preferred = typeof record?.default_seconds === "number" && Number.isSafeInteger(record.default_seconds)
    && record.default_seconds >= 60 && record.default_seconds <= max ? record.default_seconds : fallback;
  return { default_seconds: preferred, max_seconds: max };
}

/** Whole-dollar or cent input to integer micro-USD; null when invalid. */
export function dollarsToMicrousd(value: string): number | null {
  const trimmed = value.trim();
  if (!/^\d+(\.\d{1,6})?$/.test(trimmed)) return null;
  const [whole, fraction = ""] = trimmed.split(".");
  const micro = Number(whole) * 1_000_000 + Number(fraction.padEnd(6, "0"));
  return Number.isSafeInteger(micro) ? micro : null;
}

export function microusdToDollars(value: number): string {
  const whole = Math.trunc(value / 1_000_000);
  const fraction = String(value % 1_000_000).padStart(6, "0").replace(/0+$/, "");
  return fraction ? whole + "." + fraction : String(whole);
}

export function normalizeModelFabricCanary(value: unknown): ModelFabricCanaryResult | null {
  const record = recordOf(value);
  if (!record || typeof record.profile_id !== "string" || typeof record.capability !== "string") return null;
  const proof = recordOf(record.proof);
  return {
    profile_id: record.profile_id,
    capability: record.capability,
    outcome: typeof record.outcome === "string" ? record.outcome : "unknown",
    error_code: typeof record.error_code === "string" ? record.error_code : null,
    proof: proof && typeof proof.proof_hash === "string" ? {
      proof_hash: proof.proof_hash,
      profile_id: typeof proof.profile_id === "string" ? proof.profile_id : record.profile_id,
      model: typeof proof.model === "string" ? proof.model : "",
      adapter: typeof proof.adapter === "string" ? proof.adapter : "unknown",
      capability: typeof proof.capability === "string" ? proof.capability : record.capability,
      outcome: typeof proof.outcome === "string" ? proof.outcome : "unknown",
      checked_at: typeof proof.checked_at === "string" ? proof.checked_at : "",
      expires_at: typeof proof.expires_at === "string" ? proof.expires_at : "",
      proven_value: typeof proof.proven_value === "number" || typeof proof.proven_value === "string"
        ? proof.proven_value
        : "unknown",
    } : null,
    receipt_persistence: typeof record.receipt_persistence === "string" ? record.receipt_persistence : "unknown",
    proof_persistence: typeof record.proof_persistence === "string" ? record.proof_persistence : "unknown",
  };
}

export function loadRetainedModelFabricSettings(): ModelFabricSettingsStatus | null {
  if (typeof window === "undefined") return null;
  try {
    const stored = window.localStorage.getItem(MODEL_FABRIC_STORAGE_KEY);
    return stored ? normalizeModelFabricSettings(JSON.parse(stored)) : null;
  } catch {
    return null;
  }
}

export function retainModelFabricSettings(value: ModelFabricSettingsStatus): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(MODEL_FABRIC_STORAGE_KEY, JSON.stringify(normalizeModelFabricSettings(value)));
  } catch {
    // Retention is best effort; the live settings endpoint remains authoritative.
  }
}
