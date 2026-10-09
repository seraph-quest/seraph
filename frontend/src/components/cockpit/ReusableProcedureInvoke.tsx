import { useEffect, useRef, useState } from "react";
import type { GoalInfo } from "../../types";
import { object } from "../../lib/researchMethods";
import { parameterValue, procedureRequest, type ProcedureParameter } from "../../lib/reusableProcedures";
import { createGuardianUuid } from "../../lib/guardianInbox";
interface Props { proposalId: string; version: string; digest: string; pointerRevision: number; parameters: ProcedureParameter[]; goals: GoalInfo[]; scopeGoalId: string; scopeGoalRevision: number; ownerSessionId: string; onCreated: (id: string) => Promise<void>; onStale: () => void }
// Keep an unresolved publication key through explicit reinspection. This is
// session-scoped volatile recovery, never browser storage or execution authority.
const pendingInvocations = new Map<string, { signature: string; key: string }>();
export function ReusableProcedureInvoke({ proposalId, version, digest, pointerRevision, parameters, goals, scopeGoalId, scopeGoalRevision, ownerSessionId, onCreated, onStale }: Props) {
  const [values, setValues] = useState<Record<string, string>>({}), [goalId, setGoalId] = useState("");
  const [cost, setCost] = useState("0"), [ack, setAck] = useState(false), [egress, setEgress] = useState(false);
  const [busy, setBusy] = useState(false), [error, setError] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const locked = useRef(false);
  const invocationScope = `${ownerSessionId}:${proposalId}:${version}:${pointerRevision}`;
  const availableGoals = goals.filter(g => g.id === scopeGoalId && g.revision === scopeGoalRevision && g.status === "active" && g.owner_session_id === ownerSessionId && g.ownership_access !== "recovered_read_only");
  async function invoke() {
    if (!ack || locked.current || stale) return;
    locked.current = true; setBusy(true); setError(null);
    let dispatched = false;
    try {
      const goal = availableGoals.find(g => g.id === goalId);
      if (!goal || !goal.revision || !/^\d+$/.test(cost) || !Number.isSafeInteger(Number(cost))) throw Error("Select a current owned Goal and finite nonnegative budget.");
      const supplied = Object.fromEntries(parameters.map(p => {
        if (!Object.prototype.hasOwnProperty.call(values, p.name)) throw Error(`Supply a fresh value for ${p.name}.`);
        return [p.name, parameterValue(p, values[p.name])];
      }));
      const body = { version, digest, expected_pointer_revision: pointerRevision, goal_id: goal.id, goal_revision: goal.revision, parameters: supplied,
        limits: { max_steps: 16, max_inference_calls: 12, wall_seconds: 900, depth: 0, max_outstanding_children: 2, max_cost_microusd: Number(cost) }, inference_egress_acknowledged: egress };
      const signature = JSON.stringify(body);
      let pending = pendingInvocations.get(invocationScope);
      if (pending?.signature !== signature) {
        pending = { signature, key: createGuardianUuid() };
        pendingInvocations.set(invocationScope, pending);
      }
      dispatched = true;
      const result = await procedureRequest(`/api/memory/task-methods/${encodeURIComponent(proposalId)}/invoke`, { ...body, idempotency_key: pending.key });
      if (!object(result) || typeof result.task_id !== "string" || typeof result.idempotent_replay !== "boolean") throw Error("New Task receipt is unconfirmed. Inspect Work before retrying.");
      pendingInvocations.delete(invocationScope);
      if (alive.current) await onCreated(result.task_id);
    } catch (e) { if (alive.current) { setError((e as Error).message); if (dispatched) { setStale(true); onStale(); } } }
    finally { locked.current = false; if (alive.current) setBusy(false); }
  }
  return <section aria-label="Invoke current reusable method">
    <h4>Invoke exact current method</h4>
    <p>New Task, Root, approvals, deadline and budget. No old grants are reused. Fresh values have no defaults. Review and promote the new triage Task before execution.</p>
    <label>Invocation Goal<select value={goalId} disabled={busy} onChange={e => setGoalId(e.target.value)}><option value="">Choose current owned Goal</option>{availableGoals.map(g => <option key={g.id} value={g.id}>{g.title} · revision {g.revision}</option>)}</select></label>
    {parameters.map(p => <label key={p.name} className="block">Fresh parameter {p.name} · {p.step_id}{p.input_pointer} · schema {JSON.stringify(p.schema)}
      {p.schema.type === "boolean" || p.schema.type === "null" ? <select aria-label={`Fresh parameter ${p.name}`} value={values[p.name] ?? ""} disabled={busy} onChange={e => setValues({ ...values, [p.name]: e.target.value })}><option value="">Choose explicit value</option>{(p.schema.type === "boolean" ? ["true", "false"] : ["null"]).map(v => <option key={v} value={v}>{v}</option>)}</select>
        : <input aria-label={`Fresh parameter ${p.name}`} disabled={busy} value={values[p.name] ?? ""} onChange={e => setValues({ ...values, [p.name]: e.target.value })} />}
    </label>)}
    <label>Fresh inference budget in micro USD<input value={cost} disabled={busy} onChange={e => setCost(e.target.value)} /></label>
    <p>Bounds: 16 steps, 12 inference calls, 900 seconds, depth 0, 2 outstanding children.</p>
    <label><input type="checkbox" checked={egress} disabled={busy} onChange={e => setEgress(e.target.checked)} />Acknowledge inference egress under current governed policy.</label>
    <label><input type="checkbox" checked={ack} disabled={busy} onChange={e => setAck(e.target.checked)} />Create a fresh bounded Task with these exact values and current pin.</label>
    <button disabled={busy || !ack || stale} onClick={() => void invoke()}>Create Task from current method</button>
    {error && <p role="alert">{error}</p>}
  </section>;
}
