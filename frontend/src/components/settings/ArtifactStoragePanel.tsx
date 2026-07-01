import { useEffect, useRef, useState } from "react";
import { API_URL } from "../../config/constants";

interface ArtifactStorageSettings {
  screen: {
    analysis_enabled: boolean;
    provider: string;
    model: string;
  };
  screenshot_folder?: {
    enabled: boolean;
    provider: string;
    path: string;
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
      observation_count: number;
      analysis_status: Record<string, number>;
      analysis_backlog: number;
      analysis_failures: number;
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
    gateway_configured: boolean;
    llm_base_url_configured: boolean;
    vlm_base_url_configured: boolean;
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
}

const FALLBACK_SCREEN_ANALYSIS_SETTINGS: ScreenAnalysisSettings = {
  enabled: true,
  provider: "codex-local",
  model: "",
};

const ARTIFACT_METADATA_TIMEOUT_MS = import.meta.env.MODE === "test" ? 100 : 30_000;

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

function boolLabel(value: boolean): string {
  return value ? "On" : "Off";
}

function sourceLabel(value: string): string {
  return value === "default" ? "default" : value;
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
  if (analysis.analysis_failures > 0) return "warn";
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
      path: screen.screenshot_folder ?? "Seraph workspace artifacts/screenshot-folder",
      path_source: screen.screenshot_folder ? "screen-analysis-settings" : "default",
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

export function ArtifactStoragePanel() {
  const mountedRef = useRef(true);
  const fetchGenerationRef = useRef(0);
  const fallbackRetryRef = useRef(0);
  const [settings, setSettings] = useState<ArtifactStorageSettings | null>(
    () => settingsFromScreenAnalysis(FALLBACK_SCREEN_ANALYSIS_SETTINGS),
  );
  const [failed, setFailed] = useState(false);
  const [metadataWarning, setMetadataWarning] = useState<string | null>(
    "Settings metadata is loading; folder controls are available.",
  );
  const [saving, setSaving] = useState(false);
  const [reportAction, setReportAction] = useState<"idle" | "previewing" | "sending" | "testing">("idle");
  const [reportActionResult, setReportActionResult] = useState<ReportActionResult | null>(null);
  const [reportActionError, setReportActionError] = useState<string | null>(null);
  const [screenshotFolderScanning, setScreenshotFolderScanning] = useState(false);
  const [screenshotFolderScanResult, setScreenshotFolderScanResult] = useState<ScreenshotFolderScanResult | null>(null);
  const [screenshotFolderScanError, setScreenshotFolderScanError] = useState<string | null>(null);
  const [screenshotFolderDraft, setScreenshotFolderDraft] = useState("");

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
        setSettings(settingsFromScreenAnalysis(FALLBACK_SCREEN_ANALYSIS_SETTINGS));
        setFailed(false);
        setMetadataWarning("Settings metadata is temporarily unavailable; folder controls are still editable.");
        if (fallbackRetryRef.current < 12) {
          fallbackRetryRef.current += 1;
          window.setTimeout(() => {
            if (mountedRef.current && fetchGenerationRef.current === generation) void fetchSettings(() => !mountedRef.current);
          }, 5_000);
        }
      }
    }
  }
  useEffect(() => {
    let cancelled = false;
    mountedRef.current = true;

    void fetchSettings(() => cancelled);
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
  const screenshotFolderPath = screenshotFolderSource?.path ?? "";
  const screenshotFolderPathSource = screenshotFolderSource?.path_source ?? "";
  const screenshotFolderLockedByEnv = screenshotFolderPathSource === "SERAPH_SCREENSHOT_FOLDER";
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
    if (screenshotFolderSource !== null) setScreenshotFolderDraft(screenshotFolderPath);
  }, [screenshotFolderPath, screenshotFolderSource]);

  const saveScreenshotFolder = async () => {
    if (screenshotFolderSource === null || screenshotFolderLockedByEnv) return;
    setScreenshotFolderScanResult(null);
    setScreenshotFolderScanError(null);
    await updateScreenAnalysis({ screenshot_folder: screenshotFolderDraft.trim() });
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
                <div className="grid grid-cols-[92px_minmax(0,1fr)_auto] gap-2 text-[9px] items-center">
                  <div className="text-retro-text/30 uppercase tracking-wider">Folder</div>
                  <input
                    aria-label="Screenshot folder"
                    value={screenshotFolderDraft}
                    disabled={saving || screenshotFolderLockedByEnv}
                    onChange={(event) => setScreenshotFolderDraft(event.target.value)}
                    className="min-w-0 border border-retro-text/20 bg-retro-bg px-1 py-0.5 text-retro-text disabled:opacity-50"
                  />
                  <button
                    type="button"
                    disabled={
                      saving ||
                      screenshotFolderLockedByEnv ||
                      screenshotFolderDraft.trim() === screenshotFolderPath
                    }
                    onClick={() => void saveScreenshotFolder()}
                    className="border border-retro-text/20 px-2 py-1 uppercase tracking-wider text-retro-text/70 hover:text-retro-text disabled:opacity-40"
                  >
                    Save
                  </button>
                </div>
                <ArtifactRow label="Folder" value={screenshotFolderPath} />
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
                        `${screenshotFolderSource.analysis.analysis_failures} failed`
                      }
                      tone={screenshotAnalysisTone(screenshotFolderSource.analysis)}
                    />
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
                    disabled={screenshotFolderScanning || !screenshotFolderSource.exists || !screenshotFolderSource.readable}
                    onClick={() => void runScreenshotFolderScan()}
                    className="border border-retro-text/20 px-2 py-1 text-[9px] uppercase tracking-wider text-retro-text/70 hover:text-retro-text disabled:opacity-40"
                  >
                    {screenshotFolderScanning ? "Scanning" : "Scan folder"}
                  </button>
                  <div className="text-[9px] text-retro-text/40">
                    local scan only
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
                disabled={saving}
                onChange={(event) => void updateScreenAnalysis({ provider: event.target.value })}
                className="min-w-0 border border-retro-text/20 bg-retro-bg px-1 py-0.5 text-retro-text"
              >
                <option value="codex-local">codex-local</option>
                <option value="apple-vision">apple-vision</option>
                <option value="local-vlm">local-vlm</option>
                <option value="openrouter">openrouter</option>
              </select>
            </div>

            {settings.local_runtime && (
              <div className="border-t border-retro-text/10 pt-2 mt-1">
                <div className="flex items-center justify-between gap-2 mb-1">
                  <div className="text-[10px] text-retro-text">Local Gemma runtime</div>
                  <div
                    className={`text-[9px] uppercase tracking-wider ${
                      localRuntimeProofTone(settings.local_runtime.profile_proof) === "good"
                        ? "text-green-400"
                        : localRuntimeProofTone(settings.local_runtime.profile_proof) === "warn"
                          ? "text-yellow-400"
                          : "text-retro-text/40"
                    }`}
                  >
                    {settings.local_runtime.profile_proof.status.replace(/_/g, " ")}
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
