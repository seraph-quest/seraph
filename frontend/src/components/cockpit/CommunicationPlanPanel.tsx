import { useEffect, useRef, useState } from "react";
import type { GoalInfo, WorkBoardTask } from "../../types";
import { cleanupCommunicationPlan, createCommunicationTask, readCommunicationPlan, reviewCommunicationActions } from "../../lib/communications";
import type { CommunicationCleanupRequest, CommunicationCleanupResult, CommunicationCreate, CommunicationPlanRead, CommunicationSelection, ExactCommunicationApproval } from "../../lib/communications";
import { GeneralTaskError } from "../../lib/generalTask";
import { MailReplySendPanel } from "./MailReplySendPanel";
import { CalendarReschedulePanel } from "./CalendarReschedulePanel";

interface Props {
  ownerPrincipalId?: string | null; ownerSessionId?: string | null; goals?: GoalInfo[]; task?: WorkBoardTask;
  selection?: Omit<CommunicationSelection, "acknowledge_private_review">;
  deadlineAt?: string;
  onCreated?: (task: WorkBoardTask) => void | Promise<void>; onClose?: () => void;
}
// An uncertain admission retains its exact request in owner-scoped memory only.
const pendingCreates = new Map<string, CommunicationCreate>();
const button = "cockpit-feedback-button";
export function CommunicationPlanPanel({ ownerPrincipalId, ownerSessionId, goals = [], task, selection, deadlineAt, onCreated, onClose }: Props) {
  const scope = ownerPrincipalId && ownerSessionId ? `${ownerPrincipalId}\u0000${ownerSessionId}` : null;
  const [goalId, setGoalId] = useState(""); const [cost, setCost] = useState("0"); const [ack, setAck] = useState(false);
  const [pending, setPending] = useState<CommunicationCreate | null>(null);
  const [read, setRead] = useState<CommunicationPlanRead | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [approvals, setApprovals] = useState<Record<string, ExactCommunicationApproval | null>>({});
  const [outcomes, setOutcomes] = useState<Record<string, string>>({});
  const [excludedSources, setExcludedSources] = useState<string[]>([]);
  const [pendingCleanup, setPendingCleanup] = useState<CommunicationCleanupRequest | null>(null);
  const [cleanupResult, setCleanupResult] = useState<CommunicationCleanupResult | null>(null);
  const [busy, setBusy] = useState(false); const [message, setMessage] = useState<string | null>(null);
  const generation = useRef(0); const controller = useRef<AbortController | null>(null);
  const owned = Boolean(scope && (!task || (task.owner_principal_id === ownerPrincipalId && task.owner_session_id === ownerSessionId && task.ownership_access !== "recovered_read_only")));
  const ownedGoals = goals.filter(g => g.status === "active" && g.revision && g.owner_session_id === ownerSessionId && g.ownership_access !== "recovered_read_only");
  const goal = ownedGoals.find(g => g.id === goalId);
  const revisionKey = ownedGoals.map(g => `${g.id}:${g.revision}`).join("|");
  const cleanupContext = useRef("");
  cleanupContext.current = JSON.stringify([scope, task?.task_id, task?.task_revision, task?.goal_revision, owned, revisionKey, deadlineAt]);
  const chosen = {
    reply_inputs: selection?.reply_inputs.filter(v => !excludedSources.includes(`reply:${v.message_binding_id}`)) ?? [],
    meeting_inputs: selection?.meeting_inputs.filter(v => !excludedSources.includes(`meeting:${v.event_binding_id}`)) ?? [],
    reschedule_inputs: selection?.reschedule_inputs.filter(v => !excludedSources.includes(`reschedule:${v.event_binding_id}`)) ?? [],
  };
  useEffect(() => {
    ++generation.current; controller.current?.abort(); controller.current = null;
    setRead(null); setSelected([]); setApprovals({}); setOutcomes({}); setBusy(false); setMessage(null); setAck(false);
    setPendingCleanup(null); setCleanupResult(null);
    const retained = scope ? pendingCreates.get(scope) ?? null : null;
    setPending(retained); setGoalId(retained?.goal_id ?? ""); setCost(String(retained?.max_cost_microusd ?? 0));
    return () => { ++generation.current; controller.current?.abort(); };
  }, [scope, task?.task_id, task?.task_revision, task?.goal_revision, owned, revisionKey]);
  useEffect(() => {
    const discard = () => { ++generation.current; controller.current?.abort(); setBusy(false); setRead(null); setSelected([]); setApprovals({}); };
    window.addEventListener("blur", discard); document.addEventListener("visibilitychange", discard);
    return () => { window.removeEventListener("blur", discard); document.removeEventListener("visibilitychange", discard); };
  }, []);
  useEffect(() => {
    if (!deadlineAt) return;
    const cutoff = Date.parse(deadlineAt);
    if (!Number.isFinite(cutoff)) return;
    const timer = setTimeout(() => { ++generation.current; controller.current?.abort(); setBusy(false); setRead(null); setSelected([]); setApprovals({}); setMessage("The original preparation cutoff has passed. Refresh current source authority; old private content was discarded."); }, Math.max(0, cutoff - Date.now()));
    return () => clearTimeout(timer);
  }, [deadlineAt]);
  async function run(action: (signal: AbortSignal, version: number) => Promise<void>) {
    if (!owned || busy) return;
    const version = generation.current; const c = new AbortController(); controller.current = c;
    setBusy(true); setMessage(null);
    try { await action(c.signal, version); }
    catch (error) { if (generation.current === version) { setRead(null); setSelected([]); setApprovals({}); setMessage((error as Error).message || "Original request unconfirmed. Inspect it without creating a replacement."); } }
    finally { if (generation.current === version) { setBusy(false); controller.current = null; } }
  }
  async function create(signal: AbortSignal, version: number) {
    if (!scope || !ownerPrincipalId || !ownerSessionId || (!pending && (!goal?.revision || !ack))) return;
    const body = pending ?? { goal_id: goal!.id, goal_revision: goal!.revision!, max_cost_microusd: Number(cost), wall_seconds: 900,
      idempotency_key: `communication:${crypto.randomUUID()}`, selection: { ...chosen, acknowledge_private_review: true as const } };
    pendingCreates.set(scope, body); setPending(body);
    const result = await createCommunicationTask(body, ownerPrincipalId, ownerSessionId, signal);
    if (generation.current !== version) return;
    pendingCreates.delete(scope); setPending(null); await onCreated?.(result);
  }
  async function open(signal: AbortSignal, version: number) {
    if (!task) return;
    setRead(null); setSelected([]); setApprovals({});
    const result = await readCommunicationPlan(task.task_id, signal);
    if (generation.current === version) setRead(result);
  }
  async function cleanup() {
    if (!task || !owned || busy || (!read && !pendingCleanup)) return;
    const body = pendingCleanup ?? { expected_task_revision: task.task_revision };
    if (body.expected_task_revision !== task.task_revision || (read && read.task_id !== task.task_id)) return;
    const version = generation.current, context = cleanupContext.current;
    const c = new AbortController(); controller.current = c;
    const current = () => generation.current === version && cleanupContext.current === context;
    setPendingCleanup(body); setCleanupResult(null); setBusy(true); setMessage(null);
    setSelected([]); setApprovals({});
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      const result = await Promise.race([
        cleanupCommunicationPlan(task.task_id, body, c.signal),
        new Promise<never>((_, reject) => { timer = setTimeout(() => { c.abort(); reject(Error("Cleanup response timed out; original cleanup remains unconfirmed.")); }, 6500); }),
      ]);
      if (!current() || c.signal.aborted) return;
      setPendingCleanup(null); setCleanupResult(result);
      if (result.absent) setRead(null);
      else setMessage("Cleanup is unresolved. Aggregate artifact absence has not been verified; inspect original cleanup recovery. No automatic retry occurs.");
    } catch (error) {
      if (!current()) return;
      if (error instanceof GeneralTaskError && [401, 403, 404, 410].includes(error.status)) {
        setRead(null); setPendingCleanup(null);
        setMessage("Current authority to the private plan is unavailable. Private content was discarded; physical deletion has not been verified.");
      } else setMessage(`${(error as Error).message} Aggregate cleanup remains unconfirmed; the original request is retained for explicit reconciliation. No replacement task, retry or native action occurs automatically.`);
    } finally {
      clearTimeout(timer);
      if (current()) { setBusy(false); controller.current = null; }
    }
  }
  const plan = read?.plan;
  const selectionMatches = Boolean(goal && chosen.reply_inputs.every(v => v.goal_id === goal.id && v.expected_goal_revision === goal.revision)
    && [...chosen.meeting_inputs, ...chosen.reschedule_inputs].every(v => v.goal_id === goal.id && v.goal_revision === goal.revision)
    && chosen.reschedule_inputs.every(v => chosen.meeting_inputs.some(meeting => meeting.event_binding_id === v.event_binding_id)));
  function toggle(key: string, checked: boolean) {
    setSelected(values => checked ? [...values, key] : values.filter(v => v !== key));
    setApprovals(values => ({ ...values, [key]: null }));
  }
  const completeSubset = selected.length > 0 && selected.every(key => approvals[key] && approvals[key]!.expires_at * 1000 > Date.now());
  return <section className="rounded border border-cyan-500/30 p-3" aria-label="Communication preparation">
    <h3 className="font-semibold">Prepare communications</h3>
    <p className="text-xs">One bounded task prepares private replies and meeting work from explicitly selected current sources. Preparation sends no messages, reschedules no events and records no learning. Each selected action uses its own fresh exact native approval and readback.</p>
    {onClose && <button type="button" className={button} onClick={onClose}>Close communications</button>}
    {!task ? <div className="grid gap-2 mt-2">
      <label>Communication Goal<select className="cockpit-input w-full" value={goalId} disabled={busy || Boolean(pending)} onChange={e => { setGoalId(e.target.value); setAck(false); }}><option value="">Choose current owned Goal</option>{ownedGoals.map(g => <option key={g.id} value={g.id}>{g.title} · revision {g.revision}</option>)}</select></label>
      <p>Selected sources: {chosen.reply_inputs.length} replies · {chosen.meeting_inputs.length} meetings · {chosen.reschedule_inputs.length} reschedule proposals.</p>
      {selection && ([...selection.reply_inputs.map(v => ({ key: `reply:${v.message_binding_id}`, label: `Reply source ${v.message_binding_id}` })), ...selection.meeting_inputs.map(v => ({ key: `meeting:${v.event_binding_id}`, label: `Meeting source ${v.event_binding_id}` })), ...selection.reschedule_inputs.map(v => ({ key: `reschedule:${v.event_binding_id}`, label: `Reschedule proposal ${v.event_binding_id}` }))]).map(source => <label key={source.key}><input type="checkbox" checked={!excludedSources.includes(source.key)} disabled={busy || Boolean(pending)} onChange={e => { setAck(false); setExcludedSources(values => e.target.checked ? values.filter(v => v !== source.key) : [...values, source.key]); }} />{source.label}</label>)}
      <p className="text-xs">Add sources through their existing private Mail review or Calendar event selection. Existing source-specific model consent must still be current.</p>
      <label>Maximum communication cost (microusd)<input className="cockpit-input" type="number" min="0" step="1" value={cost} disabled={busy || Boolean(pending)} onChange={e => setCost(e.target.value)} /></label>
      <label><input type="checkbox" checked={ack} disabled={busy || Boolean(pending)} onChange={e => setAck(e.target.checked)} />I reviewed these selected sources, finite cost limit and private preparation.</label>
      {goal && !selectionMatches && <p role="alert">Selected sources must match this Goal revision, and each reschedule requires its selected meeting. Review the affected sources again.</p>}
      {pending && <p role="status">Original communication admission is unconfirmed. The exact request and idempotency key remain in this owner session's memory.</p>}
      <button type="button" className={button} disabled={busy || !owned || (!pending && (!goal || !ack || !selectionMatches || !Number.isSafeInteger(Number(cost)) || Number(cost) < 0))} onClick={() => void run(create)}>{pending ? "Reconcile exact communication request" : "Prepare one communication task"}</button>
    </div> : <>
      <p role="status">Original task {task.task_id} · {task.status}. Blocked or Unknown work retains its original recovery path on this Work card.</p>
      <button type="button" className={button} disabled={busy || !owned} onClick={() => void run(open)}>Open current private communication plan</button>
      {plan && <div className="grid gap-3 mt-3" aria-label="Private communication plan">
        <p role="status">Current private plan · no learning. Source changes may remove only affected entries; unresolved contacts remain visible.</p>
        <p aria-label="Private plan retention limitation">Access expiry does not delete retained artifacts. Deliberate cleanup removes only this encrypted aggregate plan after verified absence; original Mail drafts, Calendar briefs and approval/effect history retain their existing owner policies.</p>
        {plan.reply_drafts.map(reply => { const key = `reply:${reply.source_ref.source_input_digest}`; return <article key={key} className="rounded border p-2">
          <h4>{reply.subject}</h4><pre className="whitespace-pre-wrap break-words">{reply.body}</pre>{reply.caveats.map(c => <p key={c}>{c}</p>)}
          <label><input type="checkbox" checked={selected.includes(key)} disabled={busy || Boolean(pendingCleanup)} onChange={e => toggle(key, e.target.checked)} />Select this reply for independent exact review</label>
          {selected.includes(key) && <MailReplySendPanel taskId={reply.source_ref.task_id} messageRevision={reply.source_ref.source_revision}
            ownerPrincipalId={ownerPrincipalId} ownerSessionId={ownerSessionId} goalId={task.goal_id} goalRevision={task.goal_revision}
            onExactApproved={value => setApprovals(v => ({ ...v, [key]: value }))}
            onReadback={value => setOutcomes(v => ({ ...v, [key]: `${value.status} · ${value.outcome ?? "awaiting independent readback"}` }))} />}
          {outcomes[key] && <p role="status">Reply result: {outcomes[key]}</p>}
        </article>; })}
        {plan.meeting_preparations.map(meeting => <article key={meeting.source_ref.source_input_digest} className="rounded border p-2"><h4>Meeting preparation</h4><p>{meeting.brief.summary}</p>{(["agenda", "questions", "risks", "preparation_steps"] as const).map(field => <div key={field}><h5>{field.replace(/_/g, " ")}</h5><ul>{meeting.brief[field].map((v, index) => <li key={index}>{v}</li>)}</ul></div>)}</article>)}
        {plan.reschedule_proposals.map(proposal => { const key = `reschedule:${proposal.source_ref.source_input_digest}`; return <article key={key} className="rounded border p-2">
          <p>Proposed time: {proposal.input.new_start.dateTime} → {proposal.input.new_end.dateTime} · {proposal.input.new_start.timeZone}</p>
          <label><input type="checkbox" checked={selected.includes(key)} disabled={busy || Boolean(pendingCleanup)} onChange={e => toggle(key, e.target.checked)} />Select this reschedule for independent exact review</label>
          {selected.includes(key) && <CalendarReschedulePanel preparedInput={proposal.input} eventBindingId={proposal.input.event_binding_id}
            eventBindingRevision={proposal.input.expected_event_binding_revision} goalId={task.goal_id} goalRevision={task.goal_revision}
            ownerPrincipalId={ownerPrincipalId} ownerSessionId={ownerSessionId} goals={ownedGoals}
            onExactApproved={value => setApprovals(v => ({ ...v, [key]: value }))} onReadback={value => setOutcomes(v => ({ ...v, [key]: `${value.status} · ${value.outcome ?? value.failure_reason ?? "awaiting independent readback"}` }))} />}
          {outcomes[key] && <p role="status">Reschedule result: {outcomes[key]}</p>}
        </article>; })}
        {!plan.reply_drafts.length && !plan.meeting_preparations.length && !plan.reschedule_proposals.length && <p>No prepared entries are currently available.</p>}
        {plan.unresolved_questions.map((question, index) => <p role="status" key={`${question.source_id}:${index}`}>Unresolved source {question.source_id}: {question.reason}. Recovery: {question.recovery.replace(/_/g, " ")}{question.job_id ? ` · original job ${question.job_id}` : ""}.</p>)}
        <button type="button" className={button} disabled={busy || !completeSubset} onClick={() => void run(async (signal, version) => {
          const bundle = { selected_actions: selected.map(key => { const [kind, source_input_digest] = key.split(":"); return { kind: kind as "reply" | "reschedule", source_input_digest, operation_id: approvals[key]!.operation_id }; }),
            exact_preview_digests: selected.map(key => approvals[key]!.exact_preview_digest), approval_ids: selected.map(key => approvals[key]!.approval_id) };
          await reviewCommunicationActions(task.task_id, bundle, signal);
          if (generation.current === version) setMessage("Selected exact approvals are current. Execute each action through its own native controls; this review performs no effects.");
        })}>Validate selected independent exact approvals</button>
      </div>}
      {(plan || pendingCleanup) && <button type="button" className={button} disabled={busy || !owned || (pendingCleanup?.expected_task_revision ?? task.task_revision) !== task.task_revision}
        onClick={() => void cleanup()}>{pendingCleanup ? "Reconcile exact communication cleanup" : "Clean up aggregate private plan"}</button>}
      {pendingCleanup && <p role="status">Original aggregate cleanup is unconfirmed at task revision {pendingCleanup.expected_task_revision}. Reconcile this exact request explicitly; no new task, budget or native action is authorized.</p>}
      {cleanupResult && <p role="status" aria-label="Aggregate private plan cleanup result">{cleanupResult.absent
        ? "Aggregate private plan absence verified. Source artifacts and approval/effect history remain with their original owners; no learning occurred."
        : "Aggregate private plan cleanup unresolved. Physical absence is not verified; no learning occurred."}</p>}
    </>}
    {message && <p role="alert">{message}</p>}
  </section>;
}
