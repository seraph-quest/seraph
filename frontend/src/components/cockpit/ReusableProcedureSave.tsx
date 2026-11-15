import { useRef, useState } from "react";
import type { GoalInfo, WorkBoardTask } from "../../types";
import { object } from "../../lib/researchMethods";
import { procedureRequest, readProcedureSource, type ProcedureSource } from "../../lib/reusableProcedures";
import { createGuardianUuid } from "../../lib/guardianInbox";
import { TaskMethodReview } from "./TaskMethodReview";

interface Props { task: WorkBoardTask; ownerPrincipalId?: string | null; ownerSessionId?: string | null; goals: GoalInfo[]; onCreated: (id: string) => Promise<void> }
export function ReusableProcedureSave(props: Props) {
  return <OwnedSave key={`${props.task.task_id}:${props.task.task_revision}:${props.ownerPrincipalId}:${props.ownerSessionId}`} {...props} />;
}
function OwnedSave({ task, ownerPrincipalId, ownerSessionId, goals, onCreated }: Props) {
  const [source, setSource] = useState<ProcedureSource | null>(null), [names, setNames] = useState<Record<string, string>>({});
  const [proposalId, setProposalId] = useState<string | null>(null), [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false), [ack, setAck] = useState(false);
  const pending = useRef<{ signature: string; key: string } | null>(null);
  const locked = useRef(false);
  const owned = Boolean(ownerPrincipalId && ownerSessionId && task.owner_principal_id === ownerPrincipalId && task.owner_session_id === ownerSessionId && task.ownership_access !== "recovered_read_only");
  async function inspect() {
    if (!owned || locked.current) return;
    locked.current = true; setBusy(true); setError(null); setSource(null); setAck(false);
    try { setSource(readProcedureSource(await procedureRequest(`/api/work-board/tasks/${encodeURIComponent(task.task_id)}/save-method`), task)); }
    catch (e) { setError((e as Error).message); }
    finally { locked.current = false; setBusy(false); }
  }
  async function save() {
    if (!owned || locked.current || !source?.eligible || !source.source_attempt || !ack) return;
    locked.current = true; setBusy(true); setError(null);
    try {
      const selections = source.parameter_offers.filter(o => names[o.offer_id]?.trim()).map(o => ({ offer_id: o.offer_id, name: names[o.offer_id].trim() }));
      if (selections.some(s => !/^[A-Za-z0-9_-]{1,64}$/.test(s.name)) || new Set(selections.map(s => s.name)).size !== selections.length) throw Error("Use unique parameter names containing 1–64 letters, numbers, underscores or hyphens.");
      const request = { expected_revision: source.expected_revision, source_attempt: source.source_attempt, parameter_selections: selections };
      const signature = JSON.stringify(request);
      if (pending.current?.signature !== signature) pending.current = { signature, key: createGuardianUuid() };
      const result = await procedureRequest(`/api/work-board/tasks/${encodeURIComponent(task.task_id)}/save-method`, { ...request, idempotency_key: pending.current.key });
      if (!object(result) || typeof result.proposal_id !== "string" || result.task_id !== task.task_id || result.attempt_id !== source.source_attempt || result.result !== "candidate_inert" || result.behavior_changed !== false) throw Error("Immutable save receipt is unconfirmed. Inspect the source again.");
      setProposalId(result.proposal_id); setAck(false);
    } catch (e) { setError((e as Error).message); }
    finally { locked.current = false; setBusy(false); }
  }
  return <section aria-label="Save reusable general-task method" className="mt-3 rounded border border-white/10 p-3 text-xs">
    <h3>Save this task as a reusable method</h3>
    <p>The complete verified plan stays immutable. Choose only original producer-offered ordinary inputs. Saving prepares a private proposal; explicit adoption remains separate.</p>
    <button disabled={!owned || busy} onClick={() => void inspect()}>Inspect reusable source</button>
    {!owned && <p role="status">Current Task ownership is required. Recovery inspection cannot save a method.</p>}
    {error && <p role="alert">{error}</p>}
    {source && <><p role="status">{source.eligible ? "Eligible completed source" : `Source ineligible: ${source.reason_code}`}</p>
      <p>Task {source.task_id} · revision {source.expected_revision} · Attempt {source.source_attempt ?? "unavailable"}</p>
      <pre aria-label="Reusable source receipts">{JSON.stringify(source.source_receipt, null, 2)}</pre>
      {source.eligible && <>{source.parameter_offers.map(offer => <label key={offer.offer_id} className="block">
        Parameter name for {offer.step_id}{offer.input_pointer} (leave blank to retain the original ordinary input)
        <input disabled={busy} value={names[offer.offer_id] ?? ""} onChange={e => { setNames({ ...names, [offer.offer_id]: e.target.value }); setAck(false); }} maxLength={64} />
        <span className="block">Producer {offer.producer_id} · {offer.producer_contract_digest} · schema {JSON.stringify(offer.schema)}</span>
      </label>)}
      <label><input type="checkbox" checked={ack} disabled={busy} onChange={e => setAck(e.target.checked)} />Save this exact complete source and parameter selection as an immutable private proposal.</label>
      <button disabled={busy || !ack} onClick={() => void save()}>Save immutable method proposal</button></>}
    </>}
    {proposalId && <><p role="status">Proposal {proposalId} is inert pending review. Quality is unmeasured.</p><TaskMethodReview task={task} proposalId={proposalId} owned={owned} goals={goals} onCreated={onCreated} /></>}
  </section>;
}
