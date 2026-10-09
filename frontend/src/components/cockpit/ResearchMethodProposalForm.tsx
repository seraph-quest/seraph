import { useEffect, useRef, useState } from "react";
import type { WorkBoardTask } from "../../types";
import { EVIDENCE_FIELDS, SOURCE_PREFERENCES, ResearchMethodError, object, identifier, researchMethodRequest, validateProvenance, validateResearchSource, validateResearchStrategy } from "../../lib/researchMethods";
import type { ResearchMethodRequest, ResearchSource, ResearchStrategy } from "../../lib/researchMethods";

interface Props { task: WorkBoardTask; source: ResearchSource; owned: boolean; onCandidate: (value: unknown) => void; onStale: () => void }
const empty = (): ResearchStrategy => ({ schema_version: "ResearchStrategy.v1", query_templates: [], source_preferences: [], required_evidence_fields: [], draft_sections: [], stop_conditions: [""] });
export function ResearchMethodProposalForm(props: Props) {
  return <OwnedForm key={`${props.task.task_id}:${props.task.task_revision}:${props.task.owner_session_id}:${props.owned}:${props.source.attempt_id}`} {...props} />;
}
function OwnedForm({ task, source, owned, onCandidate, onStale }: Props) {
  const [strategy, setStrategy] = useState<ResearchStrategy>(empty), [busy, setBusy] = useState(false), [error, setError] = useState<string | null>(null);
  const [ack, setAck] = useState(false), [uncertain, setUncertain] = useState(false), [createdId, setCreatedId] = useState<string | null>(null);
  const alive = useRef(true), locked = useRef(false), abort = useRef(new AbortController()), pending = useRef<ResearchMethodRequest | null>(null);
  const candidateId = useRef<string | null>(null);
  useEffect(() => { alive.current = true; return () => { alive.current = false; abort.current.abort(); }; }, []);
  async function prepare() {
    if (!owned || locked.current || !alive.current || (!pending.current && !ack)) return;
    locked.current = true; setBusy(true); setError(null);
    try {
      validateResearchSource(source, task);
      if (!pending.current) {
        validateResearchStrategy(strategy);
        pending.current = { task_id: source.task_id, attempt_id: source.attempt_id, source_refs: source.source_refs, scope: source.scope, expected_revision: source.expected_revision, strategy };
      }
      let id = candidateId.current;
      if (!id) {
        const receipt = await researchMethodRequest("/research-methods", pending.current, abort.current.signal);
        if (!object(receipt) || !identifier(receipt.proposal_id) || receipt.task_id !== source.task_id || receipt.attempt_id !== source.attempt_id || receipt.result !== "candidate_inert") throw Error("Research candidate receipt is unconfirmed. Reconcile this exact request before preparing another candidate.");
        id = receipt.proposal_id;
        if (!alive.current) return;
        candidateId.current = id;
        setCreatedId(id);
      }
      const candidate = await researchMethodRequest(`/task-lessons/${encodeURIComponent(id)}`, undefined, abort.current.signal);
      validateProvenance(candidate, task, source.attempt_id, source.source_refs);
      if (!object(candidate) || candidate.proposal_id !== id || candidate.schema_version !== "task_method_proposal.v1" || candidate.source_current !== true || candidate.behavior_changed !== false || candidate.result !== "candidate_inert"
        || !object(candidate.scope) || candidate.scope.goal_id !== source.scope.goal_id || candidate.scope.goal_revision !== source.scope.goal_revision || candidate.scope.family !== "research") throw Error("Prepared research candidate or current source is unconfirmed. Inspect this candidate before another proposal.");
      validateResearchStrategy(candidate.new_method);
      const candidateStrategy = candidate.new_method;
      if (!pending.current || (["query_templates", "source_preferences", "required_evidence_fields", "draft_sections", "stop_conditions"] as const).some(k => JSON.stringify(candidateStrategy[k]) !== JSON.stringify(pending.current!.strategy[k]))) throw Error("Prepared strategy differs from the exact reviewed fields. Inspect the existing candidate before another proposal.");
      if (!alive.current) return;
      pending.current = null; setUncertain(false); setAck(false); onCandidate(candidate);
    } catch (e) { if (alive.current) {
      if (e instanceof ResearchMethodError && [400, 401, 403, 409, 422].includes(e.status) && !candidateId.current) {
        pending.current = null; setUncertain(false); setAck(false);
        if ([401, 403, 409].includes(e.status)) onStale();
      } else setUncertain(pending.current !== null);
      setError((e as Error).message);
    } } finally { locked.current = false; if (alive.current) setBusy(false); }
  }
  function change(next: ResearchStrategy) { setStrategy(next); setAck(false); }
  return <section aria-label="Structured research method proposal" className="grid gap-2 mt-3">
    <h4>Prepare a research strategy</h4><p>Use this completed dossier's verified source references. Structured fields remain private and inert; adoption requires a separate canonical review. No model proposal or provider contact.</p>
    {error && <p role="alert">{error}</p>}{busy && <p role="status">Waiting for the authenticated candidate service…</p>}
    <fieldset disabled={busy || !owned || uncertain || createdId !== null} className="grid gap-2">
      {([ ["query_templates", "Query template", 3], ["draft_sections", "Draft section", 16], ["stop_conditions", "Stop condition", 8] ] as const).map(([field, label, cap]) => <fieldset key={field}><legend>{label}s · at most {cap}</legend>
        {strategy[field].map((text, i) => <div key={i}><label>{label} {i + 1}<textarea aria-label={`${label} ${i + 1}`} className="cockpit-input w-full" maxLength={2000} rows={2} value={text} onChange={e => change({ ...strategy, [field]: strategy[field].map((s, n) => n === i ? e.target.value : s) })} /></label><span>{[...text].length}/1000 characters</span><button type="button" disabled={field === "stop_conditions" && strategy[field].length === 1} onClick={() => change({ ...strategy, [field]: strategy[field].filter((_, n) => n !== i) })}>Remove {label.toLowerCase()} {i + 1}</button></div>)}
        <button type="button" disabled={strategy[field].length >= cap} onClick={() => change({ ...strategy, [field]: [...strategy[field], ""] })}>Add {label.toLowerCase()}</button>
      </fieldset>)}
      <fieldset><legend>Source preferences · at most 5</legend>{SOURCE_PREFERENCES.map(v => <label key={v}><input type="checkbox" checked={strategy.source_preferences.includes(v)} onChange={e => change({ ...strategy, source_preferences: e.target.checked ? [...strategy.source_preferences, v] : strategy.source_preferences.filter(s => s !== v) })} />{v}</label>)}</fieldset>
      <fieldset><legend>Required evidence fields · at most 6</legend>{EVIDENCE_FIELDS.map(v => <label key={v}><input type="checkbox" checked={strategy.required_evidence_fields.includes(v)} onChange={e => change({ ...strategy, required_evidence_fields: e.target.checked ? [...strategy.required_evidence_fields, v] : strategy.required_evidence_fields.filter(s => s !== v) })} />{v}</label>)}</fieldset>
      <label><input type="checkbox" checked={ack} onChange={e => setAck(e.target.checked)} />I reviewed these exact structured fields against the verified dossier evidence.</label>
    </fieldset>
    {uncertain && <p role="status">The request or private readback is unconfirmed. Fields are frozen; explicitly reconcile the same request or inspect the known candidate. No automatic retry.</p>}
    <button type="button" disabled={busy || !owned || (!pending.current && !ack)} onClick={() => void prepare()}>{createdId ? "Inspect prepared research candidate" : uncertain ? "Reconcile exact research request" : "Prepare private research strategy"}</button>
  </section>;
}
