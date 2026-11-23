import { useEffect, useState } from "react";
import {
  ArtifactRow,
  boolLabel,
  captureStoreLabel,
  captureStoreTone,
  fetchJsonWithTimeout,
  screenshotAnalysisTone,
  screenshotFolderDisplayPath,
  screenshotFolderStateTone,
  sourceLabel,
  vlmReachabilityLabel,
  vlmReachabilityTone,
  type ArtifactStorageSettings,
  type ArtifactStorageState,
  type ScreenshotFolderPickResult,
  type ScreenshotFolderScanResult,
} from "./settingsData";

type FolderSource = NonNullable<ArtifactStorageSettings["screenshot_folder"]>;

/** The folder's plain first line (#1280 design §3): "On. 599 images, 599 waiting for analysis." */
export function screenshotFolderStatus(folder: FolderSource | null, loaded: boolean): string {
  if (!folder || !loaded) return "Checking the screenshot folder…";
  if (!folder.path || !folder.path.trim()) return "No folder chosen.";
  if (!folder.exists || !folder.readable) return "Seraph can't read this folder.";
  const lead = folder.auto_ingest_enabled ? "On." : "Automatic scanning is off.";
  const images = `${folder.image_count} image${folder.image_count === 1 ? "" : "s"}`;
  const waiting = folder.analysis?.folder_remaining_to_analyze ?? folder.analysis?.remaining_to_analyze ?? 0;
  return waiting > 0 ? `${lead} ${images}, ${waiting} waiting for analysis.` : `${lead} ${images}.`;
}

/**
 * The screenshot folder (#1280 PR 3): moved from the old first tab to
 * Automation. The path and folder picker stay on top; the analysis switch,
 * provider, counters, inspect address and scan buttons sit under Details.
 * Saves go to /api/settings/screen-analysis exactly as before.
 */
export function ScreenshotFolderPanel({ storage }: { storage: ArtifactStorageState }) {
  const { settings, failed, metadataWarning, saving, mountedRef, fetchSettings, updateScreenAnalysis } = storage;
  const [screenshotFolderScanning, setScreenshotFolderScanning] = useState(false);
  const [screenshotFolderScanResult, setScreenshotFolderScanResult] = useState<ScreenshotFolderScanResult | null>(null);
  const [screenshotFolderScanError, setScreenshotFolderScanError] = useState<string | null>(null);
  const [screenshotFolderPicking, setScreenshotFolderPicking] = useState(false);
  const [screenshotFolderClearingStale, setScreenshotFolderClearingStale] = useState(false);
  const [screenshotFolderDraft, setScreenshotFolderDraft] = useState("");

  const screenshotFolderSource = settings?.screenshot_folder ?? null;
  const screenshotFolderPath = screenshotFolderSource?.path ?? null;
  const screenshotFolderPathSource = screenshotFolderSource?.path_source ?? "";
  const screenshotFolderStaleCount = screenshotFolderSource?.analysis?.stale_count ?? 0;
  const screenshotFolderLockedByEnv = screenshotFolderPathSource === "SERAPH_SCREENSHOT_FOLDER";
  const screenshotFolderMetadataLoaded = Boolean(
    screenshotFolderSource &&
    screenshotFolderPathSource !== "not_loaded" &&
    screenshotFolderSource.status !== "metadata unavailable",
  );

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
            <div role="status" className="text-[11px] text-retro-text" data-testid="screenshot-folder-status">
              {screenshotFolderStatus(screenshotFolderSource, screenshotFolderMetadataLoaded)}
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
                <details className="text-[9px]" data-testid="screenshot-folder-details">
                  <summary className="cursor-pointer text-retro-text/60">Details</summary>
                  <div className="mt-2 flex flex-col gap-1">
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
                <div className="grid grid-cols-[92px_minmax(0,1fr)] gap-2 text-[9px]">
                  <div className="text-retro-text/30 uppercase tracking-wider">Provider</div>
                  <select
                    value={settings.screen.provider}
                    disabled={saving || !screenshotFolderMetadataLoaded}
                    onChange={(event) => void updateScreenAnalysis({ provider: event.target.value })}
                    className="min-w-0 border border-retro-text/20 bg-retro-bg px-1 py-0.5 text-retro-text"
                  >
                    <option value="">off · no screenshot analysis</option>
                    <option value="near_tee">NEAR TEE vision slot</option>
                    <option value="openrouter">openrouter</option>
                  </select>
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
                {screenshotFolderSource.capture_store && (
                  <ArtifactRow
                    label="Stored captures"
                    value={captureStoreLabel(screenshotFolderSource.capture_store)}
                    tone={captureStoreTone(screenshotFolderSource.capture_store)}
                  />
                )}
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
                </details>
              </div>
            )}

          </>
        )}
      </div>
    </div>
  );
}
