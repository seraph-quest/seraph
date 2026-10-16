import { useEffect, useRef, useState } from "react";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";

export interface BackgroundInferenceJob {
  job_id: string;
  label: string;
  enabled: boolean;
  scope: "job" | "llm_only";
  /** Null for event-driven jobs: the basis text is the estimate. */
  approx_calls_per_day: number | null;
  estimate_basis: string;
  last_skip: { status: string; reason: string; at: string } | null;
}

export interface BackgroundInferenceStatus {
  scheduler_enabled: boolean;
  error_code: string | null;
  jobs: BackgroundInferenceJob[];
}

const ENDPOINT = "/api/settings/background-inference";

/** Model callers deliberately outside these toggles, with where each is gated. */
export const EXCLUDED_MODEL_CALLERS: ReadonlyArray<{ label: string; reason: string }> = [
  { label: "Chat title generation", reason: "one text call per new conversation, triggered by chat rather than the scheduler" },
];

function recordOf(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

export function normalizeBackgroundInference(value: unknown): BackgroundInferenceStatus | null {
  const record = recordOf(value);
  if (!record || !Array.isArray(record.jobs)) return null;
  const jobs = record.jobs.flatMap((item): BackgroundInferenceJob[] => {
    const job = recordOf(item);
    if (!job || typeof job.job_id !== "string" || typeof job.enabled !== "boolean") return [];
    const skip = recordOf(job.last_skip);
    return [{
      job_id: job.job_id,
      label: typeof job.label === "string" ? job.label : job.job_id,
      enabled: job.enabled,
      scope: job.scope === "llm_only" ? "llm_only" : "job",
      approx_calls_per_day: typeof job.approx_calls_per_day === "number" && Number.isFinite(job.approx_calls_per_day) ? job.approx_calls_per_day : null,
      estimate_basis: typeof job.estimate_basis === "string" ? job.estimate_basis : "",
      last_skip: skip && typeof skip.reason === "string" && typeof skip.at === "string"
        ? { status: typeof skip.status === "string" ? skip.status : "skipped", reason: skip.reason, at: skip.at }
        : null,
    }];
  });
  return {
    scheduler_enabled: record.scheduler_enabled !== false,
    error_code: typeof record.error_code === "string" ? record.error_code : null,
    jobs,
  };
}

function callsLabel(value: number | null): string {
  if (value === null) return "per queued item";
  if (value > 0 && value < 1) return "~" + Math.round(value * 7) + "/week";
  return "~" + (Number.isInteger(value) ? value : value.toFixed(1)) + "/day";
}

/**
 * Per-job switches for scheduled jobs that may call the governed text route.
 * Jobs default ON; a switched-off job makes no model call and reports a
 * visible skip. Last-known toggles stay visible when metadata is unavailable.
 */
export function BackgroundInferencePanel() {
  const mounted = useRef(true);
  const [status, setStatus] = useState<BackgroundInferenceStatus | null>(null);
  const [stale, setStale] = useState(false);
  const [saving, setSaving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  // Unreadable stored toggles: the operator picks every job, then confirms.
  const [repairDraft, setRepairDraft] = useState<Record<string, boolean>>({});
  const [repairConfirmed, setRepairConfirmed] = useState(false);

  async function load() {
    try {
      const response = await apiFetch(API_URL + ENDPOINT, { signal: AbortSignal.timeout(5_000) });
      if (!response.ok) throw new Error("Background inference settings are unavailable (" + response.status + ").");
      const next = normalizeBackgroundInference(await response.json());
      if (!next) throw new Error("Background inference settings response is invalid.");
      if (!mounted.current) return;
      setStatus(next);
      setStale(false);
      setLoadError(null);
    } catch (failure) {
      if (!mounted.current) return;
      setStale(true);
      setLoadError(failure instanceof Error ? failure.message : "Background inference settings are unavailable.");
    }
  }

  useEffect(() => {
    mounted.current = true;
    void load();
    return () => { mounted.current = false; };
  }, []);

  async function save(savingKey: string, body: Record<string, unknown>) {
    if (saving) return;
    setSaving(savingKey);
    setError(null);
    try {
      const response = await apiFetch(API_URL + ENDPOINT, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(10_000),
      });
      if (response.status === 409) throw new Error("Stored toggles are unreadable; review every job below and confirm replacing them.");
      if (!response.ok) throw new Error("Toggle was not saved (" + response.status + "); refresh before retrying.");
      const next = normalizeBackgroundInference(await response.json());
      if (!next) throw new Error("Toggle saved; refresh to verify the current state.");
      if (!mounted.current) return;
      setStatus(next);
      setStale(false);
      setRepairDraft({});
      setRepairConfirmed(false);
    } catch (failure) {
      if (mounted.current) setError(failure instanceof Error ? failure.message : "Toggle was not saved.");
    } finally {
      if (mounted.current) setSaving(null);
    }
  }

  const unreadable = Boolean(status?.error_code);
  const repairChoice = (job: BackgroundInferenceJob) => repairDraft[job.job_id] ?? job.enabled;

  function toggle(job: BackgroundInferenceJob, enabled: boolean) {
    if (unreadable) {
      setRepairDraft((current) => ({ ...current, [job.job_id]: enabled }));
      setRepairConfirmed(false);
      return;
    }
    void save(job.job_id, { jobs: { [job.job_id]: enabled } });
  }

  function replaceUnreadable() {
    if (!status || !repairConfirmed) return;
    const jobs = Object.fromEntries(status.jobs.map((job) => [job.job_id, repairChoice(job)]));
    void save("replace", { jobs, replace_unreadable: true });
  }

  const enabledCalls = status?.jobs.filter((job) => job.enabled).reduce((total, job) => total + (job.approx_calls_per_day ?? 0), 0) ?? 0;
  const eventDriven = status?.jobs.some((job) => job.enabled && job.approx_calls_per_day === null) ?? false;

  return (
    <div className="mt-2 border border-retro-text/10 px-2 py-2 text-[9px]" data-testid="background-inference-panel">
      <div className="flex items-center justify-between gap-2 mb-1">
        <div className="text-[10px] text-retro-text">Background inference</div>
        <div className="text-retro-text/60">{status ? `up to ~${Math.round(enabledCalls)} scheduled calls/day enabled${eventDriven ? " plus per-item jobs" : ""}` : "loading"}{stale ? " · stale retained" : ""}</div>
      </div>
      <div className="mb-1 text-retro-text/50">
        Scheduled jobs that may call the OpenRouter text route once it is ready. Each call is billed.
        A switched-off job makes no model call and shows "skipped: disabled by operator". Estimates are upper bounds from the configured cadence.
      </div>
      {status && !status.scheduler_enabled && <div className="mb-1 text-retro-text/60">Scheduler is off for this deployment; no background job runs.</div>}
      {status?.error_code && <div role="status" className="mb-1 text-yellow-400">{status.error_code.replace(/_/g, " ")} · every scheduled model call is paused. Your earlier choices are unknown: set every job below, then confirm replacing the stored toggles.</div>}
      {status?.jobs.map((job) => (
        <label key={job.job_id} className="flex flex-wrap items-center gap-1 py-0.5 text-retro-text/70">
          <input
            type="checkbox"
            aria-label={"Background inference: " + job.label}
            checked={unreadable ? repairChoice(job) : job.enabled}
            disabled={saving !== null}
            onChange={(event) => toggle(job, event.target.checked)}
          />
          <span className="text-retro-text">{job.label}</span>
          <span className="text-retro-text/50">{callsLabel(job.approx_calls_per_day)} · {job.estimate_basis}</span>
          {!unreadable && !job.enabled && <span className="text-yellow-400">{job.last_skip ? `skipped: ${job.last_skip.reason.replace(/_/g, " ")} · ${job.last_skip.at}` : "off: skipped: disabled by operator"}</span>}
        </label>
      ))}
      {unreadable && status && (
        <div className="mt-1 flex flex-wrap items-center gap-2 border border-yellow-400/30 px-1 py-1">
          <label className="flex items-center gap-1 text-retro-text/70">
            <input type="checkbox" aria-label="Confirm replacing unreadable background toggles" checked={repairConfirmed} onChange={(event) => setRepairConfirmed(event.target.checked)} />
            Replace the unreadable toggles with exactly the {status.jobs.filter(repairChoice).length} job(s) checked above
          </label>
          <button type="button" disabled={!repairConfirmed || saving !== null} onClick={replaceUnreadable} className="border border-retro-text/20 px-2 py-0.5 text-retro-text/70 disabled:opacity-40">
            {saving === "replace" ? "Saving" : "Replace stored toggles"}
          </button>
        </div>
      )}
      <div className="mt-1 text-retro-text/50">
        Not covered by these toggles:{" "}
        {EXCLUDED_MODEL_CALLERS.map((item) => item.label + " (" + item.reason + ")").join("; ")}.
      </div>
      {loadError && (
        <div className="mt-1 text-yellow-400">
          {loadError}{status ? " Showing last-known toggles." : ""}{" "}
          <button type="button" className="underline" onClick={() => void load()}>Retry</button>
        </div>
      )}
      {error && <div role="alert" className="mt-1 text-red-400">{error}</div>}
    </div>
  );
}
