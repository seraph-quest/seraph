import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import type { ChatRouteReadiness } from "./ModelCard";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import {
  isSuccessfulModelFabricOutcome,
  loadRetainedModelFabricSettings,
  normalizeModelFabricRuntime,
  normalizeModelFabricSettings,
  retainModelFabricSettings,
  type ModelFabricRuntimeStatus,
  type ModelFabricSettingsStatus,
} from "../../lib/modelFabric";

/**
 * Data the Settings tabs share (#1280 PR 3). The old first tab
 * (ArtifactStoragePanel) read artifact storage and model fabric once and drew
 * everything; its pieces now live on the Model, Automation and Diagnostics
 * tabs. Each tab mounts alone, so each reads through these hooks, with the
 * same endpoints, fallbacks and save paths the old panel used.
 */

export interface VlmRuntimeStatus {
  mode: string;
  active?: boolean;
  disabled_reason?: string;
  configured: boolean;
  base_url: string;
  backend_url: string;
  chat_api_base: string;
  chat_completion_endpoint: string;
  chat_health_endpoint: string;
  queue_status_endpoint: string;
  health_endpoint: string;
  backend_health_endpoint: string;
  api_key_configured: boolean;
  feeder_window: number;
  live_probe?: {
    checked: boolean;
    reachable: boolean;
    reason?: string;
    health?: VlmProbeEndpoint;
    backend_health?: VlmProbeEndpoint;
    queue_status?: VlmProbeEndpoint;
    chat_proxy?: VlmProbeEndpoint;
  };
}

export interface VlmProbeEndpoint {
  checked: boolean;
  ok: boolean;
  status_code: number | null;
  error: string;
}

export interface CaptureStoreUsage {
  bytes: number | null;
  file_count: number | null;
  max_mb: number | null;
  retention_days: number | null;
  cap_scope?: string;
  enforced_by?: string;
  limits_status?: string;
}

export interface ArtifactStorageSettings {
  inference?: {
    provider: string;
    active_only: boolean;
    api_base: string;
    credential_configured: boolean;
    allowed_upstreams: string[];
    data_collection: string;
    zero_data_retention: boolean;
    fallbacks_allowed: boolean;
    chat_cloud_egress: string;
    chat_cloud_consent: boolean;
    chat_budget_microusd: number | null;
    status: string;
  };
  screen: {
    analysis_enabled: boolean;
    provider: string;
    model: string;
  };
  screenshot_folder?: {
    enabled: boolean;
    provider: string;
    path: string | null;
    path_source: string;
    image_count: number;
    last_image_at: string | null;
    status: string;
    exists: boolean;
    readable: boolean;
    stored_artifacts: string[];
    capture_store?: CaptureStoreUsage;
    analysis?: {
      provider: string;
      model: string;
      base_url_configured: boolean;
      runtime?: VlmRuntimeStatus;
      observation_count: number;
      analysis_status: Record<string, number>;
      analysis_backlog: number;
      analysis_failures: number;
      analysis_blocked?: number;
      stale_count?: number;
      source_missing_count?: number;
      stale_root_count?: number;
      latest_observation_at: string | null;
      latest_analyzed_at: string | null;
      latest_failure: string | null;
      digest_count: number;
      latest_digest_at: string | null;
      folder_image_count?: number;
      ingested_count?: number;
      remaining_to_ingest?: number;
      processed_count?: number;
      remaining_to_analyze?: number;
      folder_remaining_to_analyze?: number;
      persistence?: {
        db_lock_retries: number;
        db_lock_failures: number;
        selection_db_lock_retries: number;
        selection_db_lock_failures: number;
        persistence_db_lock_retries: number;
        persistence_db_lock_failures: number;
      };
    };
    auto_ingest_enabled: boolean;
    auto_ingest_interval_min: number;
    auto_ingest_limit: number;
    scan_endpoint?: string;
    inspection_endpoint: string;
    inspection_visibility: string;
    control_env: Record<string, string>;
  };
  local_runtime?: {
    active?: boolean;
    disabled_reason?: string;
    gateway_configured: boolean;
    llm_base_url_configured: boolean;
    vlm_base_url_configured: boolean;
    vlm_runtime?: VlmRuntimeStatus;
    model: string;
    profiles: Array<{
      id: string;
      runtime_path: string;
      priority: string;
      reasoning: string;
      max_tokens: number;
      timeout_seconds: number;
    }>;
    profile_proof: {
      status: string;
      per_request_reasoning_control: string;
      safe_for_single_backend_profile_routing: boolean;
      receipt_count: number;
      last_receipt_at: string | null;
      last_receipt_sha256: string | null;
      notes: string[];
    };
    proof_command?: string;
  };
  reports: {
    enabled: boolean;
    hour: number;
    analysis_provider: string;
    archive_dir: string;
    archive_dir_source: string;
    exists: boolean;
    writable: boolean;
    creation_error: string | null;
    stored_artifacts: string[];
    receipt_count: number;
    last_receipt_at: string | null;
    control_env: Record<string, string>;
  };
  email: {
    enabled: boolean;
    preview_required: boolean;
    smtp_configured: boolean;
    recipient_configured: boolean;
    allowlist_configured: boolean;
    sender_configured: boolean;
    control_env: Record<string, string>;
  };
}

