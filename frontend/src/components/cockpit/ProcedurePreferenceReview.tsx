import { useEffect, useMemo, useRef, useState } from "react";
import { procedurePreferences, type ProcedureOutcome, type ProcedureOutcomeList, type ProcedurePreferenceAction,
  type ProcedurePreferenceReview as Review, type ProcedurePreferenceScope, type ProcedurePreferenceSelection,
  type ProcedureRecommendation, type RecommendationRequest, type RecommendationCancelRequest } from "../../lib/procedurePreferences";

interface Props {
  ownerPrincipalId: string;
  ownerSessionId: string;
  scope: ProcedurePreferenceScope;
  onSelectVersion?: (version: number) => void;
}
type Pending =
  | { kind: "recommend"; body: RecommendationRequest }
  | { kind: "cancel"; jobId: string; body: RecommendationCancelRequest }
  | { kind: "action"; proposalId: string; body: ProcedurePreferenceAction }
  | { kind: "feedback"; outcome: ProcedureOutcome; label: "helpful" | "harmful"; reason: string; mutationUuid: string };
interface Stored { pending: Pending | null; jobId: string | null; proposalId: string | null }
interface State extends Stored { key: string; list: ProcedureOutcomeList | null; recommendation: ProcedureRecommendation | null; review: Review | null; selection: ProcedurePreferenceSelection | null }

function readStored(key: string): Stored {
  const raw = sessionStorage.getItem(key);
  if (!raw) return { pending: null, jobId: null, proposalId: null };
  if (raw.length > 8192) throw new Error("The retained review request exceeds its finite bound.");
  const value = JSON.parse(raw) as Stored;
  if (!value || typeof value !== "object" || (value.pending !== null && !["recommend", "cancel", "action", "feedback"].includes(value.pending?.kind))
    || (value.jobId !== null && (typeof value.jobId !== "string" || value.jobId.length > 256))
    || (value.proposalId !== null && (typeof value.proposalId !== "string" || value.proposalId.length > 256))) {
    throw new Error("The retained review request is invalid; no action was sent.");
  }
  return value;
}
function saveStored(key: string, value: Stored) {
  const raw = JSON.stringify({ pending: value.pending, jobId: value.jobId, proposalId: value.proposalId });
  if (raw.length > 8192) throw new Error("The review request exceeds its finite retention bound.");
  sessionStorage.setItem(key, raw);
}

function feedbackHistory(outcomes: ProcedureOutcome[]) {
  if (outcomes.some((item) => item.feedback_history_count === undefined)) return "Feedback history/correction counts were not recorded in this receipt.";
  return `Feedback history: ${outcomes.reduce((count, item) => count + item.feedback_history_count!, 0)} events · ${outcomes.reduce((count, item) => count + Math.max(0, item.feedback_history_count! - 1), 0)} corrections`;
}

function OutcomeSnapshot({ label, snapshot }: { label: string; snapshot: (Review | ProcedureRecommendation) & { helpful_count?: number; harmful_count?: number; reason_code?: string } }) {
  return <section aria-label={label} className="my-2 rounded border border-white/10 p-2">
    <h4>{label}</h4><p>Immutable saved evidence; current live feedback is shown separately.</p>
    <p>{snapshot.manual_disclosure}</p><p>{snapshot.quality_disclosure}</p>
    <p>Included manual invocations: {snapshot.included_count} · Helpful: {snapshot.helpful_count ?? snapshot.outcomes.filter(item => item.verified && item.feedback === "helpful").length} · Harmful: {snapshot.harmful_count ?? snapshot.outcomes.filter(item => item.feedback === "harmful").length}</p>
    <p>{feedbackHistory(snapshot.outcomes)}</p><p>Saved outcome: {snapshot.status} · {snapshot.reason_code ?? "reason not recorded"}</p>
    <ul>{snapshot.outcomes.map(item => <li key={item.task_id}>
      {item.task_id} · revision {item.task_revision} · {item.status} · {item.verified === true ? "native verified" : item.verified === false ? "native unverified" : "verification not recorded"}
      {` · feedback ${item.feedback ?? "unreviewed"} · reason ${item.reason_code ?? "not recorded"}`}
      {item.feedback_history_label && !item.feedback_current ? <span> · stale historical {item.feedback_history_label}; ineffective for this saved outcome</span> : null}
      <span> · history events {item.feedback_history_count ?? "not recorded"} · corrections {item.feedback_history_count === undefined ? "not recorded" : Math.max(0, item.feedback_history_count - 1)}</span>
    </li>)}</ul>
  </section>;
}

