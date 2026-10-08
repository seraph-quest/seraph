import { useEffect, useRef, useState } from "react";
import type { GoalInfo, WorkBoardTask } from "../../types";
import { createGeneralTask, generalTaskRequest, validateGeneralTaskPlan } from "../../lib/generalTask";
import type { GeneralTaskCreateRequest, GeneralTaskPlanRead } from "../../lib/generalTask";

interface Props {
  ownerPrincipalId?: string | null; ownerSessionId?: string | null;
  task?: WorkBoardTask; goals?: GoalInfo[]; onClose?: () => void;
  onCreated?: (task: WorkBoardTask) => void | Promise<void>; onChanged?: () => void | Promise<void>;
}
// Pending intent stays in memory under its original owner, never in browser storage.
const pendingCreates = new Map<string, GeneralTaskCreateRequest>();
export function GeneralTaskPanel({ ownerPrincipalId, ownerSessionId, task, goals = [], onClose, onCreated, onChanged }: Props) {
  const scope = ownerPrincipalId && ownerSessionId ? `${ownerPrincipalId}:${ownerSessionId}` : null;
  const [pending, setPending] = useState<GeneralTaskCreateRequest | null>(() => scope ? pendingCreates.get(scope) ?? null : null);
  const [goalId, setGoalId] = useState(pending?.input.goal_ref ?? "");
  const [intent, setIntent] = useState(pending?.input.intent ?? "");
  const [cost, setCost] = useState(String(pending?.input.limits.max_cost_microusd ?? 0));
  const [egress, setEgress] = useState(pending?.input.inference_egress_acknowledged ?? false);
  const [read, setRead] = useState<GeneralTaskPlanRead | null>(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState<string | null>(null);
  const [ack, setAck] = useState(false);
  const [planDraft, setPlanDraft] = useState("");
  const [pendingEdit, setPendingEdit] = useState<{ expected_revision: number; expected_plan_revision: number; idempotency_key: string; plan: GeneralTaskPlanRead["plan"] } | null>(null);
  const generation = useRef(0);
  const ownedGoals = goals.filter(g => g.status === "active" && g.revision && g.owner_session_id === ownerSessionId && g.ownership_access !== "recovered_read_only");
  const goal = ownedGoals.find(g => g.id === goalId);
  const owned = Boolean(scope && (!task || (task.owner_principal_id === ownerPrincipalId && task.owner_session_id === ownerSessionId && task.ownership_access !== "recovered_read_only")));
  async function refresh() {
    if (!task || !owned) return;
    const version = generation.current;
    setRead(null); setAck(false); setError(null);
    try {
      const value = validateGeneralTaskPlan(await generalTaskRequest(`/tasks/${encodeURIComponent(task.task_id)}/plan`), task);
      if (version === generation.current) { setRead(value); setPlanDraft(JSON.stringify(value.plan.steps, null, 2)); }
    } catch (e) { if (version === generation.current) setError((e as Error).message); }
  }
  useEffect(() => {
    ++generation.current; setBusy(false); setRead(null); setAck(false); setError(null); setPendingEdit(null); setPlanDraft("");
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
    } catch (e) { if (version === generation.current) setError((e as Error).message); }
    finally { if (version === generation.current) setBusy(false); }
  }
  async function accept() {
    if (!task || !read || read.accepted || !ack || !owned || busy || pendingEdit || planDraft !== JSON.stringify(read.plan.steps, null, 2)) return;
    const version = generation.current; setBusy(true); setError(null); setAck(false);
    try {
      await generalTaskRequest(`/tasks/${encodeURIComponent(task.task_id)}/actions`, { action: "promote", expected_revision: read.task_revision });
      if (version === generation.current) { setRead(null); await onChanged?.(); }
    } catch (e) { if (version === generation.current) { setRead(null); setError((e as Error).message); } }
    finally { if (version === generation.current) setBusy(false); }
  }
  async function savePlan() {
    if (!task || !read || read.accepted || !owned || busy) return;
    const version = generation.current;
    let request = pendingEdit;
    if (!request) {
      try {
        const steps: unknown = JSON.parse(planDraft);
        if (!Array.isArray(steps) || !steps.length || steps.length > read.task_input.limits.max_steps) throw Error("Plan must contain a bounded list of steps.");
        request = { expected_revision: read.task_revision, expected_plan_revision: read.plan.revision,
          idempotency_key: crypto.randomUUID(), plan: { ...read.plan, revision: read.plan.revision + 1, steps: steps as GeneralTaskPlanRead["plan"]["steps"] } };
      } catch (e) { setError((e as Error).message); return; }
      setPendingEdit(request);
    }
    setBusy(true); setAck(false); setError(null);
    try {
      const result = await generalTaskRequest(`/tasks/${encodeURIComponent(task.task_id)}/plan`, request);
      if (version !== generation.current) return;
      if (!result || typeof result !== "object" || !("task" in result)) throw Error("Plan edit receipt is unconfirmed. Reconcile the exact edit before acceptance.");
      setPendingEdit(null); setRead(null); await onChanged?.();
    } catch (e) { if (version === generation.current) setError((e as Error).message); }
    finally { if (version === generation.current) setBusy(false); }
  }
  return <section className="rounded border border-white/15 bg-slate-950 p-4 text-slate-100" aria-label={task ? "Ordinary task plan" : "Describe a task"}>
    <div className="flex justify-between gap-2"><h2 className="font-semibold">{task ? "Review ordinary task plan" : "Describe a task"}</h2>{onClose && <button type="button" disabled={busy || Boolean(pending)} onClick={onClose}>Close</button>}</div>
    {!owned && <p role="status">Blocked: current task ownership is required. Recover through Work before changing the task.</p>}
    {error && <p role="alert" className="text-amber-200">{error}</p>}
    {!task && <>
      <p className="text-xs">Describe the outcome in ordinary language. Seraph proposes a persisted typed plan from registered tools; creation does not accept or execute it.</p>
      {pending && <p role="status">An exact request has an unconfirmed receipt. Retry with the same owner, input and idempotency key; inspect Work before starting another task.</p>}
      <fieldset disabled={busy || Boolean(pending) || !owned} className="grid gap-3 mt-3">
        <label>Task Goal<select aria-label="Task Goal" value={goalId} onChange={e => setGoalId(e.target.value)}><option value="">Choose active Goal</option>{ownedGoals.map(g => <option key={g.id} value={g.id}>{g.title} · revision {g.revision}</option>)}</select></label>
        <label>What should Seraph do?<textarea aria-label="What should Seraph do?" className="cockpit-input w-full" rows={4} maxLength={8192} value={intent} onChange={e => setIntent(e.target.value)} /></label>
        <label>Inference budget in micro USD<input aria-label="Inference budget in micro USD" type="number" min={0} step={1} value={cost} onChange={e => setCost(e.target.value)} /></label>
        <label><input type="checkbox" checked={egress} onChange={e => setEgress(e.target.checked)} />I acknowledge sending this intent and permitted evidence through the governed inference route. Current consent and capability gates still apply.</label>
        <p className="text-xs">Bounds: 16 steps, 12 inference calls, 900 seconds, depth 0, 2 outstanding children. Output: a JSON object. Zero budget leaves inference-dependent planning blocked.</p>
      </fieldset>
      <button type="button" className="cockpit-feedback-button mt-3" disabled={busy || !owned || (!pending && (!goal || !intent.trim()))} onClick={() => void create()}>{busy ? "Preparing plan…" : pending ? "Retry exact task request" : "Prepare task plan"}</button>
    </>}
    {task && <>
      <p role="status">{task.status} · {read?.accepted ? "plan accepted" : "plan awaiting review"} · {task.block_reason ?? "no active block"}</p>
      <button type="button" disabled={busy || !owned || Boolean(pendingEdit)} onClick={() => void refresh()}>Refresh current task plan</button>
      {read && <>
        <p className="text-xs">Task revision {read.task_revision} · plan revision {read.plan.revision} · no_learning. Acceptance grants no new permission; the dispatcher owns admission and each effect still requires its current approval.</p>
        <p className="whitespace-pre-wrap">{read.task_input.intent}</p>
        <pre aria-label="Task limits" className="whitespace-pre-wrap text-xs">{JSON.stringify(read.task_input.limits, null, 2)}</pre>
        {read.plan.steps.map(step => {
          const descriptor = read.descriptors.find(d => d.tool_id === step.tool_id)!;
          return <section key={step.step_id} className="mt-3 rounded border border-white/10 p-2" aria-label={`Plan step ${step.step_id}`}>
            <h3>{step.step_id} · {step.tool_id} v{descriptor.version}</h3>
            <p>Depends on: {step.depends_on.join(", ") || "none"}</p>
            <p>Effects: {descriptor.effects.join(", ")} · permissions: {descriptor.permissions.join(", ")}</p>
            <p>Deadline: {descriptor.deadline}s · verifier: {descriptor.verifier}</p>
            <details><summary>Typed input and output contract</summary><pre className="whitespace-pre-wrap break-all text-xs">{JSON.stringify({ input: step.input, input_schema: descriptor.input_schema, output_contract: step.output_contract, registered_output_schema: descriptor.output_schema }, null, 2)}</pre></details>
          </section>;
        })}
        {!read.accepted && <>
          <details className="mt-3"><summary>Edit typed plan steps</summary><p className="text-xs">Edit only registered tool IDs, typed inputs, dependencies and output contracts shown above. Saving creates a new inert revision; permissions and limits stay server owned.</p>
            <label>Typed plan steps<textarea aria-label="Typed plan steps" className="cockpit-input w-full font-mono text-xs" rows={12} maxLength={65536} value={planDraft} disabled={busy || !owned || Boolean(pendingEdit)} onChange={e => { setPlanDraft(e.target.value); setAck(false); }} /></label>
            <button type="button" disabled={busy || !owned || (!pendingEdit && planDraft === JSON.stringify(read.plan.steps, null, 2))} onClick={() => void savePlan()}>{pendingEdit ? "Reconcile exact plan edit" : "Save inert plan revision"}</button>
          </details>
          {pendingEdit && <p role="status">The exact edit has an unconfirmed receipt. Reconcile it before refreshing or accepting.</p>}
          <label className="mt-3 flex gap-2"><input type="checkbox" checked={ack} disabled={busy || !owned || Boolean(pendingEdit) || planDraft !== JSON.stringify(read.plan.steps, null, 2)} onChange={e => setAck(e.target.checked)} />I reviewed this exact plan, effects, permissions and limits.</label><button type="button" className="cockpit-feedback-button" disabled={busy || !owned || !ack || Boolean(pendingEdit) || planDraft !== JSON.stringify(read.plan.steps, null, 2)} onClick={() => void accept()}>Accept reviewed task plan</button>
        </>}
      </>}
      <p className="mt-2 text-xs">Input, approval, artifact and recovery receipts remain on this Work card. If a revision or authority changes, refresh and review again; uncertain effects must be reconciled before retry.</p>
    </>}
  </section>;
}
