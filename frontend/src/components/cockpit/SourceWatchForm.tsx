import { useCallback, useEffect, useState, type ReactNode } from "react";

import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";

export interface SourceWatchFormGoal {
  id: string;
  title: string;
  revision?: number | null;
}

interface SourceWatchRecord {
  id: string;
  goal_id: string;
  goal_revision: number;
  plan_revision: number;
  state: string;
  write_mode: string;
  sources?: Array<{ source_key?: string; kind?: string; target?: string; priority?: number }>;
  last_status?: string | null;
  last_error_code?: string | null;
  baselines?: Array<{ source_key?: string; state?: string; sha256?: string; generation?: number }>;
  latest_packet?: {
    id?: string;
    status?: string;
    verification_status?: string | null;
    memory_status?: string | null;
    dossier_path?: string | null;
    task_path?: string | null;
    failure_code?: string | null;
  } | null;
}

export interface SourceWatchFormProps {
  goal: SourceWatchFormGoal | null;
  autoLoad?: boolean;
}

function detailFromPayload(payload: unknown): string {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return "request failed";
  const detail = (payload as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    const code = (detail as { code?: unknown }).code;
    if (typeof code === "string" && code) return code;
  }
  return "request failed";
}

export function SourceWatchForm({ goal, autoLoad = true }: SourceWatchFormProps) {
  const [watches, setWatches] = useState<SourceWatchRecord[]>([]);
  const [source, setSource] = useState("");
  const [includeTerms, setIncludeTerms] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const loadWatches = useCallback(async () => {
    try {
      const response = await apiFetch(`${API_URL}/api/capabilities/source-watches`);
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        setStatus(`Watch status unavailable: ${detailFromPayload(payload)}`);
        return;
      }
      setWatches(Array.isArray(payload) ? payload as SourceWatchRecord[] : []);
    } catch {
      setStatus("Watch status unavailable");
    }
  }, []);

  useEffect(() => {
    if (autoLoad) void loadWatches();
  }, [autoLoad, loadWatches]);

  const createWatch = async () => {
    if (!goal?.id || !goal.revision || !source.trim()) {
      setStatus("An active goal revision and HTTPS or workspace source are required.");
      return;
    }
    setBusy(true);
    try {
      const response = await apiFetch(`${API_URL}/api/capabilities/source-watches`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          goal_id: goal.id,
          expected_goal_revision: goal.revision,
          sources: [{
            source_key: "primary",
            kind: source.trim().startsWith("https://") ? "public_https_text" : "workspace_text",
            target: source.trim(),
            priority: 3,
          }],
          criteria: {
            include_terms: includeTerms.split(",").map((term) => term.trim()).filter(Boolean),
            exclude_terms: [],
            min_changed_lines: 1,
            min_changed_chars: 1,
            max_material_sources: 3,
          },
          schedule: { cron: "*/15 * * * *", timezone: "UTC" },
          write_mode: "approval_each_run",
        }),
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        setStatus(`Watch blocked: ${detailFromPayload(payload)}`);
        return;
      }
      setSource("");
      setIncludeTerms("");
      setStatus("Watch created. Observation is bounded and local writes remain approval-bound.");
      await loadWatches();
    } catch {
      setStatus("Watch creation failed");
    } finally {
      setBusy(false);
    }
  };

  const runWatch = async (watch: SourceWatchRecord) => {
    setBusy(true);
    try {
      const response = await apiFetch(`${API_URL}/api/capabilities/source-watches/${encodeURIComponent(watch.id)}/run`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ expected_plan_revision: watch.plan_revision }),
      });
      const payload = await response.json().catch(() => null);
      setStatus(response.ok ? `Watch run: ${String((payload as { status?: unknown })?.status ?? "recorded")}` : `Run blocked: ${detailFromPayload(payload)}`);
      await loadWatches();
    } catch {
      setStatus("Watch run unavailable");
    } finally {
      setBusy(false);
    }
  };

  return (
    <CardShell title="Guardian source watch" testId="source-watch-form">
      <div className="cockpit-outcome-copy">
        {goal ? `Bound to ${goal.title} · goal revision ${goal.revision ?? "unknown"}` : "Select an active goal to configure a bounded watch."}
      </div>
      <div className="source-watch-form-grid">
        <input
          aria-label="Guardian source"
          value={source}
          onChange={(event) => setSource(event.target.value)}
          placeholder="https://example.org/updates.txt or notes/plan.md"
          disabled={!goal || busy}
        />
        <input
          aria-label="Guardian include terms"
          value={includeTerms}
          onChange={(event) => setIncludeTerms(event.target.value)}
          placeholder="include terms, comma separated"
          disabled={!goal || busy}
        />
        <button type="button" onClick={() => void createWatch()} disabled={!goal || busy}>
          {busy ? "Working…" : "Add watch"}
        </button>
        <button type="button" onClick={() => void loadWatches()} disabled={busy}>
          Refresh watches
        </button>
      </div>
      {watches.filter((watch) => !goal || watch.goal_id === goal.id).map((watch) => (
        <div className="source-watch-record" key={watch.id} data-testid={`source-watch-${watch.id}`}>
          <div className="cockpit-outcome-primary">
            {watch.state} · goal {watch.goal_revision}/{watch.plan_revision}
          </div>
          <div className="cockpit-outcome-copy">
            {watch.sources?.length ?? 0} source(s) · {watch.write_mode} · {watch.last_status ?? "no run yet"}
          </div>
          <div className="cockpit-outcome-copy">
            baselines {watch.baselines?.filter((item) => item.state === "ready").length ?? 0}/{watch.sources?.length ?? 0}
            {watch.latest_packet?.status ? ` · packet ${watch.latest_packet.status}` : ""}
            {watch.latest_packet?.verification_status ? ` · ${watch.latest_packet.verification_status}` : ""}
            {watch.latest_packet?.memory_status ? ` · ${watch.latest_packet.memory_status}` : ""}
          </div>
          {watch.last_error_code ? <div className="cockpit-outcome-note">blocked · {watch.last_error_code}</div> : null}
          {watch.latest_packet?.dossier_path ? <div className="cockpit-outcome-note">dossier · {watch.latest_packet.dossier_path}</div> : null}
          {watch.latest_packet?.task_path ? <div className="cockpit-outcome-note">task · {watch.latest_packet.task_path}</div> : null}
          <button type="button" onClick={() => void runWatch(watch)} disabled={busy || watch.state !== "active"}>
            Run bounded observation
          </button>
        </div>
      ))}
      {status ? <div className="cockpit-outcome-note" role="status">{status}</div> : null}
    </CardShell>
  );
}

function CardShell({ title, testId, children }: { title: string; testId: string; children: ReactNode }) {
  return (
    <section className="cockpit-outcome-card" data-testid={testId}>
      <div className="cockpit-outcome-card-header">
        <div className="cockpit-outcome-card-label">{title}</div>
      </div>
      <div className="cockpit-outcome-card-body">{children}</div>
    </section>
  );
}