export function ProcedurePreferenceReview({ ownerPrincipalId, ownerSessionId, scope, onSelectVersion }: Props) {
  const key = useMemo(() => `seraph.procedure-preference:${JSON.stringify([ownerPrincipalId, ownerSessionId,
    scope.routineId, scope.version, scope.routineRevision, scope.goalId, scope.goalRevision])}`,
  [ownerPrincipalId, ownerSessionId, scope.routineId, scope.version, scope.routineRevision, scope.goalId, scope.goalRevision]);
  const currentKey = useRef(key);
  currentKey.current = key;
  const [state, setState] = useState<State | null>(null);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [acknowledgment, setAcknowledgment] = useState<{ key: string; checked: boolean }>({ key, checked: false });
  const [reason, setReason] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const bound = state?.key === key ? state : null;
  const busy = busyKey === key;
  const acknowledged = acknowledgment.key === key && acknowledgment.checked;
  const isOwned = (review: Review) => review.owner_principal_id === ownerPrincipalId && review.owner_session_id === ownerSessionId;

  const refresh = async (base: State) => {
    const captured = key;
    const [list, selection] = await Promise.all([procedurePreferences.outcomes(scope), procedurePreferences.selection(scope)]);
    let review = base.proposalId ? await procedurePreferences.review(base.proposalId) : null;
    const discovered = !base.jobId && base.pending?.kind === "recommend" ? await procedurePreferences.findJob(scope, base.pending.body) : null;
    const recommendation = base.jobId ? await procedurePreferences.inspectJob(scope, base.jobId) : discovered?.job ?? base.recommendation;
    if (!review && recommendation?.proposal_id) review = await procedurePreferences.review(recommendation.proposal_id);
    if ((review && !isOwned(review)) || (selection.review && !isOwned(selection.review))) throw new Error("The review belongs to a different Root.");
    if (currentKey.current === captured) setState({ ...base, jobId: recommendation?.job_id ?? base.jobId, list, selection, review, recommendation });
  };
  useEffect(() => {
    setAcknowledgment({ key, checked: false }); setReason(""); setNotice(null);
    let live = true;
    try {
      const stored = readStored(key);
      const base: State = { ...stored, key, list: null, review: null, recommendation: null, selection: null };
      setState(base);
      // Discovery/reload is GET-only. Retained mutation bodies remain pending.
      void refresh(base).catch((cause) => { if (live && currentKey.current === key) setNotice(String(cause.message ?? cause)); });
    } catch (cause) { setState(null); setNotice(String((cause as Error).message)); }
    return () => { live = false; };
  // The key contains every owner/Root/Goal/version revision used by requests.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const send = async (pending: Pending) => {
    if (!bound || busy) return;
    const captured = key;
    setBusyKey(captured); setNotice(null);
    let next: State = { ...bound, pending };
    try {
      saveStored(captured, next); setState(next);
      if (pending.kind === "recommend") {
        const result = await procedurePreferences.recommend(scope, pending.body);
        next = { ...next, jobId: result.job_id, proposalId: result.proposal_id, recommendation: result };
      } else if (pending.kind === "cancel") {
        const result = await procedurePreferences.cancel(scope, pending.jobId, pending.body);
        next = { ...next, jobId: result.job_id, recommendation: result };
      } else if (pending.kind === "action") {
        const result = await procedurePreferences.act(pending.proposalId, pending.body);
        if (!isOwned(result)) throw new Error("The action readback belongs to a different Root.");
        next = { ...next, proposalId: result.proposal_id, review: result };
      } else {
        await procedurePreferences.feedback(scope, pending.outcome, pending.label, pending.reason, pending.mutationUuid);
      }
      next.pending = null;
      saveStored(captured, next);
      if (currentKey.current === captured) {
        setState(next); setAcknowledgment({ key: captured, checked: false }); setReason("");
        await refresh(next);
      }
    } catch (cause) {
      if (currentKey.current === captured) setNotice(`${String((cause as Error).message)} ${next.pending ? "The exact request is retained; inspect or retry explicitly." : "The action was confirmed; current inspection is unavailable."}`);
    } finally { if (currentKey.current === captured) setBusyKey(null); }
  };
  const act = (action: "accept" | "reject" | "rollback") => {
    if (!bound?.review || !acknowledged || bound.pending || (action === "rollback" && !reason.trim())) return;
    const review = bound.review;
    void send({ kind: "action", proposalId: review.proposal_id, body: {
      action, expected_revision: review.revision, expected_preview_text_digest: review.preview_text_digest,
      expected_bundle_digest: review.bundle_digest, acknowledged_selection_only: true, mutation_uuid: crypto.randomUUID(), reason,
    } });
  };
  const outcomes = bound?.list?.outcomes ?? [];
  const review = bound?.review;
  return <section aria-label="Procedure outcome preference" className="rounded border border-white/10 p-3">
    <h3 className="font-semibold">Review a procedure preference</h3>
    <p>Explicit Helpful or Harmful feedback can suggest this reviewed version. It grants no permission and starts no invocation or schedule.</p>
    <section aria-label="Current live procedure outcomes">
    <h4>Current live outcomes (not the approved snapshot)</h4>
    {bound?.list ? <div role="status"><p>{bound.list.manual_disclosure}</p><p>{bound.list.quality_disclosure}</p><p>Included manual invocations: {bound.list.included_count}</p><p>{feedbackHistory(outcomes)}</p></div> : null}
    <ul>{outcomes.map((outcome) => <li key={outcome.task_id} className="mt-2">
      <span className="font-mono">{outcome.task_id}</span> · {outcome.status} · feedback {outcome.feedback ?? "unreviewed"}
      {outcome.feedback_history_label && !outcome.feedback_current ? <span> · historical {outcome.feedback_history_label} is stale for the current outcome; a new explicit decision is required</span> : null}
      {!outcome.feedback_allowed ? <span> · feedback unavailable until the current attempt has ended</span> : null}
      <span> · history events {outcome.feedback_history_count ?? "not recorded"} · corrections {outcome.feedback_history_count === undefined ? "not recorded" : Math.max(0, outcome.feedback_history_count - 1)}</span>
      {outcome.reason_code ? <span> · reason {outcome.reason_code}</span> : null}
      <button type="button" disabled={busy || Boolean(bound?.pending) || !outcome.feedback_allowed || Boolean(outcome.feedback_event_id) && !reason.trim()}
        onClick={() => void send({ kind: "feedback", outcome, label: "helpful", reason, mutationUuid: crypto.randomUUID() })}>Helpful</button>
      <button type="button" disabled={busy || Boolean(bound?.pending) || !outcome.feedback_allowed || Boolean(outcome.feedback_event_id) && !reason.trim()}
        onClick={() => void send({ kind: "feedback", outcome, label: "harmful", reason, mutationUuid: crypto.randomUUID() })}>Harmful</button>
    </li>)}</ul></section>
    <label>Feedback correction or rollback reason<input aria-label="Procedure feedback reason" value={reason} maxLength={500} disabled={busy} onChange={(event) => setReason(event.currentTarget.value)} /></label>
    <button type="button" disabled={busy || !bound || Boolean(bound.pending)} onClick={() => void send({ kind: "recommend", body: {
      version: scope.version, expected_routine_revision: scope.routineRevision, goal_id: scope.goalId,
      expected_goal_revision: scope.goalRevision, request_uuid: crypto.randomUUID(),
    } })}>Preview outcome recommendation</button>
    {bound?.recommendation ? <p role="status">Saved recommendation receipt: {bound.recommendation.status} · {bound.recommendation.reason_code}.
      {review ? ` Current review: ${review.status}.` : " This receipt records no_learning and creates no canonical preference."}
      {review?.status === "rolled_back" ? " The preference is rolled back; historical adoption is retained." : null}</p> : null}
    {bound?.recommendation ? <OutcomeSnapshot label="Immutable recommendation evidence" snapshot={bound.recommendation} /> : null}
    {bound?.recommendation && ["accepted", "queued", "running", "blocked"].includes(bound.recommendation.job_status)
      && bound.recommendation.job_revision && bound.recommendation.fencing_token !== undefined ?
      <button type="button" disabled={busy || Boolean(bound.pending && bound.pending.kind !== "recommend")}
        onClick={() => { const job = bound.recommendation!; void send({ kind: "cancel", jobId: job.job_id, body: {
          version: scope.version, expected_routine_revision: scope.routineRevision, goal_id: scope.goalId,
          expected_goal_revision: scope.goalRevision, request_uuid: crypto.randomUUID(),
          expected_job_revision: job.job_revision!, expected_fencing_token: job.fencing_token!,
        } }); }}>Cancel owned recommendation</button> : null}
    {review ? <div><OutcomeSnapshot label="Immutable proposal evidence" snapshot={review} /><p>{review.preview_text}</p><p>Review {review.status} · revision {review.revision}</p>
      <label><input type="checkbox" checked={acknowledged} disabled={busy || Boolean(bound?.pending)} onChange={(event) => setAcknowledgment({ key, checked: event.currentTarget.checked })} />I understand this changes future suggestions only and grants no execution authority.</label>
      {review.status === "proposed" ? <><button type="button" disabled={!acknowledged || busy || Boolean(bound?.pending)} onClick={() => act("accept")}>Adopt reviewed preference</button><button type="button" disabled={!acknowledged || busy || Boolean(bound?.pending)} onClick={() => act("reject")}>Reject preference</button></> : null}
      {review.status === "accepted" ? <button type="button" disabled={!acknowledged || busy || !reason.trim() || Boolean(bound?.pending)} onClick={() => act("rollback")}>Roll back preference</button> : null}
    </div> : null}
    {bound?.selection?.status === "suggested" && bound.selection.review ? <div aria-label="Adopted Library suggestion">
      <p>Suggested reviewed version {bound.selection.suggested_version}</p><OutcomeSnapshot label="Adopted preference evidence" snapshot={bound.selection.review} />
      <button type="button" disabled={busy} onClick={() => { if (bound.selection?.suggested_version) onSelectVersion?.(bound.selection.suggested_version); }}>Select suggested reviewed version</button>
    </div> : bound?.selection?.status === "blocked" ? <p role="status">Preference unavailable · {bound.selection.reason_code}</p> : null}
    {bound?.pending ? <p role="alert">An unconfirmed {bound.pending.kind} request is retained. <button type="button" disabled={busy} onClick={() => void send(bound.pending!)}>Retry exact review request</button></p> : null}
    <button type="button" disabled={busy || !bound} onClick={() => { if (bound) void refresh(bound).catch((cause) => setNotice(cause.message)); }}>Check current outcome state</button>
    {notice ? <p role="alert">{notice}</p> : null}
  </section>;
}
