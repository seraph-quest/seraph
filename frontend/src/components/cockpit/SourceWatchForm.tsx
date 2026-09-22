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
  owner_session_id?: string | null;
  goal_revision: number;
  plan_revision: number;
  state: string;
  write_mode: string;
  active_job_id?: string | null;
  active_job_fence?: number | null;
  sources?: Array<{ source_key?: string; kind?: string; target?: string; priority?: number }>;
  criteria?: { include_terms?: string[]; exclude_terms?: string[] };
  last_status?: string | null;
  last_error_code?: string | null;
  baselines?: Array<{ source_key?: string; state?: string; sha256?: string; generation?: number }>;
  latest_packet?: {
    id?: string;
    run_identity?: string | null;
    packet_digest?: string | null;
    status?: string;
    approval_id?: string | null;
    approval_revision?: number | null;
    verification_status?: string | null;
    memory_status?: string | null;
    dossier_path?: string | null;
    dossier_artifact_id?: string | null;
    dossier_sha256?: string | null;
    task_path?: string | null;
    task_artifact_id?: string | null;
    task_sha256?: string | null;
    failure_code?: string | null;
    strategy_delta_id?: string | null;
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
  const [correctionIncludeTerms, setCorrectionIncludeTerms] = useState("");
  const [correctionExcludeTerms, setCorrectionExcludeTerms] = useState("");
  const [correctionReason, setCorrectionReason] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionBusy, setActionBusy] = useState<string | null>(null);

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

  const packetFor = (watch: SourceWatchRecord) => watch.latest_packet;
  const jobIdFor = (watch: SourceWatchRecord) => {
    const packet = packetFor(watch);
    if (watch.active_job_id) return watch.active_job_id;
    return packet && ["blocked", "failed", "degraded", "awaiting_approval", "executing"].includes(packet.status ?? "")
      ? packet.run_identity ?? null
      : null;
  };

  const actionRequest = async (
    actionKey: string,
    label: string,
    path: string,
    body: Record<string, unknown>,
  ) => {
    setActionBusy(actionKey);
    try {
      const response = await apiFetch(`${API_URL}${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        setStatus(`${label} blocked: ${detailFromPayload(payload)}`);
        return false;
      }
      const resultStatus = typeof (payload as { status?: unknown } | null)?.status === "string"
        ? (payload as { status: string }).status
        : "recorded";
      setStatus(`${label}: ${resultStatus}`);
      await loadWatches();
      return true;
    } catch {
      setStatus(`${label} unavailable`);
      return false;
    } finally {
      setActionBusy(null);
    }
  };

  const executeApprovedPacket = async (watch: SourceWatchRecord) => {
    const packet = packetFor(watch);
    if (!packet?.id || !packet.approval_id || !packet.packet_digest) {
      setStatus("Packet execution blocked: approval and packet digest readback are required.");
      return;
    }
    const actionKey = `execute:${watch.id}`;
    setActionBusy(actionKey);
    try {
      const approvalResponse = await apiFetch(`${API_URL}/api/approvals/${encodeURIComponent(packet.approval_id)}/approve`, {
        method: "POST",
      });
      const approvalPayload = await approvalResponse.json().catch(() => null);
      if (!approvalResponse.ok && approvalPayload?.detail?.code !== "approval_already_resolved") {
        setStatus(`Approval blocked: ${detailFromPayload(approvalPayload)}`);
        return;
      }
      const executeResponse = await apiFetch(`${API_URL}/api/capabilities/source-watches/${encodeURIComponent(watch.id)}/packets/${encodeURIComponent(packet.id)}/execute`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          expected_packet_digest: packet.packet_digest,
          approval_id: packet.approval_id,
          ...(typeof packet.approval_revision === "number"
            ? { expected_approval_revision: packet.approval_revision }
            : {}),
        }),
      });
      const executePayload = await executeResponse.json().catch(() => null);
      setStatus(executeResponse.ok
        ? `Packet execution: ${String((executePayload as { status?: unknown })?.status ?? "recorded")}`
        : `Execution blocked: ${detailFromPayload(executePayload)}`);
      await loadWatches();
    } catch {
      setStatus("Packet execution unavailable");
    } finally {
      setActionBusy(null);
    }
  };

  const cancelWatchJob = async (watch: SourceWatchRecord) => {
    const jobId = jobIdFor(watch);
    const fencingToken = watch.active_job_fence;
    if (!watch.active_job_id || !jobId || typeof fencingToken !== "number" || fencingToken < 1) {
      setStatus("Cancellation blocked: the current job fence is unavailable; refresh before retrying.");
      return;
    }
    await actionRequest(
      `cancel:${watch.id}`,
      "Cancellation",
      `/api/capabilities/source-watches/${encodeURIComponent(watch.id)}/cancel`,
      {
        job_id: jobId,
        expected_plan_revision: watch.plan_revision,
        expected_fencing_token: fencingToken,
      },
    );
  };

  const recoverWatchJob = async (watch: SourceWatchRecord) => {
    const jobId = jobIdFor(watch);
    if (!jobId) {
      setStatus("Recovery blocked: no durable occurrence is available for this watch.");
      return;
    }
    await actionRequest(
      `recover:${watch.id}`,
      "Recovery",
      `/api/capabilities/source-watches/${encodeURIComponent(watch.id)}/recover`,
      { job_id: jobId, expected_plan_revision: watch.plan_revision },
    );
  };

  const applyCorrection = async (watch: SourceWatchRecord) => {
    const reason = correctionReason.trim();
    if (!reason) {
      setStatus("Correction blocked: explain the bounded criteria change first.");
      return;
    }
    await actionRequest(
      `correct:${watch.id}`,
      "Correction",
      `/api/capabilities/source-watches/${encodeURIComponent(watch.id)}/corrections`,
      {
        expected_goal_revision: watch.goal_revision,
        expected_plan_revision: watch.plan_revision,
        include_terms: correctionIncludeTerms.split(",").map((term) => term.trim()).filter(Boolean),
        exclude_terms: correctionExcludeTerms.split(",").map((term) => term.trim()).filter(Boolean),
        reason,
      },
    );
    setCorrectionReason("");
  };

  const undoCorrection = async (watch: SourceWatchRecord) => {
    const deltaId = watch.latest_packet?.strategy_delta_id;
    if (!deltaId) {
      setStatus("Undo blocked: no reversible correction receipt is available.");
      return;
    }
    await actionRequest(
      `undo:${watch.id}`,
      "Correction undo",
      `/api/capabilities/source-watches/${encodeURIComponent(watch.id)}/corrections/undo`,
      {
        expected_goal_revision: watch.goal_revision,
        expected_plan_revision: watch.plan_revision,
        prior_strategy_delta_id: deltaId,
        reason: "Operator restored the prior source-watch criteria.",
      },
    );
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
      {watches.filter((watch) => !goal || watch.goal_id === goal.id).map((watch) => {
        const packet = packetFor(watch);
        const jobId = jobIdFor(watch);
        const state = watch.last_error_code || packet?.failure_code || watch.state === "blocked"
          ? "blocked"
          : packet?.status === "awaiting_approval"
            ? "awaiting_approval"
            : packet?.status === "degraded" || watch.last_status === "degraded"
              ? "degraded"
              : packet?.status === "succeeded" && packet.verification_status === "passed"
                ? "recovered"
                : watch.state;
        const canCorrect = Boolean(
          packet?.id
          && ["succeeded", "degraded"].includes(packet.status ?? "")
          && packet.verification_status === "passed"
          && !watch.active_job_id,
        );
        const actionKeyPrefix = `${watch.id}`;
        return (
          <div className="source-watch-record" key={watch.id} data-testid={`source-watch-${watch.id}`} data-operator-state={state}>
            <div className="cockpit-outcome-primary">
              {state} · goal {watch.goal_revision}/{watch.plan_revision}
            </div>
            <div className="cockpit-outcome-copy">
              {watch.sources?.length ?? 0} source(s) · {watch.write_mode} · {watch.last_status ?? "no run yet"}
            </div>
            <div className="cockpit-outcome-copy">
              baselines {watch.baselines?.filter((item) => item.state === "ready").length ?? 0}/{watch.sources?.length ?? 0}
              {packet?.status ? ` · packet ${packet.status}` : ""}
              {packet?.verification_status ? ` · readback ${packet.verification_status}` : ""}
              {packet?.memory_status ? ` · memory ${packet.memory_status}` : ""}
            </div>
            {watch.last_error_code ? <div className="cockpit-outcome-note">blocked · {watch.last_error_code}</div> : null}
            {packet?.failure_code ? <div className="cockpit-outcome-note">recovery required · {packet.failure_code}</div> : null}
            {watch.baselines?.map((baseline) => (
              <div className="cockpit-outcome-note" key={`${watch.id}:${baseline.source_key}`}>
                baseline {baseline.source_key ?? "source"} · {baseline.state ?? "unknown"} · sha256 {baseline.sha256 ?? "unavailable"}
              </div>
            ))}
            {packet?.dossier_path ? <div className="cockpit-outcome-note">dossier · {packet.dossier_path} · sha256 {packet.dossier_sha256 ?? "unavailable"}</div> : null}
            {packet?.dossier_artifact_id ? <div className="cockpit-outcome-note">dossier artifact · {packet.dossier_artifact_id}</div> : null}
            {packet?.task_path ? <div className="cockpit-outcome-note">task · {packet.task_path} · sha256 {packet.task_sha256 ?? "unavailable"}</div> : null}
            {packet?.task_artifact_id ? <div className="cockpit-outcome-note">task artifact · {packet.task_artifact_id}</div> : null}
            {packet?.approval_id ? <div className="cockpit-outcome-note">approval · {packet.approval_id} · packet digest {packet.packet_digest ?? "unavailable"}</div> : null}
            {jobId ? <div className="cockpit-outcome-note">occurrence · {jobId} · fence {watch.active_job_fence ?? "unavailable"}</div> : null}
            <div className="source-watch-actions">
              <button type="button" onClick={() => void runWatch(watch)} disabled={busy || actionBusy !== null || watch.state !== "active"}>
                Run bounded observation
              </button>
              {packet?.status === "awaiting_approval" ? (
                <button
                  type="button"
                  onClick={() => void executeApprovedPacket(watch)}
                  disabled={busy || actionBusy !== null || !packet.approval_id || !packet.packet_digest}
                >
                  {actionBusy === `execute:${watch.id}` ? "Approving…" : "Approve and execute packet"}
                </button>
              ) : null}
              {jobId ? (
                <>
                  <button
                    type="button"
                    onClick={() => void cancelWatchJob(watch)}
                    disabled={busy || actionBusy !== null || typeof watch.active_job_fence !== "number" || watch.active_job_fence < 1}
                  >
                    {actionBusy === `cancel:${actionKeyPrefix}` ? "Cancelling…" : "Cancel occurrence"}
                  </button>
                  <button type="button" onClick={() => void recoverWatchJob(watch)} disabled={busy || actionBusy !== null}>
                    {actionBusy === `recover:${actionKeyPrefix}` ? "Recovering…" : "Recover occurrence"}
                  </button>
                </>
              ) : null}
            </div>
            {canCorrect ? (
              <div className="source-watch-correction">
                <div className="cockpit-outcome-copy">Verified packet correction</div>
                <input
                  aria-label={`Correction include terms for ${watch.id}`}
                  value={correctionIncludeTerms}
                  onChange={(event) => setCorrectionIncludeTerms(event.target.value)}
                  placeholder="include terms, comma separated"
                  disabled={actionBusy !== null}
                />
                <input
                  aria-label={`Correction exclude terms for ${watch.id}`}
                  value={correctionExcludeTerms}
                  onChange={(event) => setCorrectionExcludeTerms(event.target.value)}
                  placeholder="exclude terms, comma separated"
                  disabled={actionBusy !== null}
                />
                <input
                  aria-label={`Correction reason for ${watch.id}`}
                  value={correctionReason}
                  onChange={(event) => setCorrectionReason(event.target.value)}
                  placeholder="why change the next cycle?"
                  disabled={actionBusy !== null}
                />
                <button type="button" onClick={() => void applyCorrection(watch)} disabled={busy || actionBusy !== null}>
                  {actionBusy === `correct:${watch.id}` ? "Applying…" : "Apply correction"}
                </button>
                {packet?.strategy_delta_id ? (
                  <button type="button" onClick={() => void undoCorrection(watch)} disabled={busy || actionBusy !== null}>
                    {actionBusy === `undo:${watch.id}` ? "Undoing…" : "Undo last correction"}
                  </button>
                ) : null}
              </div>
            ) : null}
          </div>
        );
      })}
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
