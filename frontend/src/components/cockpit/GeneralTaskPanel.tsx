import { useEffect, useRef, useState } from "react";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import type { GoalInfo, WorkBoardTask } from "../../types";
import { canResumeGeneralTask, createGeneralTask, GeneralTaskError, generalTaskRequest, validateGeneralTaskPlan } from "../../lib/generalTask";
import type { GeneralTaskCreateRequest, GeneralTaskPlanRead, TaskPlan } from "../../lib/generalTask";
import { parseDocumentPreparationView, type DocumentPreparationView } from "../../lib/documentPreparation";
import { DocumentBuildEditor } from "./DocumentBuildEditor";

interface Props {
  ownerPrincipalId?: string | null; ownerSessionId?: string | null;
  task?: WorkBoardTask; goals?: GoalInfo[]; onClose?: () => void;
  onCreated?: (task: WorkBoardTask) => void | Promise<void>; onChanged?: () => void | Promise<void>;
}
// Pending intent stays in memory under its original owner, never in browser storage.
const pendingCreates = new Map<string, GeneralTaskCreateRequest>();
async function readDocumentPreparation(taskId: string, selectedRefs: string[]): Promise<DocumentPreparationView> {
  const response = await apiFetch(`${API_URL}/api/documents/preparations/${encodeURIComponent(taskId)}`);
  if (!response.ok) {
    let code = "";
    try {
      const value: unknown = await response.json();
      if (value && typeof value === "object" && !Array.isArray(value) && "detail" in value) {
        const detail = (value as { detail?: unknown }).detail;
        if (detail && typeof detail === "object" && !Array.isArray(detail) && "code" in detail && typeof (detail as { code?: unknown }).code === "string") code = (detail as { code: string }).code;
      }
    } catch { /* The status still gives the operator a bounded recovery path. */ }
    throw Error(code === "document_preparation_not_completed"
      ? "Authenticated private preparation is not complete. Review the task's current status and retry this explicit readback later."
      : `Authenticated private preparation blocked (${response.status}${code ? `, ${code}` : ""}). Refresh the current task plan before retrying.`);
  }
  return parseDocumentPreparationView(await response.json(), selectedRefs);
}
export function GeneralTaskPanel({ ownerPrincipalId, ownerSessionId, task, goals = [], onClose, onCreated, onChanged }: Props) {
  const scope = ownerPrincipalId && ownerSessionId ? `${ownerPrincipalId}:${ownerSessionId}` : null;
  const [pending, setPending] = useState<GeneralTaskCreateRequest | null>(() => scope ? pendingCreates.get(scope) ?? null : null);
  const [goalId, setGoalId] = useState(pending?.input.goal_ref ?? "");
  const [intent, setIntent] = useState(pending?.input.intent ?? "");
  const [cost, setCost] = useState(String(pending?.input.limits.max_cost_microusd ?? 0));
  const [egress, setEgress] = useState(pending?.input.inference_egress_acknowledged ?? false);
  const [read, setRead] = useState<GeneralTaskPlanRead | null>(null);
  const [localDocument, setLocalDocument] = useState(false);
  const [busy, setBusy] = useState(false), [error, setError] = useState<string | null>(null);
  const [preparationView, setPreparationView] = useState<DocumentPreparationView | null>(null);
  const [ack, setAck] = useState(false);
  const [planDraft, setPlanDraft] = useState("");
  const [replacementDraft, setReplacementDraft] = useState("");
  const [revisionReason, setRevisionReason] = useState("");
  const [pendingRevision, setPendingRevision] = useState<{ expected_revision: number; replacements: TaskPlan["steps"]; reason: string; idempotency_key: string } | null>(null);
  const [pendingEdit, setPendingEdit] = useState<{ expected_revision: number; expected_plan_revision: number; idempotency_key: string; plan: TaskPlan } | null>(null);
  const generation = useRef(0);
  const ownedGoals = goals.filter(g => g.status === "active" && g.revision && g.owner_session_id === ownerSessionId && g.ownership_access !== "recovered_read_only");
  const goal = ownedGoals.find(g => g.id === goalId);
  const owned = Boolean(scope && (!task || (task.owner_principal_id === ownerPrincipalId && task.owner_session_id === ownerSessionId && task.ownership_access !== "recovered_read_only")));
  async function refresh() {
    if (!task || !owned) return;
    const version = generation.current;
    setRead(null); setAck(false); setError(null); setPreparationView(null);
    try {
      const value = validateGeneralTaskPlan(await generalTaskRequest(`/tasks/${encodeURIComponent(task.task_id)}/plan`), task);
      if (!value.plan) {
        const registry = await generalTaskRequest("/general-tasks/tools");
        if (registry && typeof registry === "object" && "tools" in registry && Array.isArray(registry.tools)) {
          value.descriptors = registry.tools.filter(d => d && typeof d === "object" && typeof d.tool_id === "string" && Array.isArray(d.effects) && Array.isArray(d.permissions));
        }
      }
      if (version === generation.current) {
        setRead(value); setPlanDraft(JSON.stringify(value.plan?.steps ?? [], null, 2));
        const frozen = new Set(value.native_execution?.steps.map(step => step.step_id) ?? []);
        setReplacementDraft(JSON.stringify(value.plan?.steps.filter(step => !frozen.has(step.step_id)) ?? [], null, 2));
      }
    } catch (e) { if (version === generation.current) setError((e as Error).message); }
  }
  useEffect(() => {
    ++generation.current; setBusy(false); setRead(null); setAck(false); setError(null); setPendingEdit(null); setPlanDraft("");
    setPendingRevision(null); setReplacementDraft(""); setRevisionReason("");
    const retained = scope ? pendingCreates.get(scope) ?? null : null;
    setPending(retained); setIntent(retained?.input.intent ?? ""); setGoalId(retained?.input.goal_ref ?? "");
    setCost(String(retained?.input.limits.max_cost_microusd ?? 0)); setEgress(retained?.input.inference_egress_acknowledged ?? false);
    if (task && owned) void refresh();
    return () => { ++generation.current; };
  }, [scope, task?.task_id, task?.task_revision, owned]); // Exact scope fences late readbacks.
  async function create() {
    if (!scope || !ownerPrincipalId || !ownerSessionId || !owned || busy) return;
    const version = generation.current;
    let request = pending;
    if (!request) {
      if (!goal?.revision || !intent.trim() || new TextEncoder().encode(intent).length > 8192 || !Number.isSafeInteger(Number(cost)) || Number(cost) < 0) {
        setError("Choose an active owned Goal and an intent of at most 8192 UTF-8 bytes with a finite nonnegative budget."); return;
      }
      request = { goal_revision: goal.revision, idempotency_key: crypto.randomUUID(), input: {
        goal_ref: goal.id, intent, evidence_refs: [], requested_output: { type: "object" },
        limits: { max_steps: 16, max_inference_calls: 12, wall_seconds: 900, depth: 0, max_outstanding_children: 2, max_cost_microusd: Number(cost) },
        inference_egress_acknowledged: egress,
      } };
      pendingCreates.set(scope, request); setPending(request);
    }
    setBusy(true); setError(null);
    try {
      const result = await createGeneralTask(request, ownerPrincipalId, ownerSessionId);
      if (version !== generation.current) return;
      pendingCreates.delete(scope); setPending(null); setIntent(""); await onCreated?.(result);
    } catch (e) { if (version === generation.current) {
      if (e instanceof GeneralTaskError && [400, 403, 409, 422].includes(e.status)) { pendingCreates.delete(scope); setPending(null); }
      setError((e as Error).message);
    } }
    finally { if (version === generation.current) setBusy(false); }
  }
  async function accept() {
    if (!task || !read?.plan || read.accepted || !ack || !owned || busy || pendingEdit || planDraft !== JSON.stringify(read.plan.steps, null, 2)) return;
    const version = generation.current; setBusy(true); setError(null); setAck(false);
    try {
      await generalTaskRequest(`/tasks/${encodeURIComponent(task.task_id)}/actions`, { action: "promote", expected_revision: read.task_revision });
      if (version === generation.current) { setRead(null); await onChanged?.(); }
    } catch (e) { if (version === generation.current) { setRead(null); setError((e as Error).message); } }
    finally { if (version === generation.current) setBusy(false); }
  }
  async function openPrivatePreparation() {
    const binding = read?.task_input.document_source;
    if (!task || !read || !binding || busy || !owned) return;
    const version = generation.current;
    setBusy(true); setError(null); setPreparationView(null);
    try {
      const view = await readDocumentPreparation(task.task_id, binding.citation_refs);
      if (version === generation.current) setPreparationView(view);
    } catch (e) { if (version === generation.current) setError((e as Error).message); }
    finally { if (version === generation.current) setBusy(false); }
  }
  async function resume() {
    if (!task || !read || !owned || busy || !canResumeGeneralTask(read, task)) return;
    const pause = read.approval_pause!, version = generation.current;
    setBusy(true); setRead(null); setError(null);
    try {
      const result = await generalTaskRequest(`/tasks/${encodeURIComponent(task.task_id)}/plan/resume`, {
        expected_revision: read.task_revision, expected_plan_revision: read.plan!.revision,
        workflow_run_id: pause.workflow_run_id, attempt_id: pause.attempt_id, fencing_token: pause.fencing_token,
        workflow_revision: pause.workflow_revision, approval_id: pause.approval_id,
        ...(pause.child_job_id ? { child_job_id: pause.child_job_id,
          expected_manifest_revision: pause.expected_manifest_revision } : {}),
      });
      if (version !== generation.current) return;
      const receipt = result && typeof result === "object" && "task" in result ? result.task as WorkBoardTask : null;
      if (!receipt || receipt.task_id !== task.task_id || receipt.owner_principal_id !== ownerPrincipalId
        || receipt.owner_session_id !== ownerSessionId || receipt.latest_attempt?.attempt_id !== pause.attempt_id
        || receipt.latest_attempt.workflow_run_id !== pause.workflow_run_id
        || (pause.child_job_id ? receipt.latest_attempt.fencing_token <= pause.fencing_token
          : receipt.latest_attempt.fencing_token !== pause.fencing_token + 1)) {
        throw Error("Continuation receipt is unconfirmed. Refresh Work and the current plan before any further action.");
      }
      await onChanged?.();
    } catch (e) { if (version === generation.current) {
      setError(`${(e as Error).message} Refresh Work and the current plan to inspect the same run; continuation is never automatically replayed.`);
      await onChanged?.();
    } } finally { if (version === generation.current) setBusy(false); }
  }
  async function savePlan() {
    if (!task || !read || read.accepted || !owned || busy) return;
    const version = generation.current;
    let request = pendingEdit;
    if (!request) {
      try {
        const steps: unknown = JSON.parse(planDraft);
        if (!Array.isArray(steps) || !steps.length || steps.length > read.task_input.limits.max_steps) throw Error("Plan must contain a bounded list of steps.");
        request = { expected_revision: read.task_revision, expected_plan_revision: read.plan?.revision ?? 0,
          idempotency_key: crypto.randomUUID(), plan: { schema_version: 1, revision: (read.plan?.revision ?? 0) + 1, steps: steps as TaskPlan["steps"] } };
      } catch (e) { setError((e as Error).message); return; }
      setPendingEdit(request);
    }
    setBusy(true); setAck(false); setError(null);
    try {
      const result = await generalTaskRequest(`/tasks/${encodeURIComponent(task.task_id)}/plan`, request);
      if (version !== generation.current) return;
      if (!result || typeof result !== "object" || !("task" in result)) throw Error("Plan edit receipt is unconfirmed. Reconcile the exact edit before acceptance.");
      setPendingEdit(null); setRead(null); await onChanged?.();
    } catch (e) { if (version === generation.current) {
      if (e instanceof GeneralTaskError && [400, 403, 409, 422].includes(e.status)) { setPendingEdit(null); if (e.status === 409) setRead(null); }
      setError((e as Error).message);
    } }
    finally { if (version === generation.current) setBusy(false); }
  }
  async function revisePausedPlan() {
    if (!task || !read?.plan || read.native_execution?.phase !== "operator_paused" || !owned || busy) return;
    const version = generation.current;
    let request = pendingRevision;
    if (!request) {
      try {
        const replacements: unknown = JSON.parse(replacementDraft);
        if (!Array.isArray(replacements) || !replacements.length || replacements.length > 16
          || !revisionReason.trim() || revisionReason.length > 500
          || new TextEncoder().encode(replacementDraft).length > 65536) throw Error("Provide bounded replacement steps and a revision reason.");
        const frozen = new Set(read.native_execution.steps.map(step => step.step_id));
        const editable = read.plan.steps.filter(step => !frozen.has(step.step_id));
        const edited = replacements as TaskPlan["steps"];
        if (edited.length !== editable.length || new Set(edited.map(step => step.step_id)).size !== edited.length
          || edited.some(step => !editable.some(original => original.step_id === step.step_id))) throw Error("Keep the current unstarted step IDs; admitted steps remain frozen.");
        const completePlan = read.plan.steps.map(step => frozen.has(step.step_id) ? step : edited.find(row => row.step_id === step.step_id)!);
        request = { expected_revision: read.task_revision, replacements: completePlan,
          reason: revisionReason, idempotency_key: crypto.randomUUID() };
        setPendingRevision(request);
      } catch (e) { setError((e as Error).message); return; }
    }
    setBusy(true); setError(null);
    try {
      const result = await generalTaskRequest(`/tasks/${encodeURIComponent(task.task_id)}/plan/revise`, request);
      if (version !== generation.current) return;
      const receipt = result && typeof result === "object" && "task" in result ? result.task as WorkBoardTask : null;
      if (!receipt || receipt.task_id !== task.task_id || receipt.owner_principal_id !== ownerPrincipalId
        || receipt.owner_session_id !== ownerSessionId || receipt.task_revision <= read.task_revision
        || receipt.block_reason !== "general_task_operator_paused"
        || receipt.latest_attempt?.attempt_id !== task.latest_attempt?.attempt_id) throw Error("Paused revision receipt is unconfirmed. Refresh the original plan before further action.");
      setPendingRevision(null); setRead(null); await onChanged?.();
    } catch (e) { if (version === generation.current) {
      setError((e as Error).message);
      if (e instanceof GeneralTaskError && [400, 403, 409, 422].includes(e.status)) { setPendingRevision(null); if (e.status === 409) setRead(null); }
    } } finally { if (version === generation.current) setBusy(false); }
  }
  async function control(action: "pause" | "resume" | "cancel") {
    if (!task || !read?.native_execution || !owned || busy || read.task_revision !== task.task_revision) return;
    const version = generation.current, originalAttempt = task.latest_attempt;
    setBusy(true); setRead(null); setError(null);
    try {
      const result = await generalTaskRequest(`/tasks/${encodeURIComponent(task.task_id)}/actions`, {
        action, expected_revision: read.task_revision,
      });
      if (version !== generation.current) return;
      const receipt = result && typeof result === "object" && "task" in result ? result.task as WorkBoardTask : null;
      if (!receipt || receipt.task_id !== task.task_id || receipt.owner_principal_id !== ownerPrincipalId
        || receipt.owner_session_id !== ownerSessionId || receipt.task_revision <= task.task_revision
        || receipt.latest_attempt?.attempt_id !== originalAttempt?.attempt_id
        || receipt.latest_attempt?.workflow_run_id !== originalAttempt?.workflow_run_id) {
        throw Error("Task control receipt is unconfirmed.");
      }
      await onChanged?.();
    } catch (e) { if (version === generation.current) {
      setError(`${(e as Error).message} Refresh Work and inspect the original run before another action; controls are never automatically replayed.`);
      await onChanged?.();
    } } finally { if (version === generation.current) setBusy(false); }
  }
  const documentBinding = read?.task_input.document_source;
  const documentPreparation = Boolean(documentBinding && read?.plan?.steps.length === 1 && read.plan.steps[0]?.tool_id === "document_prepare");
  const documentBuild = Boolean(read?.task_input.document_build);
  return <section className="rounded border border-white/15 bg-slate-950 p-4 text-slate-100" aria-label={task ? (documentBuild ? "Local document build task" : documentPreparation ? "Local document preparation plan" : "Ordinary task plan") : "Describe a task"}>
    <div className="flex justify-between gap-2"><h2 className="font-semibold">{task ? (documentBuild ? "Review local document build task" : documentPreparation ? "Review local document preparation plan" : "Review ordinary task plan") : "Describe a task"}</h2>{onClose && <button type="button" disabled={busy || Boolean(pending)} onClick={onClose}>Close</button>}</div>
    {!owned && <p role="status">Blocked: current task ownership is required. Recover through Work before changing the task.</p>}
    {error && <p role="alert" className="text-amber-200">{error}</p>}
    {!task && <label><input type="checkbox" checked={localDocument} disabled={busy || Boolean(pending)} onChange={e => setLocalDocument(e.target.checked)} />Build a local editable document and PDF</label>}
    {!task && localDocument && ownerPrincipalId && ownerSessionId && <DocumentBuildEditor ownerPrincipalId={ownerPrincipalId} ownerSessionId={ownerSessionId} goals={goals} onCreated={onCreated} onChanged={onChanged} />}
    {!task && !localDocument && <>
      <p className="text-xs">Describe the outcome in ordinary language. Seraph proposes a persisted typed plan from registered tools; creation does not accept or execute it.</p>
      {pending && <p role="status">An exact request has an unconfirmed receipt. Retry with the same owner, input and idempotency key; inspect Work before starting another task.</p>}
      <fieldset disabled={busy || Boolean(pending) || !owned} className="grid gap-3 mt-3">
        <label>Task Goal<select aria-label="Task Goal" value={goalId} onChange={e => setGoalId(e.target.value)}><option value="">Choose active Goal</option>{ownedGoals.map(g => <option key={g.id} value={g.id}>{g.title} · revision {g.revision}</option>)}</select></label>
        <label>What should Seraph do?<textarea aria-label="What should Seraph do?" className="cockpit-input w-full" rows={4} maxLength={8192} value={intent} onChange={e => setIntent(e.target.value)} /></label>
        <label>Inference budget in micro USD<input aria-label="Inference budget in micro USD" type="number" min={0} step={1} value={cost} onChange={e => setCost(e.target.value)} /></label>
        <label><input type="checkbox" checked={egress} onChange={e => setEgress(e.target.checked)} />I acknowledge sending this intent and public tool contracts through the governed inference route. Current consent and capability gates still apply.</label>
        <p className="text-xs">Bounds: 16 steps, 12 inference calls, 900 seconds, depth 0, 2 outstanding children. Output: a JSON object. Zero budget leaves inference-dependent planning blocked.</p>
      </fieldset>
      <button type="button" className="cockpit-feedback-button mt-3" disabled={busy || !owned || (!pending && (!goal || !intent.trim()))} onClick={() => void create()}>{busy ? "Preparing plan…" : pending ? "Retry exact task request" : "Prepare task plan"}</button>
    </>}
    {task && <>
      <p role="status">{task.status} · {read?.accepted ? "plan accepted" : "plan awaiting review"} · {task.block_reason ?? "no active block"}</p>
      <button type="button" disabled={busy || !owned || Boolean(pendingEdit)} onClick={() => void refresh()}>Refresh current task plan</button>
      {read && <>
        <p className="text-xs">Task revision {read.task_revision} · plan revision {read.plan?.revision ?? "not yet valid"} · no_learning. Acceptance grants no new permission; the dispatcher owns admission and each effect still requires its current approval.</p>
        <div aria-label="Original task method binding">
          {read.strategy.status === "active" ? <>
            <p className="break-all">Reviewed method {read.strategy.method_id} · immutable version {read.strategy.version} · digest {read.strategy.digest}</p>
            <pre className="whitespace-pre-wrap break-all text-xs">{JSON.stringify(read.strategy.typed_data, null, 2)}</pre>
            <p>Quality is unmeasured. Admission pins this version; future selection changes do not replace it. Current revocation and source authority still apply.</p>
          </> : <p>{read.strategy.status === "none" ? "Explicit baseline task method" : "Task method blocked"} · {read.strategy.reason}</p>}
        </div>
        {read.native_execution && <section aria-label="Native task execution" className="mt-3 rounded border border-white/10 p-2">
          <p role="status">Native phase {read.native_execution.phase} · manifest revision {read.native_execution.manifest_revision}</p>
          {read.native_execution.cancellation?.state && <p role="status">{
            read.native_execution.cancellation?.state === "pending" ? "Cancellation fenced this run. The original tool callback or effect remains unresolved."
              : read.native_execution.cancellation?.state === "callback_closed_outcome_debt" ? "The original callback has closed. Its effect outcome still requires reconciliation."
                : "Cancellation completed with original tool closure and known effect outcomes."}</p>}
          <p>Original deadline {read.native_execution.original_deadline_at} · native cutoff {read.native_execution.native_deadline_at}</p>
          <p>Remaining work: {read.native_execution.remaining_steps.join(", ") || "none"}</p>
          {read.native_execution.steps.map(step => <p key={step.step_id}>{step.step_id} · {step.status} · {step.contact_state}</p>)}
          <p>Verified partial outputs: {read.native_execution.partial_output_refs.length}</p>
          {read.native_execution.partial_output_refs.map(ref => <p key={ref.artifact_id} className="break-all text-xs">{ref.artifact_id}</p>)}
          <p className="text-xs">Partial outputs remain separate from final task success. Unknown contact requires reconciliation before continuing.</p>
          <div className="mt-2 flex flex-wrap gap-2">
            <button type="button" disabled={busy || !owned || !["native_ready", "assembly", "native_wait"].includes(read.native_execution.phase)
              || read.native_execution.steps.some(step => !["verified", "cancelled"].includes(step.status))}
              onClick={() => void control("pause")}>Pause remaining task work</button>
            <button type="button" disabled={busy || !owned || read.native_execution.phase !== "operator_paused"}
              onClick={() => void control("resume")}>Resume paused task work</button>
            <button type="button" disabled={busy || !owned || Boolean(read.native_execution.cancellation?.state) || !task.latest_attempt || Boolean(task.latest_attempt.ended_at)
              || !["running", "blocked"].includes(task.status)} onClick={() => void control("cancel")}>Cancel native task work</button>
          </div>
          <p className="text-xs">Safe pause requires closed tool work. Cancellation fences future work and late output; an uncertain contacted tool remains visible until reconciliation.</p>
        </section>}
        {read.approval_pause && <section aria-label="Paused task approval" className="mt-3 rounded border border-white/10 p-2">
          <p role="status">Approval {read.approval_pause.approval_status} · {read.approval_pause.step_id} · {read.approval_pause.tool_id}{read.approval_pause.reason ? ` · ${read.approval_pause.reason}` : ""}</p>
          <p>Existing run {read.approval_pause.workflow_run_id} · attempt {read.approval_pause.attempt_id} · original deadline {read.approval_pause.original_deadline_at}</p>
          <p>Review the existing approval on this Work card, then refresh the current task plan. Continuing retains this run and deadline.</p>
          <button type="button" disabled={busy || !owned || !canResumeGeneralTask(read, task)} onClick={() => void resume()}>Continue approved task run</button>
        </section>}
        {documentBuild && ownerPrincipalId && ownerSessionId && <DocumentBuildEditor ownerPrincipalId={ownerPrincipalId} ownerSessionId={ownerSessionId} goals={goals} task={task} read={read} onChanged={onChanged} />}
        {!documentBuild && <>
        {!read.plan && <p role="status">Proposal blocked: {read.proposal_error}. The intent is retained on this Triage card. Edit a typed plan with registered tools, save a valid revision, then review again.</p>}
        {!read.plan && <details><summary>Current registered tools for plan recovery</summary><pre className="whitespace-pre-wrap break-all text-xs">{JSON.stringify(read.descriptors.map(d => ({ tool_id: d.tool_id, version: d.version, input_schema: d.input_schema, output_schema: d.output_schema, effects: d.effects, permissions: d.permissions, deadline: d.deadline, verifier: d.verifier })), null, 2)}</pre></details>}
        <p className="whitespace-pre-wrap">{read.task_input.intent}</p>
        <pre aria-label="Task limits" className="whitespace-pre-wrap text-xs">{JSON.stringify(read.task_input.limits, null, 2)}</pre>
        {read.plan?.steps.map(step => {
          const descriptor = read.descriptors.find(d => d.tool_id === step.tool_id)!;
          return <section key={step.step_id} className="mt-3 rounded border border-white/10 p-2" aria-label={`Plan step ${step.step_id}`}>
            <h3>{step.step_id} · {step.tool_id} v{descriptor.version}</h3>
            <p>Depends on: {step.depends_on.join(", ") || "none"}</p>
            <p>Effects: {descriptor.effects.join(", ")} · permissions: {descriptor.permissions.join(", ")}</p>
            <p>Deadline: {descriptor.deadline}s · verifier: {descriptor.verifier}</p>
            <details><summary>Typed input and output contract</summary><pre className="whitespace-pre-wrap break-all text-xs">{JSON.stringify({ input: step.input, input_schema: descriptor.input_schema, output_contract: step.output_contract, registered_output_schema: descriptor.output_schema }, null, 2)}</pre></details>
          </section>;
        })}
        {documentPreparation && documentBinding && <section aria-label="Local document preparation" className="mt-3 rounded border border-cyan-500/30 p-2">
          <p className="font-semibold">Owner-bound local document source</p>
          <p className="break-all text-xs">{documentBinding.artifact_ref} · source revision {documentBinding.source_revision} · {documentBinding.citation_refs.length} exact citations · selection {documentBinding.selection_digest}</p>
          <p className="text-xs">Local-use acknowledgement is persisted separately from model egress. The private cited view is opened only by this explicit authenticated readback action.</p>
          <button type="button" disabled={busy || !owned} onClick={() => void openPrivatePreparation()}>Open authenticated private preparation</button>
          {preparationView && <section aria-label="Authenticated private cited preparation" className="mt-2 rounded border border-emerald-500/30 p-2">
            <p role="status">Authenticated private cited preparation · no provider contact · no learning.</p>
            {preparationView.sections.map((section) => <article key={section.source_ref} className="mt-2"><h4 className="break-all font-mono">{section.source_ref}</h4><pre className="whitespace-pre-wrap break-all">{section.text}</pre>{(section.formula !== null || section.cached_value !== null) && <dl><dt>Formula (inert)</dt><dd className="whitespace-pre-wrap break-all">{section.formula ?? "none"}</dd><dt>Cached value (freshness unknown)</dt><dd className="whitespace-pre-wrap break-all">{section.cached_value ?? "unavailable"}</dd></dl>}{section.cached_value === null && <p role="status">Cached value unavailable; freshness unknown. Formula remains inert.</p>}</article>)}
          </section>}
        </section>}
        {read.native_execution?.phase === "operator_paused" && <details className="mt-3">
          <summary>Revise unstarted task steps</summary>
          <p className="text-xs">Admitted and completed steps stay fixed. Edit the current unstarted step IDs below. Saving keeps this original run safely paused; review its current plan before resuming.</p>
          <label>Replacement steps<textarea aria-label="Replacement steps" className="cockpit-input w-full font-mono text-xs" rows={10} maxLength={65536} value={replacementDraft} disabled={busy || !owned || Boolean(pendingRevision)} onChange={e => setReplacementDraft(e.target.value)} /></label>
          <label>Revision reason<input aria-label="Revision reason" className="cockpit-input w-full" maxLength={500} value={revisionReason} disabled={busy || !owned || Boolean(pendingRevision)} onChange={e => setRevisionReason(e.target.value)} /></label>
          <button type="button" disabled={busy || !owned || (!pendingRevision && !revisionReason.trim())} onClick={() => void revisePausedPlan()}>{pendingRevision ? "Reconcile exact paused revision" : "Save paused plan revision"}</button>
        </details>}
        {!read.accepted && <>
          {!documentPreparation && <details className="mt-3"><summary>Edit typed plan steps</summary><p className="text-xs">Edit only registered tool IDs, typed inputs, dependencies and output contracts shown above. Saving creates a new inert revision; permissions and limits stay server owned.</p>
            <label>Typed plan steps<textarea aria-label="Typed plan steps" className="cockpit-input w-full font-mono text-xs" rows={12} maxLength={65536} value={planDraft} disabled={busy || !owned || Boolean(pendingEdit)} onChange={e => { setPlanDraft(e.target.value); setAck(false); }} /></label>
            <button type="button" disabled={busy || !owned || (!pendingEdit && planDraft === JSON.stringify(read.plan?.steps ?? [], null, 2))} onClick={() => void savePlan()}>{pendingEdit ? "Reconcile exact plan edit" : "Save inert plan revision"}</button>
          </details>}
          {!documentPreparation && pendingEdit && <p role="status">The exact edit has an unconfirmed receipt. Reconcile it before refreshing or accepting.</p>}
          <label className="mt-3 flex gap-2"><input type="checkbox" checked={ack} disabled={busy || !owned || !read.plan || Boolean(pendingEdit) || planDraft !== JSON.stringify(read.plan?.steps ?? [], null, 2)} onChange={e => setAck(e.target.checked)} />I reviewed this exact plan, effects, permissions and limits.</label><button type="button" className="cockpit-feedback-button" disabled={busy || !owned || !read.plan || !ack || Boolean(pendingEdit) || planDraft !== JSON.stringify(read.plan?.steps ?? [], null, 2)} onClick={() => void accept()}>Accept reviewed task plan</button>
        </>}
        </>}
      </>}
      <p className="mt-2 text-xs">Input, approval, artifact and recovery receipts remain on this Work card. If a revision or authority changes, refresh and review again; uncertain effects must be reconciled before retry.</p>
    </>}
  </section>;
}
