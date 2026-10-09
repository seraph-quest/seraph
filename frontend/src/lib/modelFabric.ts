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
}

export interface OpenRouterSlotStatus {
  status: "configuration_required" | "blocked" | "ready";
  error_code: string | null;
  proof_expires_at: string | null;
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
  openrouter_setup?: OpenRouterSetupStatus | null;
  near_text?: NearTextSetupStatus | null;
  /** Local metadata failure marker; unrelated settings remain usable. */
  near_text_metadata_unavailable?: boolean;
  inference_accounting?: InferenceAccountingStatus | null;
  egress_revision?: number;
  egress_revoked?: boolean;
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
      status: rawState?.status === "ready" || rawState?.status === "configuration_required" ? rawState.status : "blocked",
      error_code: typeof rawState?.error_code === "string" ? rawState.error_code : rawState ? null : "slot_metadata_unavailable",
      proof_expires_at: typeof rawState?.proof_expires_at === "string" ? rawState.proof_expires_at : null,
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
    openrouter_setup: normalizeOpenRouterSetup(record.openrouter_setup),
    near_text: nearText,
    near_text_metadata_unavailable: record.near_text_metadata_unavailable === true || (record.near_text != null && !nearText),
    inference_accounting: normalizeInferenceAccounting(record.inference_accounting),
    egress_revision: typeof record.egress_revision === "number" ? record.egress_revision : undefined,
    egress_revoked: record.egress_revoked === true,
  };
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