export interface ScreenAnalysisSettings {
  enabled: boolean;
  provider: string;
  model: string;
  screenshot_folder?: string;
  screenshot_folder_source?: string;
}

export const FALLBACK_SCREEN_ANALYSIS_SETTINGS: ScreenAnalysisSettings = {
  enabled: true,
  provider: "",
  model: "",
};

export const ARTIFACT_METADATA_TIMEOUT_MS = import.meta.env.MODE === "test" ? 100 : 30_000;
export const MODEL_FABRIC_EMPIRICAL_CANARY_CAPABILITIES = new Set([
  "text",
  "vision",
  "streaming",
  "structured_output",
  "tool_use",
  "embedding",
]);

export interface ReportActionResult {
  action?: string;
  status?: string;
  reason?: string | null;
  recipient_hash?: string | null;
  email?: {
    status?: string;
    reason?: string | null;
    recipient_hash?: string | null;
  };
  report?: {
    date?: string;
    analysis_provider?: string;
    artifacts?: Record<string, string>;
  };
  receipt?: {
    receipt_sha256?: string;
    status?: string;
    reason?: string | null;
  };
}

export interface ScreenshotFolderScanResult {
  screenshot_folder?: string;
  scanned?: number;
  ingested?: number;
  skipped_duplicates?: number;
  rejected?: Array<{ image_path?: string; reason?: string }>;
}

export interface ScreenshotFolderPickResult {
  screenshot_folder?: string;
  screenshot_folder_source?: string;
}
export function boolLabel(value: boolean): string {
  return value ? "On" : "Off";
}

export function sourceLabel(value: string): string {
  if (!value) return "not loaded";
  return value === "default" ? "default" : value.replace(/_/g, " ");
}

export function dirStateLabel(exists: boolean, writable: boolean, creationError: string | null): string {
  if (creationError) return `creation failed: ${creationError}`;
  if (!exists) return "missing";
  return writable ? "ready" : "read-only";
}

export function dirStateTone(exists: boolean, writable: boolean, creationError: string | null): "normal" | "good" | "warn" {
  if (creationError || !exists || !writable) return "warn";
  return "good";
}

export function screenshotFolderStateTone(settings: NonNullable<ArtifactStorageSettings["screenshot_folder"]>): "normal" | "good" | "warn" {
  if (!settings.exists || !settings.readable || settings.status === "invalid_root" || settings.status === "read_error") {
    return "warn";
  }
  return settings.image_count > 0 ? "good" : "normal";
}

export function screenshotAnalysisTone(
  analysis: NonNullable<ArtifactStorageSettings["screenshot_folder"]>["analysis"],
): "normal" | "good" | "warn" {
  if (!analysis) return "normal";
  if (analysis.analysis_failures > 0 || (analysis.analysis_blocked ?? 0) > 0) return "warn";
  if (analysis.analysis_backlog > 0) return "normal";
  return analysis.observation_count > 0 ? "good" : "normal";
}

export function localRuntimeProofTone(
  proof: NonNullable<ArtifactStorageSettings["local_runtime"]>["profile_proof"],
): "normal" | "good" | "warn" {
  if (proof.safe_for_single_backend_profile_routing) return "good";
  if (proof.status === "missing") return "normal";
  return "warn";
}

