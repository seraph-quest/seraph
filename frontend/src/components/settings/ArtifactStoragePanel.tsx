import { useEffect, useRef, useState } from "react";
import { API_URL } from "../../config/constants";

interface ArtifactStorageSettings {
  screen: {
    analysis_enabled: boolean;
    provider: string;
    model: string;
    capture_mode: string;
    cadence_seconds: number | null;
    daemon_connected: boolean;
    daemon_alive: boolean;
    artifact_count: number;
    last_artifact_at: string | null;
    budget: {
      min_seconds_between_captures: number;
      max_daily_captures: number;
      archive_retention_days: number;
      archive_max_mb: number;
    };
    preservation_enabled: boolean;
    archive_dir: string;
    archive_dir_source: string;
    exists: boolean;
    writable: boolean;
    creation_error: string | null;
    stored_artifacts: string[];
    inspection_endpoint: string;
    inspection_visibility: string;
    daemon_status: {
      state: string | null;
      screen_analysis: string | null;
      capture_ready: boolean;
      last_error: string | null;
      last_error_kind: string | null;
      updated_at: string | null;
      active_window?: string | null;
      frontmost_app?: string | null;
      window_title?: string | null;
      last_poll_at?: string | null;
      last_capture_at?: string | null;
      last_context_post_at?: string | null;
      status_source: string;
    };
    control_env: Record<string, string>;
  };
  framekeeper?: {
    enabled: boolean;
    provider: string;
    screenshot_folder?: string;
    screenshot_folder_source?: string;
    artifact_root: string;
    artifact_root_source: string;
    image_count: number;
    last_image_at: string | null;
    status: string;
    exists: boolean;
    readable: boolean;
    stored_artifacts: string[];
    auto_ingest_enabled: boolean;
    auto_ingest_interval_min: number;
    auto_ingest_limit: number;
    ingest_endpoint: string;
    inspection_endpoint: string;
    inspection_visibility: string;
    control_env: Record<string, string>;
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
  preserve_captures: boolean;
  archive_dir: string;
  framekeeper_screenshot_folder?: string;
  framekeeper_artifact_root?: string;
  capture_mode: string;
  cadence_seconds: number | null;
  daemon_connected: boolean;
  daemon_alive: boolean;
  artifact_count: number;
  last_artifact_at: string | null;
  min_seconds_between_captures: number;
  max_daily_captures: number;
  archive_retention_days: number;
  archive_max_mb: number;
}

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

interface FramekeeperIngestResult {
  artifact_root?: string;
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

function captureStateLabel(settings: ArtifactStorageSettings["screen"]): string {
  if (!settings.analysis_enabled) return "disabled";
  if (settings.daemon_status.last_error) return settings.daemon_status.last_error;
  if (!settings.daemon_connected) return "daemon offline";
  if (settings.capture_mode === "on_switch") return "waiting for app/window switch";
  if (settings.daemon_status.capture_ready) return "ready";
  return "waiting for next capture";
}

function captureStateTone(settings: ArtifactStorageSettings["screen"]): "normal" | "good" | "warn" {
  if (settings.daemon_status.last_error || !settings.daemon_connected) return "warn";
  if (settings.capture_mode === "on_switch") return "normal";
  return settings.daemon_status.capture_ready ? "good" : "warn";
}

function framekeeperStateTone(settings: NonNullable<ArtifactStorageSettings["framekeeper"]>): "normal" | "good" | "warn" {
  if (!settings.exists || !settings.readable || settings.status === "invalid_root" || settings.status === "read_error") {
    return "warn";
  }
  return settings.image_count > 0 ? "good" : "normal";
}

function isArtifactStorageSettings(value: unknown): value is ArtifactStorageSettings {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Partial<ArtifactStorageSettings>;
  return (
    typeof candidate.screen?.archive_dir === "string" &&
    typeof candidate.screen?.analysis_enabled === "boolean" &&
    typeof candidate.screen?.provider === "string" &&
    typeof candidate.screen?.preservation_enabled === "boolean" &&
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
    typeof candidate.model === "string" &&
    typeof candidate.preserve_captures === "boolean" &&
    typeof candidate.archive_dir === "string" &&
    typeof candidate.capture_mode === "string" &&
    typeof candidate.daemon_connected === "boolean"
  );
}

async function fetchJsonWithTimeout(path: string, timeoutMs = 3_000): Promise<unknown> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${API_URL}${path}`, { signal: controller.signal });
    if (!response.ok) throw new Error(`Request failed: ${response.status}`);
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
      capture_mode: screen.capture_mode,
      cadence_seconds: screen.cadence_seconds,
      daemon_connected: screen.daemon_connected,
      daemon_alive: screen.daemon_alive ?? screen.daemon_connected,
      artifact_count: screen.artifact_count ?? 0,
      last_artifact_at: screen.last_artifact_at ?? null,
      budget: {
        min_seconds_between_captures: screen.min_seconds_between_captures ?? 0,
        max_daily_captures: screen.max_daily_captures ?? 0,
        archive_retention_days: screen.archive_retention_days ?? 365,
        archive_max_mb: screen.archive_max_mb ?? 0,
      },
      preservation_enabled: screen.preserve_captures,
      archive_dir: screen.archive_dir,
      archive_dir_source: "screen-analysis-settings",
      exists: true,
      writable: true,
      creation_error: null,
      stored_artifacts: ["image", "provider_output", "analysis_json"],
      inspection_endpoint: "/api/observer/screen-artifacts",
      inspection_visibility: "localhost_only",
      daemon_status: {
        state: screen.daemon_connected ? "running" : "unknown",
        screen_analysis: screen.enabled ? "active" : "disabled",
        capture_ready: screen.daemon_connected,
        last_error: null,
        last_error_kind: null,
        updated_at: null,
        status_source: "screen-analysis",
      },
      control_env: {
        enabled: "SERAPH_PRESERVE_SCREEN_CAPTURES",
        archive_dir: "SERAPH_SCREEN_CAPTURE_ARCHIVE_DIR or SCREEN_CAPTURE_ARCHIVE_DIR",
      },
    },
    framekeeper: {
      enabled: true,
      provider: "framekeeper",
      screenshot_folder: screen.framekeeper_screenshot_folder ?? screen.framekeeper_artifact_root ?? "~/Library/Application Support/Framekeeper/artifacts",
      screenshot_folder_source: (screen.framekeeper_screenshot_folder ?? screen.framekeeper_artifact_root) ? "screen-analysis-settings" : "default",
      artifact_root: screen.framekeeper_screenshot_folder ?? screen.framekeeper_artifact_root ?? "~/Library/Application Support/Framekeeper/artifacts",
      artifact_root_source: (screen.framekeeper_screenshot_folder ?? screen.framekeeper_artifact_root) ? "screen-analysis-settings" : "default",
      image_count: 0,
      last_image_at: null,
      status: "metadata unavailable",
      exists: false,
      readable: false,
      stored_artifacts: ["image"],
      auto_ingest_enabled: true,
      auto_ingest_interval_min: 5,
      auto_ingest_limit: 100,
      ingest_endpoint: "/api/observer/framekeeper/ingest",
      inspection_endpoint: "/api/observer/screen-artifacts",
      inspection_visibility: "localhost_only",
      control_env: {
        screenshot_folder: "SERAPH_FRAMEKEEPER_SCREENSHOT_FOLDER",
        artifact_root: "SERAPH_FRAMEKEEPER_ARTIFACT_ROOT",
        auto_ingest_enabled: "FRAMEKEEPER_INGEST_ENABLED",
        auto_ingest_interval: "FRAMEKEEPER_INGEST_INTERVAL_MIN",
        auto_ingest_limit: "FRAMEKEEPER_INGEST_LIMIT",
      },
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
  const [settings, setSettings] = useState<ArtifactStorageSettings | null>(null);
  const [failed, setFailed] = useState(false);
  const [metadataWarning, setMetadataWarning] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [reportAction, setReportAction] = useState<"idle" | "previewing" | "sending" | "testing">("idle");
  const [reportActionResult, setReportActionResult] = useState<ReportActionResult | null>(null);
  const [reportActionError, setReportActionError] = useState<string | null>(null);
  const [framekeeperIngesting, setFramekeeperIngesting] = useState(false);
  const [framekeeperIngestResult, setFramekeeperIngestResult] = useState<FramekeeperIngestResult | null>(null);
  const [framekeeperIngestError, setFramekeeperIngestError] = useState<string | null>(null);
  const [framekeeperRootDraft, setFramekeeperRootDraft] = useState("");

  async function fetchSettings(isCancelled: () => boolean = () => !mountedRef.current) {
    const generation = fetchGenerationRef.current + 1;
    fetchGenerationRef.current = generation;
    const canPublish = () => !isCancelled() && fetchGenerationRef.current === generation;
    try {
      let data: ArtifactStorageSettings | null = null;
      let hasArtifactMetadata = false;
      try {
        const screenData = await fetchJsonWithTimeout("/api/settings/screen-analysis");
        if (isScreenAnalysisSettings(screenData)) {
          data = settingsFromScreenAnalysis(screenData);
        } else if (isArtifactStorageSettings(screenData)) {
          data = screenData;
          hasArtifactMetadata = true;
        } else {
          throw new Error("Screen analysis settings response is invalid.");
        }
      } catch {
        const artifactData = await fetchJsonWithTimeout("/api/settings/artifact-storage");
        if (!isArtifactStorageSettings(artifactData)) throw new Error("Artifact storage response is invalid.");
        data = artifactData;
        hasArtifactMetadata = true;
      }
      if (data === null) throw new Error("Framekeeper source settings response is unavailable.");
      if (canPublish()) {
        setSettings(data);
        setMetadataWarning(hasArtifactMetadata ? null : "Archive metadata loading; analysis controls are live.");
        setFailed(false);
      }
      if (!hasArtifactMetadata) {
        void fetchJsonWithTimeout("/api/settings/artifact-storage")
          .then((artifactData) => {
            if (!canPublish()) return;
            if (isArtifactStorageSettings(artifactData)) {
              setSettings(artifactData);
              setMetadataWarning(null);
            } else {
              setMetadataWarning("Archive metadata degraded; analysis controls are still live.");
            }
          })
          .catch(() => {
            if (canPublish()) {
              setMetadataWarning("Archive metadata degraded; analysis controls are still live.");
            }
          });
      }
    } catch {
      if (canPublish()) {
        setFailed(true);
        setMetadataWarning(null);
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
      const response = await fetch(`${API_URL}/api/settings/screen-analysis`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
      if (!response.ok) {
        if (mountedRef.current) setMetadataWarning("Save failed; screen capture settings were not updated.");
        return;
      }
      if (!mountedRef.current) return;
      await fetchSettings(() => !mountedRef.current);
    } catch {
      if (mountedRef.current) setMetadataWarning("Save failed; screen capture settings were not updated.");
    } finally {
      if (mountedRef.current) setSaving(false);
    }
  };

  const updateCaptureMode = async (mode: string) => {
    if (settings === null || saving) return;
    setSaving(true);
    try {
      const response = await fetch(`${API_URL}/api/settings/capture-mode`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode }),
      });
      if (!response.ok) {
        if (mountedRef.current) setMetadataWarning("Save failed; capture mode was not updated.");
        return;
      }
      if (!mountedRef.current) return;
      await fetchSettings(() => !mountedRef.current);
    } catch {
      if (mountedRef.current) setMetadataWarning("Save failed; capture mode was not updated.");
    } finally {
      if (mountedRef.current) setSaving(false);
    }
  };

  const screenBudget = settings?.screen.budget ?? {
    min_seconds_between_captures: 0,
    max_daily_captures: 0,
    archive_retention_days: 365,
    archive_max_mb: 0,
  };
  const framekeeperSource = settings?.framekeeper ?? null;
  const framekeeperFolder = framekeeperSource?.screenshot_folder ?? framekeeperSource?.artifact_root ?? "";
  const framekeeperFolderSource = framekeeperSource?.screenshot_folder_source ?? framekeeperSource?.artifact_root_source ?? "";
  const framekeeperRootLockedByEnv = framekeeperFolderSource === "SERAPH_FRAMEKEEPER_SCREENSHOT_FOLDER" || framekeeperFolderSource === "SERAPH_FRAMEKEEPER_ARTIFACT_ROOT";
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
      const response = await fetch(
        `${API_URL}${action === "test" ? "/api/settings/end-of-day-report/test-email" : "/api/settings/end-of-day-report/manual"}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
              body: action === "test"
                ? undefined
                : JSON.stringify({
                    send_email: action === "send",
                    preview_acknowledged: action === "send" && previewReadyForSend,
                  }),
            }
          );
      if (!response.ok) throw new Error(`Request failed: ${response.status}`);
      const payload = (await response.json()) as ReportActionResult;
      if (!mountedRef.current) return;
      setReportActionResult(payload);
      await fetchSettings(() => !mountedRef.current);
    } catch {
      if (mountedRef.current) setReportActionError("Report action failed.");
    } finally {
      if (mountedRef.current) setReportAction("idle");
    }
  };

  const runFramekeeperIngest = async () => {
    if (framekeeperIngesting || framekeeperSource === null) return;
    setFramekeeperIngesting(true);
    setFramekeeperIngestError(null);
    try {
      const response = await fetch(`${API_URL}${framekeeperSource.ingest_endpoint}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ screenshot_folder: framekeeperFolder, limit: 100 }),
      });
      if (!response.ok) throw new Error(`Request failed: ${response.status}`);
      const payload = (await response.json()) as FramekeeperIngestResult;
      if (!mountedRef.current) return;
      setFramekeeperIngestResult(payload);
      await fetchSettings(() => !mountedRef.current);
    } catch {
      if (mountedRef.current) setFramekeeperIngestError("Framekeeper ingest failed.");
    } finally {
      if (mountedRef.current) setFramekeeperIngesting(false);
    }
  };

  useEffect(() => {
    if (framekeeperSource !== null) setFramekeeperRootDraft(framekeeperFolder);
  }, [framekeeperFolder, framekeeperSource]);

  const saveFramekeeperRoot = async () => {
    if (framekeeperSource === null || framekeeperRootLockedByEnv) return;
    setFramekeeperIngestResult(null);
    setFramekeeperIngestError(null);
    await updateScreenAnalysis({ framekeeper_screenshot_folder: framekeeperRootDraft.trim() });
  };

  return (
    <div className="px-1">
      <div className="text-[10px] uppercase tracking-wider text-retro-border font-bold mb-2">
        Framekeeper Source
      </div>
      <div className="border border-retro-text/10 rounded px-2 py-2 flex flex-col gap-2">
        {failed ? (
          <div className="flex flex-col gap-2">
            <div className="text-[9px] text-red-400">Framekeeper source settings unavailable.</div>
            <button
              type="button"
              onClick={() => void fetchSettings()}
              className="w-fit border border-retro-text/20 px-2 py-1 text-[9px] uppercase tracking-wider text-retro-text/60 hover:text-retro-text"
            >
              Retry
            </button>
          </div>
        ) : settings === null ? (
          <div className="text-[9px] text-retro-text/40">Loading Framekeeper source settings...</div>
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
                  consumes Framekeeper screenshots and keeps reports local-first
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

            {framekeeperSource && (
              <div className="border border-retro-text/10 px-2 py-2 flex flex-col gap-1">
                <div className="flex items-center justify-between gap-2">
                  <div className="text-[10px] text-retro-text">Framekeeper screenshots</div>
                  <div className={`text-[9px] uppercase tracking-wider ${
                    framekeeperStateTone(framekeeperSource) === "good"
                      ? "text-green-400"
                      : framekeeperStateTone(framekeeperSource) === "warn"
                        ? "text-yellow-400"
                        : "text-retro-text/50"
                  }`}>
                    {framekeeperSource.status.replace(/_/g, " ")}
                  </div>
                </div>
                <div className="grid grid-cols-[92px_minmax(0,1fr)_auto] gap-2 text-[9px] items-center">
                  <div className="text-retro-text/30 uppercase tracking-wider">Folder</div>
                  <input
                    aria-label="Framekeeper screenshot folder"
                    value={framekeeperRootDraft}
                    disabled={saving || framekeeperRootLockedByEnv}
                    onChange={(event) => setFramekeeperRootDraft(event.target.value)}
                    className="min-w-0 border border-retro-text/20 bg-retro-bg px-1 py-0.5 text-retro-text disabled:opacity-50"
                  />
                  <button
                    type="button"
                    disabled={
                      saving ||
                      framekeeperRootLockedByEnv ||
                      framekeeperRootDraft.trim() === framekeeperFolder
                    }
                    onClick={() => void saveFramekeeperRoot()}
                    className="border border-retro-text/20 px-2 py-1 uppercase tracking-wider text-retro-text/70 hover:text-retro-text disabled:opacity-40"
                  >
                    Save
                  </button>
                </div>
                <ArtifactRow label="Folder" value={framekeeperFolder} />
                <ArtifactRow label="Source" value={sourceLabel(framekeeperFolderSource)} />
                <ArtifactRow
                  label="Images"
                  value={`${framekeeperSource.image_count} images${framekeeperSource.last_image_at ? ` · latest ${framekeeperSource.last_image_at}` : ""}`}
                  tone={framekeeperStateTone(framekeeperSource)}
                />
                <ArtifactRow
                  label="Auto scan"
                  value={
                    framekeeperSource.auto_ingest_enabled
                      ? `every ${framekeeperSource.auto_ingest_interval_min}m · up to ${framekeeperSource.auto_ingest_limit} images`
                      : "off"
                  }
                  tone={framekeeperSource.auto_ingest_enabled ? "good" : "normal"}
                />
                <ArtifactRow label="Ingest" value={framekeeperSource.ingest_endpoint} tone="good" />
                <ArtifactRow label="Inspect" value={`${framekeeperSource.inspection_endpoint} (${framekeeperSource.inspection_visibility.replace(/_/g, " ")})`} />
                <div className="flex flex-wrap items-center gap-2 pt-1">
                  <button
                    type="button"
                    disabled={framekeeperIngesting || !framekeeperSource.exists || !framekeeperSource.readable}
                    onClick={() => void runFramekeeperIngest()}
                    className="border border-retro-text/20 px-2 py-1 text-[9px] uppercase tracking-wider text-retro-text/70 hover:text-retro-text disabled:opacity-40"
                  >
                    {framekeeperIngesting ? "Ingesting" : "Ingest now"}
                  </button>
                  <div className="text-[9px] text-retro-text/40">
                    local scan only
                  </div>
                </div>
                {framekeeperIngestError && (
                  <div className="text-[9px] text-red-400">{framekeeperIngestError}</div>
                )}
                {framekeeperIngestResult && (
                  <div className="border border-retro-text/10 px-2 py-1 text-[9px] text-retro-text/60">
                    scanned {framekeeperIngestResult.scanned ?? 0} · ingested {framekeeperIngestResult.ingested ?? 0}
                    {" · "}duplicates {framekeeperIngestResult.skipped_duplicates ?? 0}
                    {(framekeeperIngestResult.rejected?.length ?? 0) > 0
                      ? ` · rejected ${framekeeperIngestResult.rejected?.length ?? 0}`
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
                <option value="openrouter">openrouter</option>
              </select>
            </div>
            <div className="grid grid-cols-[92px_minmax(0,1fr)] gap-2 text-[9px]">
              <div className="text-retro-text/30 uppercase tracking-wider">Mode</div>
              <select
                value={settings.screen.capture_mode}
                disabled={saving}
                onChange={(event) => void updateCaptureMode(event.target.value)}
                className="min-w-0 border border-retro-text/20 bg-retro-bg px-1 py-0.5 text-retro-text"
              >
                <option value="on_switch">on_switch</option>
                <option value="balanced">balanced / 300s</option>
                <option value="detailed">detailed / 60s</option>
              </select>
            </div>
            <ArtifactRow
              label="Daemon"
              value={settings.screen.daemon_connected ? "linked" : settings.screen.daemon_alive ? "alive, waiting for context post" : "offline - no new captures"}
              tone={settings.screen.daemon_connected || settings.screen.daemon_alive ? "good" : "warn"}
            />
            <ArtifactRow
              label="Capture"
              value={captureStateLabel(settings.screen)}
              tone={captureStateTone(settings.screen)}
            />
            <ArtifactRow
              label="Active"
              value={settings.screen.daemon_status.active_window ?? "not observed"}
              tone={settings.screen.daemon_status.active_window ? "normal" : "warn"}
            />
            <ArtifactRow
              label="Last capture"
              value={settings.screen.daemon_status.last_capture_at ?? "none"}
              tone={settings.screen.daemon_status.last_capture_at ? "good" : "warn"}
            />
            <ArtifactRow
              label="Stored"
              value={`${settings.screen.artifact_count} captures${settings.screen.last_artifact_at ? ` · latest ${settings.screen.last_artifact_at}` : ""}`}
              tone={settings.screen.artifact_count > 0 ? "good" : "warn"}
            />
            <div className="grid grid-cols-[92px_minmax(0,1fr)] gap-2 text-[9px]">
              <div className="text-retro-text/30 uppercase tracking-wider">Preserve</div>
              <button
                type="button"
                disabled={saving}
                onClick={() => void updateScreenAnalysis({ preserve_captures: !settings.screen.preservation_enabled })}
                className={`w-fit border px-2 py-1 uppercase tracking-wider ${
                  settings.screen.preservation_enabled
                    ? "border-green-400 text-green-400"
                    : "border-retro-text/20 text-retro-text/40"
                }`}
              >
                {boolLabel(settings.screen.preservation_enabled)}
              </button>
            </div>
            <ArtifactRow label="Screen dir" value={settings.screen.archive_dir} />
            <ArtifactRow
              label="Budget"
              value={`min ${screenBudget.min_seconds_between_captures}s · day ${screenBudget.max_daily_captures || "unlimited"}`}
            />
            <ArtifactRow
              label="Retention"
              value={`${screenBudget.archive_retention_days}d · ${screenBudget.archive_max_mb || "unlimited"} MB`}
            />
            <ArtifactRow
              label="Dir state"
              value={dirStateLabel(settings.screen.exists, settings.screen.writable, settings.screen.creation_error)}
              tone={dirStateTone(settings.screen.exists, settings.screen.writable, settings.screen.creation_error)}
            />
            <ArtifactRow label="Source" value={sourceLabel(settings.screen.archive_dir_source)} />
            <ArtifactRow label="Status" value={sourceLabel(settings.screen.daemon_status.status_source)} />
            <ArtifactRow
              label="Inspect"
              value={`${settings.screen.inspection_endpoint} (${settings.screen.inspection_visibility.replace(/_/g, " ")})`}
              tone="good"
            />
            <ArtifactRow label="Enable via" value={settings.screen.control_env.enabled} />

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
