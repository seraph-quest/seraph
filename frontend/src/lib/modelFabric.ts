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

export interface OpenRouterSetupStatus {
  schema_version: string;
  profile_id: string;
  api_base: string;
  provider_kind: string;
  model_ids: string[];
  capabilities: string[];
  temperature: number;
  max_output_tokens: number;
  timeout_seconds: number;
  allowed_upstreams: string[];
  allow_fallbacks: boolean;
  require_parameters: boolean;
  data_collection: string;
  data_retention_policy: string;
  zero_data_retention: boolean;
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

function normalizeOpenRouterSetup(value: unknown): OpenRouterSetupStatus | null {
  const record = recordOf(value);
  if (!record || typeof record.profile_id !== "string") return null;
  return {
    schema_version: typeof record.schema_version === "string" ? record.schema_version : "unknown",
    profile_id: record.profile_id,
    api_base: typeof record.api_base === "string" ? record.api_base : "",
    provider_kind: typeof record.provider_kind === "string" ? record.provider_kind : "openrouter",
    model_ids: stringArray(record.model_ids),
    capabilities: stringArray(record.capabilities),
    temperature: typeof record.temperature === "number" ? record.temperature : 0.7,
    max_output_tokens: typeof record.max_output_tokens === "number" ? record.max_output_tokens : 4096,
    timeout_seconds: typeof record.timeout_seconds === "number" ? record.timeout_seconds : 120,
    allowed_upstreams: stringArray(record.allowed_upstreams),
    allow_fallbacks: record.allow_fallbacks === true,
    require_parameters: record.require_parameters !== false,
    data_collection: typeof record.data_collection === "string" ? record.data_collection : "unknown",
    data_retention_policy: typeof record.data_retention_policy === "string" ? record.data_retention_policy : "unknown",
    zero_data_retention: record.zero_data_retention === true,
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
    window.localStorage.setItem(MODEL_FABRIC_STORAGE_KEY, JSON.stringify(value));
  } catch {
    // Retention is best effort; the live settings endpoint remains authoritative.
  }
}