export function isArtifactStorageSettings(value: unknown): value is ArtifactStorageSettings {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Partial<ArtifactStorageSettings>;
  return (
    typeof candidate.screen?.analysis_enabled === "boolean" &&
    typeof candidate.screen?.provider === "string" &&
    typeof candidate.screen?.model === "string" &&
    typeof candidate.reports?.archive_dir === "string" &&
    typeof candidate.reports?.enabled === "boolean" &&
    typeof candidate.email?.enabled === "boolean"
  );
}

export function isScreenAnalysisSettings(value: unknown): value is ScreenAnalysisSettings {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Partial<ScreenAnalysisSettings>;
  return (
    typeof candidate.enabled === "boolean" &&
    typeof candidate.provider === "string" &&
    typeof candidate.model === "string"
  );
}

export function localApiCandidates(): string[] {
  const candidates: string[] = [];
  if (
    typeof window !== "undefined" &&
    (window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1")
  ) {
    candidates.push("");
    candidates.push(`http://${window.location.hostname}:8004`);
  }
  candidates.push(API_URL);
  if (API_URL.includes("://localhost:")) {
    candidates.push(API_URL.replace("://localhost:", "://127.0.0.1:"));
  }
  if (API_URL.includes("://127.0.0.1:")) {
    candidates.push(API_URL.replace("://127.0.0.1:", "://localhost:"));
  }
  return Array.from(new Set(candidates));
}

export async function fetchJsonWithTimeout(path: string, timeoutMs = 3_000, init?: RequestInit): Promise<unknown> {
  let lastError: unknown = null;
  for (const apiUrl of localApiCandidates()) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await apiFetch(`${apiUrl}${path}`, { ...init, signal: controller.signal });
      if (!response.ok) throw new Error(`Request failed: ${response.status}`);
      return await response.json();
    } catch (error) {
      lastError = error;
    } finally {
      window.clearTimeout(timeout);
    }
  }
  throw lastError ?? new Error("Request failed.");
}

export async function mutateJsonWithTimeout(path: string, timeoutMs: number, init: RequestInit): Promise<unknown> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    // Mutations belong to the configured backend. A conflict or uncertain
    // transport result must never retry a different workspace/backend.
    const response = await apiFetch(`${API_URL}${path}`, { ...init, signal: controller.signal });
    if (!response.ok) {
      const payload = await response.json().catch(() => null);
      const detail: unknown = payload?.detail;
      const description = typeof detail === "string" ? detail
        : detail && typeof detail === "object" ? [
          "code" in detail && typeof detail.code === "string" ? detail.code : null,
          "message" in detail && typeof detail.message === "string" ? detail.message : null,
        ].filter(Boolean).join(" · ") : "";
      throw new Error(`Request failed: ${response.status}${description ? " · " + description : ""}`);
    }
    return await response.json();
  } finally {
    window.clearTimeout(timeout);
  }
}

