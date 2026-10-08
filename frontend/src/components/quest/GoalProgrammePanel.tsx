import { useEffect, useRef, useState } from "react";
import type { GoalInfo } from "../../types";
import { isGoalProgramme, isDiscoveryRun, ProgrammeError, programmeApi } from "./goalProgrammeApi";
import type { GoalProgramme, ProgrammePreview, ProgrammeRequest, DiscoveryRun } from "./goalProgrammeApi";

export function GoalProgrammePanel({ goal, goalDraftChanged = false }: { goal: GoalInfo; goalDraftChanged?: boolean }) {
  const [brief, setBrief] = useState("");
  const [days, setDays] = useState("7");
  const [ceiling, setCeiling] = useState("0");
  const [notifications, setNotifications] = useState("0");
  const [programmes, setProgrammes] = useState<GoalProgramme[]>([]);
  const [grantRevision, setGrantRevision] = useState<number | null>(null);
  const [review, setReview] = useState<{ preview: ProgrammePreview; request: ProgrammeRequest } | null>(null);
  const [acks, setAcks] = useState([false, false, false]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [recovery, setRecovery] = useState<{ programme: GoalProgramme; action: "pause" | "revoke" } | null>(null);
  const [recoverAck, setRecoverAck] = useState(false);
  const [runs, setRuns] = useState<DiscoveryRun[]>([]);
  const [discoveryBrief, setDiscoveryBrief] = useState<string | null>(null);
  const mounted = useRef(false);
  const currentDraft = useRef("");
  const signature = JSON.stringify([brief, days, ceiling, notifications, goal.revision, goalDraftChanged]);
  currentDraft.current = signature;
  const load = async (signal?: AbortSignal) => {
    const result = await programmeApi<{ goal_id: string; grant_revision: number; programmes: GoalProgramme[] }>(goal.id, "", undefined, signal);
    if (!mounted.current || signal?.aborted) return;
    if (result.goal_id !== goal.id || !Number.isSafeInteger(result.grant_revision) || result.grant_revision < 0
      || !Array.isArray(result.programmes) || !result.programmes.every((item) => isGoalProgramme(item) && item.goal_id === goal.id)) {
      throw new Error("Programme metadata incomplete. Reload before reviewing authority.");
    }
    setProgrammes(result.programmes); setGrantRevision(result.grant_revision);
  };
  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    void load(controller.signal).catch(() => { if (!controller.signal.aborted) setError("Programme metadata unavailable. Reload to review current authority."); });
    return () => { mounted.current = false; controller.abort(); };
  // Each mount is keyed to the saved Goal identity by GoalForm.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [goal.id]);
  useEffect(() => { setReview(null); setAcks([false, false, false]); }, [signature]);
  const canConfigure = !goalDraftChanged && Number.isInteger(goal.revision) && (goal.revision ?? 0) > 0 && grantRevision !== null && !busy;
  const preview = async () => {
    const duration = Number(days), amount = Number(ceiling), cap = Number(notifications);
    if (!brief.trim() || brief.trim().length > 2000 || !Number.isInteger(duration) || duration < 1 || duration > 7
      || !Number.isSafeInteger(amount) || amount < 0 || !Number.isInteger(cap) || cap < 0 || cap > 2) {
      setError("Supply a public brief, 1–7 days, a finite nonnegative integer inference ceiling, and 0–2 notifications per day."); return;
    }
    const request: ProgrammeRequest = { expected_goal_revision: goal.revision!, expected_grant_revision: grantRevision!,
      public_brief: brief.trim(), duration_days: duration, budget: { max_inference_microusd: amount, max_outstanding_runs: 1 },
      cadence: "daily", notification_limits: { per_day: cap } };
    const submitted = signature;
    setBusy(true); setError(""); setReview(null); setAcks([false, false, false]);
    try {
      const result = await programmeApi<ProgrammePreview>(goal.id, "/preview", request);
      if (!mounted.current || currentDraft.current !== submitted) return;
      if (result.public_only !== true || result.preview_only !== true || !isGoalProgramme(result.programme) || result.programme.goal_id !== goal.id
        || result.programme.goal_revision !== goal.revision || result.programme.public_brief !== request.public_brief
        || result.programme.grant_revision !== request.expected_grant_revision + 1
        || result.programme.budget.max_inference_microusd !== request.budget.max_inference_microusd
        || result.programme.notification_limits.per_day !== request.notification_limits.per_day
        || Date.parse(result.programme.expires_at) - Date.parse(result.programme.confirmed_at) !== request.duration_days * 86400000
        || result.review_digest !== result.programme.review_digest) throw new Error("Programme review binding mismatch. Reload and review again.");
      setReview({ preview: result, request }); await load();
    } catch (err) { if (mounted.current) setError(err instanceof Error ? err.message : "Programme preview failed."); }
    finally { if (mounted.current) setBusy(false); }
  };
  const accept = async () => {
    if (!review || !acks.every(Boolean) || !canConfigure) return;
    setBusy(true); setError("");
    try {
      await programmeApi(goal.id, "/accept", { ...review.request, review_digest: review.preview.review_digest,
        public_web_acknowledged: true, local_artifacts_acknowledged: true, inference_ceiling_acknowledged: true });
      if (mounted.current) { setReview(null); setAcks([false, false, false]); await load(); }
    } catch (err) { if (mounted.current) { setReview(null); setAcks([false, false, false]); setError(err instanceof Error ? err.message : "Programme acceptance failed."); } }
    finally { if (mounted.current) setBusy(false); }
  };
  const control = async (programme: GoalProgramme, action: "pause" | "revoke", recovering = false) => {
    setBusy(true); setError(""); setReview(null); setAcks([false, false, false]);
    try {
      await programmeApi(goal.id, `/${encodeURIComponent(programme.id)}/${action}`, {
        expected_grant_revision: programme.grant_revision, recover_owner_acknowledged: recovering });
      if (mounted.current) { setRecovery(null); setRecoverAck(false); await load(); }
    } catch (err) {
      if (!mounted.current) return;
      if (err instanceof ProgrammeError && err.code === "programme_owner_recovery_required" && !recovering) {
        setRecovery({ programme, action }); setRecoverAck(false);
      } else { setError(err instanceof Error ? err.message : "Programme control failed."); setRecovery(null); setRecoverAck(false); }
    } finally { if (mounted.current) setBusy(false); }
  };
  const inspectDiscovery = async () => {
    setBusy(true); setError(""); setDiscoveryBrief(null);
    try {
      const result = await programmeApi<{ goal_id: string; runs: unknown[]; no_learning: true; current_day_only: true }>(goal.id, "/discovery");
      if (result.goal_id !== goal.id || result.no_learning !== true || result.current_day_only !== true
        || !Array.isArray(result.runs) || !result.runs.every(isDiscoveryRun)) throw new Error("Discovery history incomplete. Review retained receipts before new work.");
      if (mounted.current) setRuns(result.runs);
    } catch (err) { if (mounted.current) setError(err instanceof Error ? err.message : "Discovery history unavailable."); }
    finally { if (mounted.current) setBusy(false); }
  };
  const readDiscovery = async (run: DiscoveryRun) => {
    setBusy(true); setError(""); setDiscoveryBrief(null);
    try {
      const result = await programmeApi<{ job_id: string; programme_id: string; physical_readback: boolean; no_learning: boolean; brief: unknown }>(goal.id,
        `/${encodeURIComponent(run.programme_id)}/discovery/${encodeURIComponent(run.job_id)}/brief`);
      if (result.job_id !== run.job_id || result.programme_id !== run.programme_id || result.physical_readback !== true
        || result.no_learning !== true || !result.brief || typeof result.brief !== "object") throw new Error("Selected discovery brief lacks current physical readback.");
      if (mounted.current) setDiscoveryBrief(JSON.stringify(result.brief, null, 2));
    } catch (err) { if (mounted.current) setError(err instanceof Error ? err.message : "Discovery brief readback denied."); }
    finally { if (mounted.current) setBusy(false); }
  };
  return <section aria-label="Public goal programme" className="space-y-3 border border-slate-700 rounded p-3 text-xs">
    <div className="cockpit-card-title">Public goal programme</div>
    <p>Enter a separate public brief locally. Your private priority title and description are never copied into this review. No query, URL or output path is required.</p>
    <p>Daily cadence · one outstanding run · maximum seven days. Logout and a new login do not renew the grant. Changing the public brief and clicking Preview immediately pauses previous programmes. Abandoning this review will not resume them. An unchanged-brief renewal pauses its predecessor on acceptance.</p>
    <p>The reviewed public brief can produce one bounded daily discovery brief. The inference ceiling covers the whole generation. A zero ceiling, unavailable governed route or unresolved previous occurrence blocks new work. No learning or external mutation is performed.</p>
    {goalDraftChanged && <p role="status">Save and reopen your changed priority before reviewing a programme for its current revision.</p>}
    <label className="block">Public brief<textarea aria-label="Public brief" value={brief} maxLength={2000} onChange={(e) => setBrief(e.target.value)} className="cockpit-input w-full" /></label>
    <label className="block">Programme duration (days)<input aria-label="Programme duration (days)" type="number" min="1" max="7" value={days} onChange={(e) => setDays(e.target.value)} className="cockpit-input" /></label>
    <label className="block">Inference ceiling (micro USD)<input aria-label="Inference ceiling (micro USD)" type="number" min="0" step="1" value={ceiling} onChange={(e) => setCeiling(e.target.value)} className="cockpit-input" /></label>
    <label className="block">Programme notifications per day<input aria-label="Programme notifications per day" type="number" min="0" max="2" value={notifications} onChange={(e) => setNotifications(e.target.value)} className="cockpit-input" /></label>
    <button type="button" disabled={!canConfigure} onClick={() => void preview()} className="cockpit-action">Preview finite public programme</button>
    {review && <div aria-label="Exact programme review" className="space-y-2">
      {review.preview.paused_programme_ids?.length > 0 && <div role="status">Changed-brief preview paused these previous programmes: {review.preview.paused_programme_ids.join(", ")}. Abandoning review will not resume them.</div>}
      <div>Exact public brief: {review.preview.programme.public_brief}</div>
      <div>Public-web search/read · local artifact prefix: {review.preview.programme.artifact_prefix}</div>
      <div>Inference ceiling: {review.preview.programme.budget.max_inference_microusd} micro USD · daily · one outstanding run</div>
      <div>Goal revision {review.preview.programme.goal_revision} · grant revision {review.preview.programme.grant_revision} · expires {review.preview.programme.expires_at}</div>
      <div>Capabilities: {review.preview.programme.capability_ids.join(", ")} · route epoch {review.preview.programme.route_epoch}</div>
      <div>Preview state: {review.preview.programme.state} {review.preview.programme.reason_code} {review.preview.programme.recovery}</div>
      {["I reviewed this exact public brief and public-web search/read", "I reviewed this exact local artifact prefix", "I reviewed this finite inference ceiling and expiry"].map((label, index) =>
        <label key={label} className="block"><input type="checkbox" checked={acks[index]} onChange={(e) => setAcks((old) => old.map((value, i) => i === index ? e.target.checked : value))} /> {label}</label>)}
      <button type="button" disabled={!canConfigure || !acks.every(Boolean)} onClick={() => void accept()} className="cockpit-action">Accept reviewed finite programme</button>
    </div>}
    {programmes.map((programme) => <article key={programme.id} aria-label={`Programme ${programme.id}`}>
      <div>{programme.state} · goal revision {programme.goal_revision} · grant revision {programme.grant_revision} · expires {programme.expires_at}</div>
      <div>{programme.public_brief}</div><div>{programme.reason_code} {programme.recovery}</div>
      <div>History and existing liabilities remain retained.</div>
      <button type="button" disabled={busy || programme.state === "revoked" || programme.state === "paused"} onClick={() => void control(programme, "pause")} className="cockpit-action">Pause programme {programme.grant_revision}</button>
      <button type="button" disabled={busy || programme.state === "revoked"} onClick={() => void control(programme, "revoke")} className="cockpit-action">Revoke programme {programme.grant_revision}</button>
    </article>)}
    <button type="button" disabled={busy} onClick={() => void inspectDiscovery()} className="cockpit-action">Inspect discovery runs</button>
    {runs.map((run) => <article key={run.job_id} aria-label={`Discovery ${run.job_id}`}>
      <div>{run.occurrence_day} · {run.status} · grant revision {run.grant_revision} · original deadline {run.deadline_at}</div>
      <div>External effect: {run.external_effect_state} · {run.outstanding_held ? "Outstanding occurrence held" : "Occurrence closed"} · no_learning</div>
      {run.accounting_liability && <div>Original accounting liability is unresolved.</div>}
      {run.denial_cause && <div>Untouched occurrence cancelled: {run.denial_cause}. No contact or replay was admitted.</div>}
      {run.search_blocked_reason && <div role="status">Public search blocked: {run.search_blocked_reason}. Inspect the original occurrence; provider replay is forbidden.</div>}
      {run.outcome && <div>{run.outcome.state} · coverage {run.outcome.coverage} · freshness {run.outcome.freshness}</div>}
      {run.recovery && <p>{run.recovery}</p>}
      <button type="button" disabled={busy || !run.outcome || !["succeeded", "degraded"].includes(run.status) || run.external_effect_state === "unknown"}
        onClick={() => void readDiscovery(run)} className="cockpit-action">Read selected discovery brief {run.occurrence_day}</button>
    </article>)}
    {discoveryBrief && <pre aria-label="Discovery brief physical readback" className="whitespace-pre-wrap">{discoveryBrief}</pre>}
    {recovery && <div role="status">
      <p>A fresh login needs explicit owner recovery to {recovery.action} programme {recovery.programme.id}, grant revision {recovery.programme.grant_revision}. This only controls that old programme and never accepts or renews authority.</p>
      <label><input type="checkbox" checked={recoverAck} onChange={(e) => setRecoverAck(e.target.checked)} /> I acknowledge recovery for this exact old programme control</label>
      <button type="button" disabled={busy || !recoverAck} onClick={() => void control(recovery.programme, recovery.action, true)} className="cockpit-action">Confirm recovered {recovery.action}</button>
      <button type="button" disabled={busy} onClick={() => { setRecovery(null); setRecoverAck(false); }}>Cancel recovery</button>
    </div>}
    {error && <div role="alert">{error}</div>}
    <button type="button" disabled={busy} onClick={() => { setBusy(true); void load().then(() => setError("")).catch(() => setError("Programme metadata unavailable. Retained history remains visible.")).finally(() => { if (mounted.current) setBusy(false); }); }}>Reload programmes</button>
  </section>;
}
