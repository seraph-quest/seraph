import { useEffect, useRef, useState } from "react";
import { API_URL } from "../../config/constants";
import {
  loadRetainedModelFabricSettings,
  isGreenModelFabricCanary,
  isSuccessfulModelFabricOutcome,
  normalizeModelFabricCanary,
  normalizeModelFabricRuntime,
  normalizeModelFabricSettings,
  retainModelFabricSettings,
  type ModelFabricCanaryResult,
  type ModelFabricRuntimeStatus,
  type ModelFabricSettingsStatus,
} from "../../lib/modelFabric";
import { OpenRouterSetupPanel } from "./OpenRouterSetupPanel";

interface VlmRuntimeStatus {
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

interface VlmProbeEndpoint {
  checked: boolean;
  ok: boolean;
  status_code: number | null;
  error: string;
}

interface ArtifactStorageSettings {
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

interface ScreenAnalysisSettings {
  enabled: boolean;
  provider: string;
  model: string;
  screenshot_folder?: string;
  screenshot_folder_source?: string;
}

const FALLBACK_SCREEN_ANALYSIS_SETTINGS: ScreenAnalysisSettings = {
  enabled: true,
  provider: "",
  model: "",
};

const ARTIFACT_METADATA_TIMEOUT_MS = import.meta.env.MODE === "test" ? 100 : 30_000;
const MODEL_FABRIC_EMPIRICAL_CANARY_CAPABILITIES = new Set([
  "text",
  "vision",
  "streaming",
  "structured_output",
  "tool_use",
]);

interface ReportActionResult {
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

interface ScreenshotFolderScanResult {
  screenshot_folder?: string;
  scanned?: number;
  ingested?: number;
  skipped_duplicates?: number;
  rejected?: Array<{ image_path?: string; reason?: string }>;
}

interface ScreenshotFolderPickResult {
  screenshot_folder?: string;
  screenshot_folder_source?: string;
}

function boolLabel(value: boolean): string {
  return value ? "On" : "Off";
}

function sourceLabel(value: string): string {
  if (!value) return "not loaded";
  return value === "default" ? "default" : value.replace(/_/g, " ");
}

function dirStateLabel(exists: boolean, writable: boolean, creationError: string | null): string {
  if (creationError) return `creation failed: ${creationError}`;
  if (!exists) return "missing";
  return writable ? "ready" : "read-only";
}

function dirStateTone(exists: boolean, writable: boolean, creationError: string | null): "normal" | "good" | "warn" {
  if (creationError || !exists || !writable) return "warn";
  return "good";
}

function screenshotFolderStateTone(settings: NonNullable<ArtifactStorageSettings["screenshot_folder"]>): "normal" | "good" | "warn" {
  if (!settings.exists || !settings.readable || settings.status === "invalid_root" || settings.status === "read_error") {
    return "warn";
  }
  return settings.image_count > 0 ? "good" : "normal";
}

function screenshotAnalysisTone(
  analysis: NonNullable<ArtifactStorageSettings["screenshot_folder"]>["analysis"],
): "normal" | "good" | "warn" {
  if (!analysis) return "normal";
  if (analysis.analysis_failures > 0 || (analysis.analysis_blocked ?? 0) > 0) return "warn";
  if (analysis.analysis_backlog > 0) return "normal";
  return analysis.observation_count > 0 ? "good" : "normal";
}

function localRuntimeProofTone(
  proof: NonNullable<ArtifactStorageSettings["local_runtime"]>["profile_proof"],
): "normal" | "good" | "warn" {
  if (proof.safe_for_single_backend_profile_routing) return "good";
  if (proof.status === "missing") return "normal";
  return "warn";
}

function isArtifactStorageSettings(value: unknown): value is ArtifactStorageSettings {
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

function isScreenAnalysisSettings(value: unknown): value is ScreenAnalysisSettings {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Partial<ScreenAnalysisSettings>;
  return (
    typeof candidate.enabled === "boolean" &&
    typeof candidate.provider === "string" &&
    typeof candidate.model === "string"
  );
}

function localApiCandidates(): string[] {
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

async function fetchJsonWithTimeout(path: string, timeoutMs = 3_000, init?: RequestInit): Promise<unknown> {
  let lastError: unknown = null;
  for (const apiUrl of localApiCandidates()) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(`${apiUrl}${path}`, { ...init, signal: controller.signal });
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

async function postModelFabricCanary(path: string, body: Record<string, unknown>): Promise<unknown> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 35_000);
  try {
    const response = await fetch(`${API_URL}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(`Canary request failed: ${response.status}`);
    return await response.json();
  } finally {
    window.clearTimeout(timeout);
  }
}

function settingsFromScreenAnalysis(screen: ScreenAnalysisSettings): ArtifactStorageSettings {
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

function ArtifactRow({
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

function screenshotFolderDisplayPath(path: string | null): string {
  return path && path.trim() ? path : "not set";
}

function vlmReachabilityLabel(runtime?: VlmRuntimeStatus): string {
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

function vlmReachabilityTone(runtime?: VlmRuntimeStatus): "normal" | "good" | "warn" {
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

function modelFabricWorkloadLabel(runtime: ModelFabricRuntimeStatus | null, workload: string): string {
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

function modelFabricWorkloadTone(runtime: ModelFabricRuntimeStatus | null, workload: string): "normal" | "good" | "warn" {
  const route = runtime?.runtime_paths[workload]
    ?? runtime?.workloads[workload === "chat_agent" ? "interactive" : workload === "screenshot_image_analysis" ? "vision" : workload];
  if (!route) return "normal";
  if (route.last_outcome === null) return "normal";
  return isSuccessfulModelFabricOutcome(route.last_outcome) && route.persistence === "persisted" ? "good" : "warn";
}

function modelFabricProofLabel(runtime: ModelFabricRuntimeStatus | null): string {
  if (!runtime?.proofs.length) return "no capability proof metadata";
  const counts = runtime.proofs.reduce<Record<string, number>>((summary, proof) => {
    summary[proof.status] = (summary[proof.status] ?? 0) + 1;
    return summary;
  }, {});
  return Object.entries(counts).map(([status, count]) => `${count} ${status}`).join(" · ");
}

export function ArtifactStoragePanel() {
  const mountedRef = useRef(true);
  const fetchGenerationRef = useRef(0);
  const fallbackRetryRef = useRef(0);
  const [settings, setSettings] = useState<ArtifactStorageSettings | null>(
    () => settingsFromScreenAnalysis(FALLBACK_SCREEN_ANALYSIS_SETTINGS),
  );
  const [failed, setFailed] = useState(false);
  const [metadataWarning, setMetadataWarning] = useState<string | null>(
    "Settings metadata is loading; folder controls will unlock when real metadata arrives.",
  );
  const [saving, setSaving] = useState(false);
  const [reportAction, setReportAction] = useState<"idle" | "previewing" | "sending" | "testing">("idle");
  const [reportActionResult, setReportActionResult] = useState<ReportActionResult | null>(null);
  const [reportActionError, setReportActionError] = useState<string | null>(null);
  const [screenshotFolderScanning, setScreenshotFolderScanning] = useState(false);
  const [screenshotFolderScanResult, setScreenshotFolderScanResult] = useState<ScreenshotFolderScanResult | null>(null);
  const [screenshotFolderScanError, setScreenshotFolderScanError] = useState<string | null>(null);
  const [screenshotFolderPicking, setScreenshotFolderPicking] = useState(false);
  const [screenshotFolderClearingStale, setScreenshotFolderClearingStale] = useState(false);
  const [screenshotFolderDraft, setScreenshotFolderDraft] = useState("");
  const [modelFabric, setModelFabric] = useState<ModelFabricSettingsStatus | null>(loadRetainedModelFabricSettings);
  const [modelFabricRuntime, setModelFabricRuntime] = useState<ModelFabricRuntimeStatus | null>(null);
  const [modelFabricStale, setModelFabricStale] = useState(() => loadRetainedModelFabricSettings() !== null);
  const [modelFabricError, setModelFabricError] = useState<string | null>(null);
  const [canaryProfile, setCanaryProfile] = useState("");
  const [canaryCapability, setCanaryCapability] = useState("text");
  const [canaryRunning, setCanaryRunning] = useState(false);
  const [canaryResult, setCanaryResult] = useState<ModelFabricCanaryResult | null>(null);
  const [canaryError, setCanaryError] = useState<string | null>(null);

  async function fetchSettings(isCancelled: () => boolean = () => !mountedRef.current) {
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
  }

  async function fetchModelFabric(isCancelled: () => boolean = () => !mountedRef.current) {
    try {
      const [settingsPayload, runtimePayload] = await Promise.all([
        fetchJsonWithTimeout("/api/settings/model-fabric", 5_000),
        fetchJsonWithTimeout("/api/runtime/status", 5_000),
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
      setModelFabricRuntime(nextRuntime);
      setModelFabricStale(false);
      setModelFabricError(nextRuntime ? null : "Runtime route receipts are unavailable; configuration remains usable.");
      setCanaryProfile((current) => current || nextSettings.profiles.find((profile) => profile.enabled)?.id || "");
    } catch {
      if (isCancelled()) return;
      setModelFabricStale(true);
      setModelFabricError("Model-fabric metadata is temporarily unavailable; showing last-known settings.");
    }
  }

  async function runModelFabricCanary() {
    if (!modelFabric || !canaryProfile || canaryRunning) return;
    setCanaryRunning(true);
    setCanaryResult(null);
    setCanaryError(null);
    try {
      const selectedProfile = modelFabric.profiles.find((profile) => profile.id === canaryProfile);
      const payload = await postModelFabricCanary(modelFabric.canary_endpoint, {
        profile_id: canaryProfile,
        capability: selectedCanaryCapability,
        timeout_seconds: selectedProfile?.canary_timeout_seconds ?? 10,
        proof_ttl_seconds: 3600,
      });
      const result = normalizeModelFabricCanary(payload);
      if (!result) throw new Error("Canary response is invalid.");
      if (!mountedRef.current) return;
      setCanaryResult(result);
      await fetchModelFabric();
    } catch (error) {
      if (mountedRef.current) {
        setCanaryError(error instanceof Error ? error.message : "Model-fabric canary failed.");
      }
    } finally {
      if (mountedRef.current) setCanaryRunning(false);
    }
  }

  async function saveOpenRouterSetup(payload: Record<string, unknown>): Promise<ModelFabricSettingsStatus> {
    const response = await fetchJsonWithTimeout("/api/settings/model-fabric", 20_000, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const nextSettings = normalizeModelFabricSettings(response);
    if (!nextSettings) throw new Error("OpenRouter setup response is invalid.");
    if (!mountedRef.current) return nextSettings;
    retainModelFabricSettings(nextSettings);
    setModelFabric(nextSettings);
    setModelFabricStale(false);
    setModelFabricError(null);
    setCanaryProfile((current) => current || nextSettings.profiles.find((profile) => profile.enabled)?.id || "");
    return nextSettings;
  }
  useEffect(() => {
    let cancelled = false;
    mountedRef.current = true;

    void fetchSettings(() => cancelled);
    void fetchModelFabric(() => cancelled);
    return () => {
      cancelled = true;
      mountedRef.current = false;
    };
  }, []);

  const updateScreenAnalysis = async (patch: Record<string, unknown>) => {
    if (settings === null || saving) return;
    setSaving(true);
    try {
      await fetchJsonWithTimeout("/api/settings/screen-analysis", 20_000, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
      if (!mountedRef.current) return;
      await fetchSettings(() => !mountedRef.current);
    } catch {
      if (mountedRef.current) setMetadataWarning("Save failed; screenshot analysis settings were not updated.");
    } finally {
      if (mountedRef.current) setSaving(false);
    }
  };

  const screenshotFolderSource = settings?.screenshot_folder ?? null;
  const canaryProfileStatus = modelFabric?.profiles.find((profile) => profile.id === canaryProfile);
  const availableCanaryCapabilities = Array.from(new Set([
    ...(canaryProfileStatus?.capabilities.filter((capability) => MODEL_FABRIC_EMPIRICAL_CANARY_CAPABILITIES.has(capability)) ?? []),
    "health",
    "latency_ms",
  ]));
  const selectedCanaryCapability = availableCanaryCapabilities.includes(canaryCapability)
    ? canaryCapability
    : availableCanaryCapabilities[0] ?? "health";
  const screenshotFolderPath = screenshotFolderSource?.path ?? null;
  const screenshotFolderPathSource = screenshotFolderSource?.path_source ?? "";
  const screenshotFolderStaleCount = screenshotFolderSource?.analysis?.stale_count ?? 0;
  const screenshotFolderLockedByEnv = screenshotFolderPathSource === "SERAPH_SCREENSHOT_FOLDER";
  const screenshotFolderMetadataLoaded = Boolean(
    screenshotFolderSource &&
    screenshotFolderPathSource !== "not_loaded" &&
    screenshotFolderSource.status !== "metadata unavailable",
  );
  const previewReadyForSend =
    reportActionResult?.action === "manual-preview" &&
    reportActionResult?.status === "ok" &&
    reportActionResult?.receipt?.status === "succeeded";

  const runReportAction = async (action: "preview" | "send" | "test") => {
    if (reportAction !== "idle") return;
    if (action === "send" && !previewReadyForSend) {
      setReportActionError("Preview the report before sending.");
      return;
    }
    setReportAction(action === "preview" ? "previewing" : action === "send" ? "sending" : "testing");
    setReportActionError(null);
    try {
      const payload = (await fetchJsonWithTimeout(
        action === "test" ? "/api/settings/end-of-day-report/test-email" : "/api/settings/end-of-day-report/manual",
        20_000,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: action === "test"
            ? undefined
            : JSON.stringify({
                send_email: action === "send",
                preview_acknowledged: action === "send" && previewReadyForSend,
              }),
        },
      )) as ReportActionResult;
      if (!mountedRef.current) return;
      setReportActionResult(payload);
      await fetchSettings(() => !mountedRef.current);
    } catch {
      if (mountedRef.current) setReportActionError("Report action failed.");
    } finally {
      if (mountedRef.current) setReportAction("idle");
    }
  };

  const runScreenshotFolderScan = async () => {
    if (screenshotFolderScanning || screenshotFolderSource === null) return;
    setScreenshotFolderScanning(true);
    setScreenshotFolderScanError(null);
    try {
      const scanEndpoint = screenshotFolderSource.scan_endpoint;
      if (!scanEndpoint) throw new Error("Screenshot folder scan endpoint unavailable.");
      if (!screenshotFolderPath) throw new Error("Screenshot folder is not configured.");
      const payload = (await fetchJsonWithTimeout(scanEndpoint, 30_000, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ screenshot_folder: screenshotFolderPath, limit: 100 }),
      })) as ScreenshotFolderScanResult;
      if (!mountedRef.current) return;
      setScreenshotFolderScanResult(payload);
      await fetchSettings(() => !mountedRef.current);
    } catch {
      if (mountedRef.current) setScreenshotFolderScanError("Screenshot folder scan failed.");
    } finally {
      if (mountedRef.current) setScreenshotFolderScanning(false);
    }
  };

  useEffect(() => {
    if (screenshotFolderSource !== null) setScreenshotFolderDraft(screenshotFolderPath ?? "");
  }, [screenshotFolderPath, screenshotFolderSource]);

  const saveScreenshotFolder = async () => {
    if (screenshotFolderSource === null || screenshotFolderLockedByEnv) return;
    setScreenshotFolderScanResult(null);
    setScreenshotFolderScanError(null);
    await updateScreenAnalysis({ screenshot_folder: screenshotFolderDraft.trim() });
  };

  const pickScreenshotFolder = async () => {
    if (screenshotFolderSource === null || screenshotFolderLockedByEnv || screenshotFolderPicking) return;
    setScreenshotFolderPicking(true);
    setScreenshotFolderScanResult(null);
    setScreenshotFolderScanError(null);
    try {
      const payload = (await fetchJsonWithTimeout(
        "/api/settings/screen-analysis/screenshot-folder/pick",
        120_000,
        { method: "POST" },
      )) as ScreenshotFolderPickResult;
      if (!mountedRef.current) return;
      if (payload.screenshot_folder) {
        setScreenshotFolderDraft(payload.screenshot_folder);
      }
      await fetchSettings(() => !mountedRef.current);
    } catch {
      if (mountedRef.current) setScreenshotFolderScanError("Folder picker failed or was cancelled.");
    } finally {
      if (mountedRef.current) setScreenshotFolderPicking(false);
    }
  };

  const clearStaleScreenshotFolderObservations = async () => {
    if (screenshotFolderClearingStale || screenshotFolderStaleCount <= 0) return;
    setScreenshotFolderClearingStale(true);
    setScreenshotFolderScanResult(null);
    setScreenshotFolderScanError(null);
    try {
      await fetchJsonWithTimeout(
        "/api/settings/screen-analysis/screenshot-folder/clear-stale",
        20_000,
        { method: "POST" },
      );
      if (!mountedRef.current) return;
      await fetchSettings(() => !mountedRef.current);
    } catch {
      if (mountedRef.current) setScreenshotFolderScanError("Stale screenshot cleanup failed.");
    } finally {
      if (mountedRef.current) setScreenshotFolderClearingStale(false);
    }
  };

  return (
    <div className="px-1">
      <div className="text-[10px] uppercase tracking-wider text-retro-border font-bold mb-2">
        Screenshot Folder
      </div>
      <div className="border border-retro-text/10 rounded px-2 py-2 flex flex-col gap-2">
        {failed ? (
          <div className="flex flex-col gap-2">
            <div className="text-[9px] text-red-400">Screenshot folder settings unavailable.</div>
            <button
              type="button"
              onClick={() => void fetchSettings()}
              className="w-fit border border-retro-text/20 px-2 py-1 text-[9px] uppercase tracking-wider text-retro-text/60 hover:text-retro-text"
            >
              Retry
            </button>
          </div>
        ) : settings === null ? (
          <div className="text-[9px] text-retro-text/40">Loading screenshot folder settings...</div>
        ) : (
          <>
            {metadataWarning && (
              <div className="border border-yellow-400/40 px-2 py-1 text-[9px] text-yellow-400">
                {metadataWarning}
              </div>
            )}
            <div className="flex items-center justify-between gap-2">
              <div className="min-w-0">
                <div className="text-[10px] text-retro-text">Seraph analysis</div>
                <div className="text-[9px] text-retro-text/40 truncate">
                  scans a local screenshot folder; reports stay in Seraph
                </div>
              </div>
              <button
                type="button"
                disabled={saving}
                onClick={() => void updateScreenAnalysis({ enabled: !settings.screen.analysis_enabled })}
                className={`border px-2 py-1 text-[9px] uppercase tracking-wider ${
                  settings.screen.analysis_enabled
                    ? "border-green-400 text-green-400"
                    : "border-retro-text/20 text-retro-text/40"
                }`}
              >
                {boolLabel(settings.screen.analysis_enabled)}
              </button>
            </div>

            {screenshotFolderSource && (
              <div className="border border-retro-text/10 px-2 py-2 flex flex-col gap-1">
                <div className="flex items-center justify-between gap-2">
                  <div className="text-[10px] text-retro-text">Local screenshot images</div>
                  <div className={`text-[9px] uppercase tracking-wider ${
                    screenshotFolderStateTone(screenshotFolderSource) === "good"
                      ? "text-green-400"
                      : screenshotFolderStateTone(screenshotFolderSource) === "warn"
                        ? "text-yellow-400"
                        : "text-retro-text/50"
                  }`}>
                    {screenshotFolderSource.status.replace(/_/g, " ")}
                  </div>
                </div>
                <div className="grid grid-cols-[92px_minmax(0,1fr)] gap-2 text-[9px] items-center">
                  <div className="text-retro-text/30 uppercase tracking-wider">Folder</div>
                  <div className="flex min-w-0 flex-wrap items-center gap-2">
                    <input
                      aria-label="Screenshot folder"
                      value={screenshotFolderDraft}
                      disabled={saving || screenshotFolderLockedByEnv || !screenshotFolderMetadataLoaded}
                      onChange={(event) => setScreenshotFolderDraft(event.target.value)}
                      placeholder={screenshotFolderMetadataLoaded ? "Choose a local folder" : "folder metadata not loaded"}
                      className="min-w-[180px] flex-1 border border-retro-text/20 bg-retro-bg px-1 py-0.5 text-retro-text disabled:opacity-50"
                    />
                    <button
                      type="button"
                      disabled={saving || screenshotFolderLockedByEnv || screenshotFolderPicking}
                      onClick={() => void pickScreenshotFolder()}
                      className="border border-retro-text/20 px-2 py-1 uppercase tracking-wider text-retro-text/70 hover:text-retro-text disabled:opacity-40"
                    >
                      {screenshotFolderPicking ? "Choosing" : "Choose"}
                    </button>
                    <button
                      type="button"
                      disabled={
                        saving ||
                        screenshotFolderLockedByEnv ||
                        !screenshotFolderMetadataLoaded ||
                        screenshotFolderDraft.trim() === (screenshotFolderPath ?? "")
                      }
                      onClick={() => void saveScreenshotFolder()}
                      className="border border-retro-text/20 px-2 py-1 uppercase tracking-wider text-retro-text/70 hover:text-retro-text disabled:opacity-40"
                    >
                      Save
                    </button>
                  </div>
                </div>
                <div className="text-[9px] text-retro-text/40">
                  {screenshotFolderLockedByEnv
                    ? "locked by SERAPH_SCREENSHOT_FOLDER"
                    : "Choose opens the local folder picker; typing the path is the fallback."}
                </div>
                <ArtifactRow label="Folder" value={screenshotFolderDisplayPath(screenshotFolderPath)} tone={screenshotFolderPath ? "normal" : "warn"} />
                <ArtifactRow label="Source" value={sourceLabel(screenshotFolderPathSource)} />
                <ArtifactRow
                  label="Images"
                  value={`${screenshotFolderSource.image_count} images${screenshotFolderSource.last_image_at ? ` · latest ${screenshotFolderSource.last_image_at}` : ""}`}
                  tone={screenshotFolderStateTone(screenshotFolderSource)}
                />
                <ArtifactRow
                  label="Auto scan"
                  value={
                    screenshotFolderSource.auto_ingest_enabled
                      ? `every ${screenshotFolderSource.auto_ingest_interval_min}m · up to ${screenshotFolderSource.auto_ingest_limit} images`
                      : "off"
                  }
                  tone={screenshotFolderSource.auto_ingest_enabled ? "good" : "normal"}
                />
                <ArtifactRow label="Reads" value="local image files only" tone="good" />
                {screenshotFolderSource.analysis && (
                  <>
                    <ArtifactRow
                      label="Analyzer"
                      value={
                        screenshotFolderSource.analysis.provider === "not_configured"
                          ? "not configured"
                          : `${screenshotFolderSource.analysis.provider}${screenshotFolderSource.analysis.model ? ` · ${screenshotFolderSource.analysis.model}` : ""}`
                      }
                      tone={screenshotFolderSource.analysis.provider === "not_configured" ? "warn" : "good"}
                    />
                    {screenshotFolderSource.analysis.runtime && (
                      <>
                        <ArtifactRow
                          label="Runtime"
                          value={
                            screenshotFolderSource.analysis.runtime.configured
                              ? `${screenshotFolderSource.analysis.runtime.mode} · ${screenshotFolderSource.analysis.runtime.base_url}` +
                                (screenshotFolderSource.analysis.runtime.backend_url ? ` -> ${screenshotFolderSource.analysis.runtime.backend_url}` : "")
                              : "not configured"
                          }
                          tone={screenshotFolderSource.analysis.runtime.configured ? "good" : "warn"}
                        />
                        <ArtifactRow
                          label="Reach"
                          value={vlmReachabilityLabel(screenshotFolderSource.analysis.runtime)}
                          tone={vlmReachabilityTone(screenshotFolderSource.analysis.runtime)}
                        />
                      </>
                    )}
                    <ArtifactRow
                      label="Ingested"
                      value={
                        `${screenshotFolderSource.analysis.ingested_count ?? screenshotFolderSource.analysis.observation_count} / ` +
                        `${screenshotFolderSource.analysis.folder_image_count ?? screenshotFolderSource.image_count}` +
                        ` · remaining ${screenshotFolderSource.analysis.remaining_to_ingest ?? Math.max(screenshotFolderSource.image_count - screenshotFolderSource.analysis.observation_count, 0)}`
                      }
                      tone={
                        (screenshotFolderSource.analysis.remaining_to_ingest ?? 1) === 0 &&
                        (screenshotFolderSource.analysis.ingested_count ?? screenshotFolderSource.analysis.observation_count) > 0
                          ? "good"
                          : "normal"
                      }
                    />
                    <ArtifactRow
                      label="Processed"
                      value={
                        `${screenshotFolderSource.analysis.processed_count ?? screenshotFolderSource.analysis.analysis_status.succeeded ?? 0} analyzed · ` +
                        `${screenshotFolderSource.analysis.remaining_to_analyze ?? screenshotFolderSource.analysis.analysis_backlog} queued`
                      }
                      tone={screenshotAnalysisTone(screenshotFolderSource.analysis)}
                    />
                    <ArtifactRow
                      label="Status"
                      value={
                        `${screenshotFolderSource.analysis.observation_count} observations · ` +
                        `${screenshotFolderSource.analysis.analysis_backlog} backlog · ` +
                        `${screenshotFolderSource.analysis.analysis_failures} failed · ` +
                        `${screenshotFolderSource.analysis.analysis_blocked ?? 0} blocked`
                      }
                      tone={screenshotAnalysisTone(screenshotFolderSource.analysis)}
                    />
                    {screenshotFolderStaleCount > 0 && (
                      <ArtifactRow
                        label="Stale"
                        value={
                          `${screenshotFolderStaleCount} cleanup candidates · ` +
                          `${screenshotFolderSource.analysis.source_missing_count ?? 0} missing · ` +
                          `${screenshotFolderSource.analysis.stale_root_count ?? 0} old root`
                        }
                        tone="warn"
                      />
                    )}
                    {screenshotFolderSource.analysis.persistence && (
                      <ArtifactRow
                        label="DB locks"
                        value={
                          `${screenshotFolderSource.analysis.persistence.db_lock_retries} retries · ` +
                          `${screenshotFolderSource.analysis.persistence.db_lock_failures} failed · ` +
                          `${screenshotFolderSource.analysis.persistence.persistence_db_lock_retries} writes`
                        }
                        tone={screenshotFolderSource.analysis.persistence.db_lock_failures > 0 ? "warn" : "good"}
                      />
                    )}
                    <ArtifactRow
                      label="Latest"
                      value={screenshotFolderSource.analysis.latest_analyzed_at ?? screenshotFolderSource.analysis.latest_observation_at ?? "none"}
                      tone={screenshotFolderSource.analysis.latest_analyzed_at ? "good" : "normal"}
                    />
                    <ArtifactRow
                      label="Digest"
                      value={
                        `${screenshotFolderSource.analysis.digest_count} windows` +
                        (screenshotFolderSource.analysis.latest_digest_at ? ` · latest ${screenshotFolderSource.analysis.latest_digest_at}` : "")
                      }
                      tone={screenshotFolderSource.analysis.digest_count > 0 ? "good" : "normal"}
                    />
                    {screenshotFolderSource.analysis.latest_failure && (
                      <ArtifactRow label="Failure" value={screenshotFolderSource.analysis.latest_failure} tone="warn" />
                    )}
                  </>
                )}
                <ArtifactRow label="Inspect" value={`${screenshotFolderSource.inspection_endpoint} (${screenshotFolderSource.inspection_visibility.replace(/_/g, " ")})`} />
                <div className="flex flex-wrap items-center gap-2 pt-1">
                  <button
                    type="button"
                    disabled={screenshotFolderScanning || !screenshotFolderPath || !screenshotFolderSource.exists || !screenshotFolderSource.readable}
                    onClick={() => void runScreenshotFolderScan()}
                    className="border border-retro-text/20 px-2 py-1 text-[9px] uppercase tracking-wider text-retro-text/70 hover:text-retro-text disabled:opacity-40"
                  >
                    {screenshotFolderScanning ? "Scanning" : "Scan folder"}
                  </button>
                  <button
                    type="button"
                    disabled={screenshotFolderClearingStale || screenshotFolderStaleCount <= 0}
                    onClick={() => void clearStaleScreenshotFolderObservations()}
                    className="border border-retro-text/20 px-2 py-1 text-[9px] uppercase tracking-wider text-retro-text/70 hover:text-retro-text disabled:opacity-40"
                  >
                    {screenshotFolderClearingStale ? "Clearing" : "Clear stale"}
                  </button>
                  <div className="text-[9px] text-retro-text/40">
                    {screenshotFolderLockedByEnv ? "locked by env" : "local scan only"}
                  </div>
                </div>
                {screenshotFolderScanError && (
                  <div className="text-[9px] text-red-400">{screenshotFolderScanError}</div>
                )}
                {screenshotFolderScanResult && (
                  <div className="border border-retro-text/10 px-2 py-1 text-[9px] text-retro-text/60">
                    scanned {screenshotFolderScanResult.scanned ?? 0} · added {screenshotFolderScanResult.ingested ?? 0}
                    {" · "}duplicates {screenshotFolderScanResult.skipped_duplicates ?? 0}
                    {(screenshotFolderScanResult.rejected?.length ?? 0) > 0
                      ? ` · rejected ${screenshotFolderScanResult.rejected?.length ?? 0}`
                      : ""}
                  </div>
                )}
              </div>
            )}

            <div className="grid grid-cols-[92px_minmax(0,1fr)] gap-2 text-[9px]">
              <div className="text-retro-text/30 uppercase tracking-wider">Provider</div>
              <select
                value={settings.screen.provider}
                disabled={saving || !screenshotFolderMetadataLoaded}
                onChange={(event) => void updateScreenAnalysis({ provider: event.target.value })}
                className="min-w-0 border border-retro-text/20 bg-retro-bg px-1 py-0.5 text-retro-text"
              >
                <option value="">not set</option>
                <option value="openrouter">openrouter</option>
              </select>
            </div>

            {settings.inference && (
              <div className="border-t border-retro-text/10 pt-2 mt-1">
                <div className="flex items-center justify-between gap-2 mb-1">
                  <div className="text-[10px] text-retro-text">Inference gateway</div>
                  <div className={`text-[9px] uppercase tracking-wider ${
                    settings.inference.status === "ready" ? "text-green-400" : "text-yellow-400"
                  }`}>
                    {settings.inference.status.replace(/_/g, " ")}
                  </div>
                </div>
                <ArtifactRow
                  label="Provider"
                  value={`${settings.inference.provider} · ${settings.inference.active_only ? "only active" : "phase disabled"}`}
                  tone={settings.inference.active_only ? "good" : "warn"}
                />
                <ArtifactRow
                  label="Route"
                  value={`${settings.inference.api_base} · ${settings.inference.credential_configured ? "key configured" : "key missing"}`}
                  tone={settings.inference.credential_configured ? "good" : "warn"}
                />
                <ArtifactRow
                  label="Upstreams"
                  value={settings.inference.allowed_upstreams.length > 0 ? settings.inference.allowed_upstreams.join(", ") : "allow-list required"}
                  tone={settings.inference.allowed_upstreams.length > 0 ? "good" : "warn"}
                />
                <ArtifactRow
                  label="Data policy"
                  value={`${settings.inference.data_collection} · ZDR ${settings.inference.zero_data_retention ? "on" : "off"} · fallbacks ${settings.inference.fallbacks_allowed ? "on" : "off"}`}
                  tone={settings.inference.data_collection === "deny" && !settings.inference.fallbacks_allowed ? "good" : "warn"}
                />
                <ArtifactRow
                  label="Consent"
                  value={`${settings.inference.chat_cloud_egress} · ${settings.inference.chat_cloud_consent ? "acknowledged" : "acknowledgement required"}`}
                  tone={settings.inference.chat_cloud_consent ? "good" : "warn"}
                />
              </div>
            )}

            <div className="border-t border-retro-text/10 pt-2 mt-1">
              <div className="flex items-center justify-between gap-2 mb-1">
                <div className="text-[10px] text-retro-text">Model fabric</div>
                <div className={`text-[9px] uppercase tracking-wider ${
                  modelFabricStale || modelFabric?.status === "degraded" || modelFabricRuntime?.status === "degraded"
                    ? "text-yellow-400"
                    : modelFabric
                      ? "text-green-400"
                      : "text-retro-text/40"
                }`}>
                  {modelFabricStale ? "stale" : modelFabricRuntime?.status ?? modelFabric?.status ?? "loading"}
                </div>
              </div>
              {modelFabricError && (
                <div className="border border-yellow-400/40 px-2 py-1 mb-1 text-[9px] text-yellow-400">
                  {modelFabricError}
                </div>
              )}
              <ArtifactRow
                label="Topology"
                value={
                  modelFabricRuntime
                    ? `text: ${modelFabricRuntime.topology.text.join(", ") || "none"} · VLM: ${modelFabricRuntime.topology.vlm.join(", ") || "none"}`
                    : "text and screenshot VLM route receipts unavailable"
                }
                tone={modelFabricRuntime ? "good" : "normal"}
              />
              <ArtifactRow
                label="Configured"
                value={
                  modelFabric?.profiles.length
                    ? modelFabric.profiles.map((profile) => (
                        `${profile.id}/${profile.model}:${profile.routable ? "routable" : `not routable (${profile.non_routable_reasons.join(", ") || profile.model_fabric_exclusion_reason || "unknown"})`}:cost ${profile.cost_source ?? "unknown"}`
                      )).join(" · ")
                    : "no candidates"
                }
                tone={modelFabric?.profiles.some((profile) => profile.routable) ? "good" : "warn"}
              />
              <ArtifactRow
                label="Excluded"
                value={
                  modelFabric?.excluded_profiles.length
                    ? modelFabric.excluded_profiles.map((profile) => (
                        `${profile.id}/${profile.model}:${profile.model_fabric_exclusion_reason ?? (profile.non_routable_reasons.join(", ") || "excluded")}`
                      )).join(" · ")
                    : "none"
                }
                tone={modelFabric?.excluded_profiles.length ? "warn" : "good"}
              />
              <ArtifactRow
                label="Text"
                value={modelFabricWorkloadLabel(modelFabricRuntime, "chat_agent")}
                tone={modelFabricWorkloadTone(modelFabricRuntime, "chat_agent")}
              />
              <ArtifactRow
                label="VLM"
                value={modelFabricWorkloadLabel(modelFabricRuntime, "screenshot_image_analysis")}
                tone={modelFabricWorkloadTone(modelFabricRuntime, "screenshot_image_analysis")}
              />
              <ArtifactRow
                label="Fallback"
                value={
                  `${(modelFabricRuntime?.runtime_paths.chat_agent ?? modelFabricRuntime?.workloads.interactive)?.fallback_used ? `last used:${(modelFabricRuntime?.runtime_paths.chat_agent ?? modelFabricRuntime?.workloads.interactive)?.fallback_reason_code ?? "reason unknown"}` : "last not used"} · ` +
                  (modelFabric?.workload_policies.length
                    ? modelFabric.workload_policies.map((policy) => `${policy.runtime_path}:${policy.fallback_allowed ? "allowed" : "blocked"}`).join(" · ")
                    : `default ${modelFabric?.defaults.fallback_allowed ? "allowed" : "blocked"}`)
                }
                tone={modelFabric?.workload_policies.some((policy) => policy.fallback_allowed) ? "normal" : "good"}
              />
              <ArtifactRow
                label="Proofs"
                value={modelFabricProofLabel(modelFabricRuntime)}
                tone={
                  modelFabricRuntime?.proofs.some((proof) => proof.status === "stale" || proof.status === "missing")
                    ? "warn"
                    : modelFabricRuntime?.proofs.length
                      ? "good"
                      : "normal"
                }
              />
              <OpenRouterSetupPanel
                setup={modelFabric?.openrouter_setup}
                stale={modelFabricStale}
                onSave={saveOpenRouterSetup}
              />
              <div className="mt-2 border border-retro-text/10 px-2 py-2">
                <div className="text-[9px] text-retro-text/50 mb-1">
                  Manual exact-route canary. This runs inference only when you press Run; status refreshes never probe.
                </div>
                <div className="flex flex-wrap items-center gap-1">
                  <label className="text-[9px] text-retro-text/40" htmlFor="model-fabric-canary-profile">Profile</label>
                  <select
                    id="model-fabric-canary-profile"
                    aria-label="Canary profile"
                    value={canaryProfile}
                    disabled={canaryRunning || !modelFabric}
                    onChange={(event) => setCanaryProfile(event.target.value)}
                    className="min-w-0 border border-retro-text/20 bg-retro-bg px-1 py-0.5 text-[9px] text-retro-text disabled:opacity-40"
                  >
                    <option value="">choose profile</option>
                    {modelFabric?.profiles.map((profile) => (
                      <option key={profile.id} value={profile.id}>
                        {profile.id} · {profile.transport_adapter}
                      </option>
                    ))}
                  </select>
                  <label className="text-[9px] text-retro-text/40" htmlFor="model-fabric-canary-capability">Capability</label>
                  <select
                    id="model-fabric-canary-capability"
                    aria-label="Canary capability"
                    value={selectedCanaryCapability}
                    disabled={canaryRunning || !modelFabric}
                    onChange={(event) => setCanaryCapability(event.target.value)}
                    className="min-w-0 border border-retro-text/20 bg-retro-bg px-1 py-0.5 text-[9px] text-retro-text disabled:opacity-40"
                  >
                    {availableCanaryCapabilities.map((capability) => (
                      <option key={capability} value={capability}>{capability.replace(/_/g, " ")}</option>
                    ))}
                  </select>
                  <button
                    type="button"
                    disabled={canaryRunning || !canaryProfile || modelFabricStale}
                    onClick={() => void runModelFabricCanary()}
                    className="border border-retro-text/20 px-2 py-1 text-[9px] uppercase tracking-wider text-retro-text/70 hover:text-retro-text disabled:opacity-40"
                  >
                    {canaryRunning ? "Running" : "Run canary"}
                  </button>
                </div>
                {canaryError && <div className="mt-1 text-[9px] text-red-400">{canaryError}</div>}
                {canaryResult && (
                  <div className={`mt-1 text-[9px] ${isGreenModelFabricCanary(canaryResult) ? "text-green-400" : "text-yellow-400"}`}>
                    {canaryResult.profile_id}/{canaryResult.capability} · {canaryResult.outcome}
                    {canaryResult.error_code ? ` · ${canaryResult.error_code}` : ""}
                    {canaryResult.proof ? ` · proof ${canaryResult.proof.proof_hash.slice(0, 12)}` : " · no proof"}
                    {` · receipt ${canaryResult.receipt_persistence}`}
                    {` · proof persistence ${canaryResult.proof_persistence}`}
                    {!isGreenModelFabricCanary(canaryResult) ? " · not authorizing" : ""}
                  </div>
                )}
              </div>
            </div>

            {settings.local_runtime && (
              <div className="border-t border-retro-text/10 pt-2 mt-1">
                <div className="flex items-center justify-between gap-2 mb-1">
                  <div className="text-[10px] text-retro-text">
                    {settings.local_runtime.active === false ? "Local runtime (inactive)" : "Local Gemma runtime"}
                  </div>
                  <div
                    className={`text-[9px] uppercase tracking-wider ${
                      localRuntimeProofTone(settings.local_runtime.profile_proof) === "good"
                        ? "text-green-400"
                        : localRuntimeProofTone(settings.local_runtime.profile_proof) === "warn"
                          ? "text-yellow-400"
                          : "text-retro-text/40"
                    }`}
                  >
                    {settings.local_runtime.active === false
                      ? "disabled · OpenRouter-only"
                      : settings.local_runtime.profile_proof.status.replace(/_/g, " ")}
                  </div>
                </div>
                <ArtifactRow
                  label="Gateway"
                  value={settings.local_runtime.gateway_configured ? "configured" : "not configured"}
                  tone={settings.local_runtime.gateway_configured ? "good" : "warn"}
                />
                <ArtifactRow
                  label="Model"
                  value={settings.local_runtime.model || "not configured"}
                  tone={settings.local_runtime.model ? "good" : "warn"}
                />
                <ArtifactRow
                  label="Reach"
                  value={vlmReachabilityLabel(settings.local_runtime.vlm_runtime)}
                  tone={vlmReachabilityTone(settings.local_runtime.vlm_runtime)}
                />
                <ArtifactRow
                  label="Proof"
                  value={settings.local_runtime.profile_proof.per_request_reasoning_control.replace(/_/g, " ")}
                  tone={localRuntimeProofTone(settings.local_runtime.profile_proof)}
                />
                <ArtifactRow
                  label="Routing"
                  value={
                    settings.local_runtime.profile_proof.safe_for_single_backend_profile_routing
                      ? "single backend profile routing safe"
                      : "single backend profile routing not safe"
                  }
                  tone={localRuntimeProofTone(settings.local_runtime.profile_proof)}
                />
                <ArtifactRow
                  label="Profiles"
                  value={
                    settings.local_runtime.profiles.length > 0
                      ? settings.local_runtime.profiles
                          .map((profile) => `${profile.id}:${profile.priority}/${profile.reasoning}`)
                          .join(" · ")
                      : "none"
                  }
                  tone={settings.local_runtime.profiles.length > 0 ? "good" : "normal"}
                />
                <ArtifactRow
                  label="Receipt"
                  value={
                    settings.local_runtime.profile_proof.last_receipt_sha256
                      ? `${settings.local_runtime.profile_proof.last_receipt_sha256.slice(0, 12)} · ${settings.local_runtime.profile_proof.last_receipt_at ?? "time unknown"}`
                      : `${settings.local_runtime.profile_proof.receipt_count} receipts`
                  }
                />
                {settings.local_runtime.profile_proof.notes.length > 0 && (
                  <ArtifactRow
                    label="Note"
                    value={settings.local_runtime.profile_proof.notes.slice(0, 2).join(" · ")}
                    tone="warn"
                  />
                )}
              </div>
            )}

            <div className="border-t border-retro-text/10 pt-2 mt-1">
              <div className="flex items-center justify-between gap-2 mb-1">
                <div className="text-[10px] text-retro-text">End-of-day reports</div>
                <div className={`text-[9px] uppercase tracking-wider ${settings.reports.enabled ? "text-green-400" : "text-retro-text/40"}`}>
                  {boolLabel(settings.reports.enabled)}
                </div>
              </div>
              <ArtifactRow label="Report dir" value={settings.reports.archive_dir} />
              <ArtifactRow
                label="Dir state"
                value={dirStateLabel(settings.reports.exists, settings.reports.writable, settings.reports.creation_error)}
                tone={dirStateTone(settings.reports.exists, settings.reports.writable, settings.reports.creation_error)}
              />
              <ArtifactRow label="Provider" value={settings.reports.analysis_provider} />
              <ArtifactRow label="Hour" value={`${settings.reports.hour}:00`} />
              <ArtifactRow
                label="Receipts"
                value={`${settings.reports.receipt_count ?? 0} receipts${settings.reports.last_receipt_at ? ` · latest ${settings.reports.last_receipt_at}` : ""}`}
              />
              <div className="flex flex-wrap gap-1 pt-1">
                <button
                  type="button"
                  disabled={reportAction !== "idle"}
                  onClick={() => void runReportAction("preview")}
                  className="border border-retro-text/20 px-2 py-1 text-[9px] uppercase tracking-wider text-retro-text/70 hover:text-retro-text disabled:opacity-40"
                >
                  Preview
                </button>
                  <button
                    type="button"
                    disabled={reportAction !== "idle" || !settings.email.enabled || !previewReadyForSend}
                    onClick={() => void runReportAction("send")}
                    className="border border-retro-text/20 px-2 py-1 text-[9px] uppercase tracking-wider text-retro-text/70 hover:text-retro-text disabled:opacity-40"
                  >
                  Send
                </button>
                <button
                  type="button"
                  disabled={reportAction !== "idle" || !settings.email.enabled}
                  onClick={() => void runReportAction("test")}
                  className="border border-retro-text/20 px-2 py-1 text-[9px] uppercase tracking-wider text-retro-text/70 hover:text-retro-text disabled:opacity-40"
                >
                  Test
                </button>
              </div>
              {reportActionError && (
                <div className="text-[9px] text-red-400">{reportActionError}</div>
              )}
              {reportActionResult && (
                <div className="border border-retro-text/10 px-2 py-1 text-[9px] text-retro-text/60">
                  {(reportActionResult.action ?? reportActionResult.status ?? "report")} ·{" "}
                  {reportActionResult.email?.status ?? reportActionResult.status ?? "ok"}
                  {reportActionResult.email?.recipient_hash || reportActionResult.recipient_hash
                    ? ` · recipient ${reportActionResult.email?.recipient_hash ?? reportActionResult.recipient_hash}`
                    : ""}
                  {reportActionResult.receipt?.receipt_sha256
                    ? ` · receipt ${reportActionResult.receipt.receipt_sha256.slice(0, 12)}`
                    : ""}
                </div>
              )}
            </div>

            <div className="border-t border-retro-text/10 pt-2 mt-1">
              <div className="text-[10px] text-retro-text mb-1">Email delivery</div>
              <div className="flex flex-col gap-1">
                <ArtifactRow
                  label="Email"
                  value={boolLabel(settings.email.enabled)}
                  tone={settings.email.enabled ? "good" : "normal"}
                />
                <ArtifactRow
                  label="Preview"
                  value={settings.email.preview_required ? "Required" : "Not required"}
                  tone={settings.email.preview_required ? "warn" : "normal"}
                />
                <ArtifactRow
                  label="SMTP"
                  value={settings.email.smtp_configured ? "Configured" : "Missing"}
                  tone={settings.email.smtp_configured ? "good" : "warn"}
                />
                <ArtifactRow
                  label="Sender"
                  value={settings.email.sender_configured ? "Configured" : "Missing"}
                  tone={settings.email.sender_configured ? "good" : "warn"}
                />
                <ArtifactRow
                  label="Allowlist"
                  value={settings.email.allowlist_configured ? "Configured" : "Missing"}
                  tone={settings.email.allowlist_configured ? "good" : "warn"}
                />
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