export async function postModelFabricCanary(path: string, body: Record<string, unknown>): Promise<unknown> {
  return mutateJsonWithTimeout(path, 35_000, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function settingsFromScreenAnalysis(screen: ScreenAnalysisSettings): ArtifactStorageSettings {
  return {
    screen: {
      analysis_enabled: screen.enabled,
      provider: screen.provider,
      model: screen.model,
    },
    screenshot_folder: {
      enabled: true,
      provider: "screenshot_folder",
      path: screen.screenshot_folder ?? null,
      path_source: screen.screenshot_folder_source ?? (screen.screenshot_folder ? "screen-analysis-settings" : "not_loaded"),
      image_count: 0,
      last_image_at: null,
      status: "metadata unavailable",
      exists: false,
      readable: false,
      stored_artifacts: ["image"],
      analysis: {
        provider: "metadata unavailable",
        model: "",
        base_url_configured: false,
        observation_count: 0,
        analysis_status: {},
        analysis_backlog: 0,
        analysis_failures: 0,
        latest_observation_at: null,
        latest_analyzed_at: null,
        latest_failure: null,
        digest_count: 0,
        latest_digest_at: null,
        folder_image_count: 0,
        ingested_count: 0,
        remaining_to_ingest: 0,
        processed_count: 0,
        remaining_to_analyze: 0,
        folder_remaining_to_analyze: 0,
        persistence: {
          db_lock_retries: 0,
          db_lock_failures: 0,
          selection_db_lock_retries: 0,
          selection_db_lock_failures: 0,
          persistence_db_lock_retries: 0,
          persistence_db_lock_failures: 0,
        },
      },
      auto_ingest_enabled: true,
      auto_ingest_interval_min: 5,
      auto_ingest_limit: 100,
      scan_endpoint: "/api/observer/screenshot-folder/scan",
      inspection_endpoint: "/api/observer/screen-artifacts",
      inspection_visibility: "localhost_only",
      control_env: {
        path: "SERAPH_SCREENSHOT_FOLDER",
        auto_ingest_enabled: "SCREENSHOT_FOLDER_INGEST_ENABLED",
        auto_ingest_interval: "SCREENSHOT_FOLDER_INGEST_INTERVAL_MIN",
        auto_ingest_limit: "SCREENSHOT_FOLDER_INGEST_LIMIT",
      },
    },
    local_runtime: {
      gateway_configured: false,
      llm_base_url_configured: false,
      vlm_base_url_configured: false,
      model: "",
      profiles: [],
      profile_proof: {
        status: "missing",
        per_request_reasoning_control: "unverified",
        safe_for_single_backend_profile_routing: false,
        receipt_count: 0,
        last_receipt_at: null,
        last_receipt_sha256: null,
        notes: [],
      },
      proof_command: "PYTHONPATH=. uv run python ../scripts/verify_local_gemma_profiles.py",
    },
    reports: {
      enabled: false,
      hour: 21,
      analysis_provider: "metadata unavailable",
      archive_dir: "metadata unavailable",
      archive_dir_source: "metadata unavailable",
      exists: false,
      writable: false,
      creation_error: "Archive metadata unavailable.",
      stored_artifacts: ["report_text", "report_json"],
      receipt_count: 0,
      last_receipt_at: null,
      control_env: {
        archive_dir: "REPORT_ARCHIVE_DIR",
        enabled: "END_OF_DAY_REPORT_ENABLED",
        llm: "END_OF_DAY_REPORT_LLM_ENABLED",
      },
    },
    email: {
      enabled: false,
      preview_required: true,
      smtp_configured: false,
      recipient_configured: false,
      allowlist_configured: false,
      sender_configured: false,
      control_env: {
        enabled: "EMAIL_REPORTS_ENABLED",
        preview_required: "EMAIL_REPORTS_PREVIEW_REQUIRED",
        smtp_host: "SMTP_HOST",
        recipient: "EMAIL_REPORTS_TO",
        allowlist: "EMAIL_REPORTS_TO_ALLOWLIST",
      },
    },
  };
}

export function ArtifactRow({
  label,
  value,
  tone = "normal",
}: {
  label: string;
  value: string;
  tone?: "normal" | "good" | "warn";
}) {
  const toneClass =
    tone === "good" ? "text-green-400" : tone === "warn" ? "text-yellow-400" : "text-retro-text";

  return (
    <div className="grid grid-cols-[92px_minmax(0,1fr)] gap-2 text-[9px]">
      <div className="text-retro-text/30 uppercase tracking-wider">{label}</div>
      <div className={`${toneClass} min-w-0 truncate`} title={value}>
        {value}
      </div>
    </div>
  );
}

export function formatStoreBytes(bytes: number): string {
  const mib = bytes / (1024 * 1024);
  if (bytes === 0) return "0 MiB";
  if (mib < 1) return `${Math.max(1, Math.round(bytes / 1024))} KiB`;
  if (mib < 10) return `${mib.toFixed(1)} MiB`;
  return `${Math.round(mib)} MiB`;
}

export function captureStoreLabel(store: CaptureStoreUsage): string {
  if (store.max_mb === null || store.retention_days === null) {
    const used = store.bytes === null || store.bytes === undefined ? "usage unavailable" : formatStoreBytes(store.bytes);
    return `${used} · limits unavailable (settings file unreadable) · nothing pruned`;
  }
  const kept = `kept ${store.retention_days} days`;
  const cap = store.max_mb > 0 ? `${store.max_mb} MiB cap` : "no size cap";
  if (store.bytes === null || store.bytes === undefined) {
    return `usage unavailable · ${cap} · ${kept}`;
  }
  return store.max_mb > 0
    ? `${formatStoreBytes(store.bytes)} of ${cap} · ${kept}`
    : `${formatStoreBytes(store.bytes)} · ${cap} · ${kept}`;
}

export function captureStoreTone(store: CaptureStoreUsage): "normal" | "good" | "warn" {
  if (store.bytes === null || store.bytes === undefined || store.max_mb === null) return "warn";
  return store.max_mb > 0 && store.bytes > store.max_mb * 1024 * 1024 ? "warn" : "normal";
}

export function screenshotFolderDisplayPath(path: string | null): string {
  return path && path.trim() ? path : "not set";
}

export function vlmReachabilityLabel(runtime?: VlmRuntimeStatus): string {
  if (runtime?.active === false) {
    return "inactive · OpenRouter-only";
  }
  const probe = runtime?.live_probe;
  if (!runtime?.configured) {
    return "not configured";
  }
  if (!probe?.checked) {
    return "not checked";
  }
  const requiredEndpoints = [
    ["health", probe.health],
    ["backend", probe.backend_health],
    ["queue", probe.queue_status],
    ["chat", probe.chat_proxy],
  ] as const;
  if (probe.reachable && requiredEndpoints.every(([, endpoint]) => endpoint?.ok)) {
    return "direct route ok";
  }
  const failing = requiredEndpoints.filter(([, endpoint]) => !endpoint?.ok);
  const reason = failing
    .map(([label, endpoint]) => {
      const probeEndpoint = endpoint as VlmProbeEndpoint | undefined;
      return `${label}:${probeEndpoint?.error || probeEndpoint?.status_code || "missing"}`;
    })
    .join(" ");
  return reason ? `direct route failing · ${reason}` : "direct route failing";
}

export function vlmReachabilityTone(runtime?: VlmRuntimeStatus): "normal" | "good" | "warn" {
  if (runtime?.active === false) {
    return "normal";
  }
  const probe = runtime?.live_probe;
  if (!runtime?.configured || !probe?.checked) {
    return "normal";
  }
  return probe.reachable &&
    probe.health?.ok &&
    probe.backend_health?.ok &&
    probe.queue_status?.ok &&
    probe.chat_proxy?.ok
    ? "good"
    : "warn";
}
export function modelFabricWorkloadLabel(runtime: ModelFabricRuntimeStatus | null, workload: string): string {
  const route = runtime?.runtime_paths[workload]
    ?? runtime?.workloads[workload === "chat_agent" ? "interactive" : workload === "screenshot_image_analysis" ? "vision" : workload];
  if (!route) return "no route receipt";
  const selected = route.selected ? `selected ${route.selected.profile_id}` : "selected none";
  const attempted = route.attempted ? `attempted ${route.attempted.profile_id}:${route.attempted.outcome}` : "attempted none";
  const succeeded = route.succeeded
    ? `actual ${route.succeeded.profile_id}/${route.succeeded.model}`
    : "actual none";
  const fallback = route.fallback_used
    ? `fallback ${route.fallback_reason_code ?? "used"}`
    : route.fallback_used === false
      ? "no fallback"
      : "fallback unknown";
  const degraded = route.degradation_codes.length ? `degraded ${route.degradation_codes.join(",")}` : "";
  const persistence = route.persistence_error_code ? `persistence ${route.persistence_error_code}` : "";
  return `${selected} · ${attempted} · ${succeeded} · ${fallback}${degraded ? ` · ${degraded}` : ""}${persistence ? ` · ${persistence}` : ""}`;
}

export function modelFabricWorkloadTone(runtime: ModelFabricRuntimeStatus | null, workload: string): "normal" | "good" | "warn" {
  const route = runtime?.runtime_paths[workload]
    ?? runtime?.workloads[workload === "chat_agent" ? "interactive" : workload === "screenshot_image_analysis" ? "vision" : workload];
  if (!route) return "normal";
  if (route.last_outcome === null) return "normal";
  return isSuccessfulModelFabricOutcome(route.last_outcome) && route.persistence === "persisted" ? "good" : "warn";
}

export function modelFabricProofLabel(runtime: ModelFabricRuntimeStatus | null): string {
  if (!runtime?.proofs.length) return "no capability proof metadata";
  const counts = runtime.proofs.reduce<Record<string, number>>((summary, proof) => {
    summary[proof.status] = (summary[proof.status] ?? 0) + 1;
    return summary;
  }, {});
  return Object.entries(counts).map(([status, count]) => `${count} ${status}`).join(" · ");
}

/** Chat readiness from /api/runtime/status.effective_runtime (#1280). */
export function chatRouteReadiness(value: unknown): ChatRouteReadiness | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const route = value as Record<string, unknown>;
  const readiness = route.inference_readiness && typeof route.inference_readiness === "object"
    ? route.inference_readiness as Record<string, unknown> : null;
  return {
    inference_ready: route.inference_ready === true,
    reasons: Array.isArray(readiness?.reasons) ? readiness.reasons.filter((item): item is string => typeof item === "string") : [],
  };
}

/** Where the artifact settings on screen came from. "fallback" is a placeholder, never truth. */
export type ArtifactSettingsSource = "fallback" | "screen" | "full";

/** /api/settings/artifact-storage, with the screen-analysis fallback the old first tab used. */
export function useArtifactStorageSettings() {
  const mountedRef = useRef(true);
  const fetchGenerationRef = useRef(0);
  const fallbackRetryRef = useRef(0);
  const [settings, setSettings] = useState<ArtifactStorageSettings | null>(
    () => settingsFromScreenAnalysis(FALLBACK_SCREEN_ANALYSIS_SETTINGS),
  );
  const [source, setSource] = useState<ArtifactSettingsSource>("fallback");
  const [failed, setFailed] = useState(false);
  const [metadataWarning, setMetadataWarning] = useState<string | null>(
    "Settings metadata is loading; folder controls will unlock when real metadata arrives.",
  );
  const [saving, setSaving] = useState(false);

  const fetchSettings = useCallback(async function fetchSettings(isCancelled: () => boolean = () => !mountedRef.current): Promise<void> {
    const generation = fetchGenerationRef.current + 1;
    fetchGenerationRef.current = generation;
    const canPublish = () => !isCancelled() && fetchGenerationRef.current === generation;
    try {
      try {
        const artifactData = await fetchJsonWithTimeout("/api/settings/artifact-storage", ARTIFACT_METADATA_TIMEOUT_MS);
        if (isArtifactStorageSettings(artifactData)) {
          if (canPublish()) {
            fallbackRetryRef.current = 0;
            setSettings(artifactData);
            setSource("full");
            setMetadataWarning(null);
            setFailed(false);
          }
          return;
        } else {
          throw new Error("Artifact storage response is invalid.");
        }
      } catch {}

      const screenData = await fetchJsonWithTimeout("/api/settings/screen-analysis", 20_000);
      let screenSettings: ArtifactStorageSettings;
      if (isArtifactStorageSettings(screenData)) {
        screenSettings = screenData;
      } else if (isScreenAnalysisSettings(screenData)) {
        screenSettings = settingsFromScreenAnalysis(screenData);
      } else {
        throw new Error("Screen analysis settings response is invalid.");
      }
      if (canPublish()) {
        setSettings(screenSettings);
        setSource(isArtifactStorageSettings(screenData) ? "full" : "screen");
        setFailed(false);
        setMetadataWarning("Folder metadata is still loading; analysis controls are live.");
        if (fallbackRetryRef.current < 12) {
          fallbackRetryRef.current += 1;
          window.setTimeout(() => {
            if (mountedRef.current && fetchGenerationRef.current === generation) void fetchSettings(() => !mountedRef.current);
          }, 5_000);
        }
      }
    } catch {
      if (canPublish()) {
        setSettings((current) => current ?? settingsFromScreenAnalysis(FALLBACK_SCREEN_ANALYSIS_SETTINGS));
        setFailed(false);
        setMetadataWarning("Settings metadata is temporarily unavailable; folder controls are disabled until real metadata returns.");
        if (fallbackRetryRef.current < 12) {
          fallbackRetryRef.current += 1;
          window.setTimeout(() => {
            if (mountedRef.current && fetchGenerationRef.current === generation) void fetchSettings(() => !mountedRef.current);
          }, 5_000);
        }
      }
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    mountedRef.current = true;
    void fetchSettings(() => cancelled);
    return () => {
      cancelled = true;
      mountedRef.current = false;
    };
  }, [fetchSettings]);

  const updateScreenAnalysis = async (patch: Record<string, unknown>): Promise<boolean> => {
    if (settings === null || saving) return false;
    setSaving(true);
    try {
      await fetchJsonWithTimeout("/api/settings/screen-analysis", 20_000, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
      if (!mountedRef.current) return true;
      await fetchSettings(() => !mountedRef.current);
      return true;
    } catch {
      if (mountedRef.current) setMetadataWarning("Save failed; screenshot analysis settings were not updated.");
      return false;
    } finally {
      if (mountedRef.current) setSaving(false);
    }
  };

  return { settings, source, failed, metadataWarning, saving, mountedRef, fetchSettings, updateScreenAnalysis };
}

export type ArtifactStorageState = ReturnType<typeof useArtifactStorageSettings>;

/** One artifact-storage reader shared by the panels of one tab (Automation). */
export function ArtifactStorageScope({ children }: { children: (storage: ArtifactStorageState) => ReactNode }) {
  const storage = useArtifactStorageSettings();
  return <>{children(storage)}</>;
}

/** /api/settings/model-fabric plus /api/runtime/status, as the old first tab read them. */
export function useModelFabric() {
  const mountedRef = useRef(true);
  const [modelFabric, setModelFabric] = useState<ModelFabricSettingsStatus | null>(loadRetainedModelFabricSettings);
  const [runtime, setRuntime] = useState<ModelFabricRuntimeStatus | null>(null);
  const [chatRoute, setChatRoute] = useState<ChatRouteReadiness | null>(null);
  const [cordisMetadata, setCordisMetadata] = useState<unknown>(null);
  const [stale, setStale] = useState(() => loadRetainedModelFabricSettings() !== null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async (isCancelled: () => boolean = () => !mountedRef.current): Promise<void> => {
    try {
      const [settingsPayload, runtimePayload] = await Promise.all([
        fetchJsonWithTimeout("/api/settings/model-fabric", 5_000),
        fetchJsonWithTimeout("/api/runtime/status", 5_000).then(value => {
          if (!isCancelled()) setCordisMetadata(value && typeof value === "object" && "cordis_runtime" in value ? value.cordis_runtime : null);
          return value;
        }).catch(reason => { if (!isCancelled()) setCordisMetadata(null); throw reason; }),
      ]);
      const nextSettings = normalizeModelFabricSettings(settingsPayload);
      const runtimeRecord = runtimePayload && typeof runtimePayload === "object" && !Array.isArray(runtimePayload)
        ? runtimePayload as Record<string, unknown>
        : null;
      const nextRuntime = normalizeModelFabricRuntime(runtimeRecord?.model_fabric);
      if (!nextSettings) throw new Error("Model-fabric settings response is invalid.");
      if (isCancelled()) return;
      retainModelFabricSettings(nextSettings);
      setModelFabric(nextSettings);
      setRuntime(nextRuntime);
      setChatRoute(chatRouteReadiness(runtimeRecord?.effective_runtime));
      setStale(false);
      setError(nextRuntime ? null : "Runtime route receipts are unavailable; configuration remains usable.");
    } catch {
      if (isCancelled()) return;
      // The Model card never shows a last-known route as current (#1280).
      setChatRoute(null);
      setStale(true);
      setError("Model-fabric metadata is temporarily unavailable; showing last-known settings.");
    }
  }, []);

  const save = useCallback(async (payload: Record<string, unknown>): Promise<ModelFabricSettingsStatus> => {
    const response = await mutateJsonWithTimeout("/api/settings/model-fabric", 20_000, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const nextSettings = normalizeModelFabricSettings(response);
    if (!nextSettings) throw new Error("Model-fabric settings response is invalid.");
    if (!mountedRef.current) return nextSettings;
    retainModelFabricSettings(nextSettings);
    setModelFabric(nextSettings);
    setStale(false);
    setError(null);
    return nextSettings;
  }, []);

  /** Publish settings another call returned (the catalog check). */
  const publish = useCallback((next: ModelFabricSettingsStatus) => {
    retainModelFabricSettings(next);
    setModelFabric(next);
  }, []);

  useEffect(() => {
    let cancelled = false;
    mountedRef.current = true;
    void refresh(() => cancelled);
    return () => {
      cancelled = true;
      mountedRef.current = false;
    };
  }, [refresh]);

  return { modelFabric, runtime, chatRoute, cordisMetadata, stale, error, mountedRef, refresh, save, publish };
}

export type ModelFabricState = ReturnType<typeof useModelFabric>;
