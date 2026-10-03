import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import { newResearchCreation, readResearchPending, readResearchReport, readResearchState,
  researchStorageKey, submitResearchControl, submitResearchCreation, validateResearchInput } from "../../lib/researchDossier";
import type { ResearchPending, ResearchSource, ResearchState } from "../../lib/researchDossier";
import type { GoalInfo, WorkBoardTask } from "../../types";

interface Props {
  ownerPrincipalId?: string | null; ownerSessionId?: string | null;
  task?: WorkBoardTask; goals?: GoalInfo[]; onClose?: () => void;
  onCreated?: (task: WorkBoardTask) => void | Promise<void>;
  onChanged?: () => void | Promise<void>;
}
const sourceDraft = (): ResearchSource => ({ kind: "public_https_text", url: "", first_line: 1, last_line: 1 });

export function ResearchDossierPanel({ ownerPrincipalId, ownerSessionId, task, goals = [], onClose, onCreated, onChanged }: Props) {
  const [pending, setPending] = useState<ResearchPending | null>(null);
  const [state, setState] = useState<ResearchState | null>(null);
  const [report, setReport] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [storageError, setStorageError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [goalId, setGoalId] = useState("");
  const [title, setTitle] = useState("Evidence research dossier");
  const [question, setQuestion] = useState("");
  const [perspectives, setPerspectives] = useState([{ instruction: "", sourceSlots: "0" }]);
  const [sources, setSources] = useState<ResearchSource[]>([sourceDraft()]);
  const [sourceAcknowledged, setSourceAcknowledged] = useState(false);
  const [limitAcknowledged, setLimitAcknowledged] = useState(false);
  const [limit, setLimit] = useState<number | null>(null);
  const controller = useRef<AbortController | null>(null);
  const generation = useRef(0);
  const goal = goals.find((item) => item.id === goalId && item.ownership_access !== "recovered_read_only");
  const readOnly = task?.ownership_access === "recovered_read_only";
  let storageKey: string | null = null;
  try {
    if (ownerPrincipalId && ownerSessionId) storageKey = researchStorageKey(ownerPrincipalId, ownerSessionId, task?.task_id ?? "create");
  } catch { /* The effect reports this unavailable owner boundary. */ }

  useEffect(() => {
    const version = ++generation.current;
    controller.current?.abort(); controller.current = null;
    setBusy(false);
    setState(null); setReport(null); setPending(null); setError(null); setStorageError(null);
    if (!storageKey) { setStorageError("The current owner and session are required for research."); return; }
    try { setPending(readResearchPending(storageKey)); }
    catch { setStorageError("Retained research request is corrupt or unavailable. No mutation can be sent."); }
    const abort = new AbortController();
    if (task) void readResearchState(task.task_id, abort.signal).then((result) => {
      if (generation.current === version) setState(result);
    }).catch(() => { if (!abort.signal.aborted && generation.current === version) setError("Research has not reached a readable native admission. Refresh after checking the Board block reason."); });
    return () => { ++generation.current; abort.abort(); controller.current?.abort(); };
  }, [storageKey, task?.task_id]);

  useEffect(() => {
    setLimit(null); setLimitAcknowledged(false);
    if (task || !goal || !goal.revision) return;
    const abort = new AbortController();
    void apiFetch(`${API_URL}/api/work-board/goals/${encodeURIComponent(goal.id)}/execution-limits?goal_revision=${goal.revision}`, { signal: abort.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error("Goal limit unavailable");
        const result = await response.json();
        if (!Number.isSafeInteger(result.effective_max_runtime_seconds) || result.effective_max_runtime_seconds < 1) throw new Error("Goal limit incomplete");
        if (!abort.signal.aborted) setLimit(Math.min(300, result.effective_max_runtime_seconds));
      }).catch(() => { if (!abort.signal.aborted) setError("Read the current Goal budget before creating research."); });
    return () => abort.abort();
  }, [goal?.id, goal?.revision, task?.task_id]);

  const refresh = async () => {
    if (!task) return;
    const version = generation.current;
    try { const result = await readResearchState(task.task_id); if (version === generation.current) { setState(result); setError(null); } }
    catch { if (version === generation.current) setError("Current research state is unavailable; check its Board admission or block reason."); }
  };

  const submit = async (operation: ResearchPending) => {
    if (!storageKey || readOnly || storageError || busy) return;
    const version = generation.current;
    const abort = new AbortController(); controller.current = abort;
    setBusy(true); setError(null); setPending(operation);
    try {
      if (operation.kind === "create") {
        const created = await submitResearchCreation(storageKey, operation, abort.signal);
        if (version === generation.current) { setPending(null); await onCreated?.(created); }
      } else {
        const current = await submitResearchControl(storageKey, operation, abort.signal);
        if (version === generation.current) {
          setState(current); setPending(readResearchPending(storageKey)); await onChanged?.();
        }
      }
    } catch (reason) {
      if (version === generation.current) {
        setError(reason instanceof Error ? reason.message : "Research response is uncertain. Retry the exact retained request.");
        try {
          const retained = readResearchPending(storageKey);
          setPending(retained);
          if (retained === null) setStorageError("The exact research request could not be retained. No further mutation can be sent.");
        }
        catch { setStorageError("Research request retention is unavailable. No further mutation can be sent."); }
      }
    } finally { if (version === generation.current) setBusy(false); }
  };

  const create = (event: FormEvent) => {
    event.preventDefault();
    if (pending) { void submit(pending); return; }
    if (!goal?.revision || !sourceAcknowledged || !limitAcknowledged || limit === null) return;
    try {
      const input = validateResearchInput({ schema_version: 1, question, sources,
        perspectives: perspectives.map((item) => ({ instruction: item.instruction, source_slots: item.sourceSlots.split(",").map((slot) => Number(slot.trim())) })),
        source_egress_acknowledged: true, no_learning: true });
      void submit(newResearchCreation(goal.id, goal.revision, title, input));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Research input is invalid."); }
  };

  const control = (action: "recover" | "cancel") => {
    if (!task || !state || pending) return;
    void submit({ kind: "control", task_id: task.task_id, action,
      body: { expected_revision: state.task_revision, idempotency_key: `research-${action}:${crypto.randomUUID()}` } });
  };

  if (task) return <section className="mt-3 rounded border border-white/10 p-3" aria-label="Research dossier inspector">
    <h3 className="font-semibold">Research dossier</h3>
    <p className="text-xs opacity-75">Attributed synthesis with mechanical citation checks. Semantic truth remains unverified. Memory: no_learning.</p>
    {state && <>
      <p className="mt-2 text-sm">{state.status} · {state.phase ?? "terminal or executing"} · original deadline {state.deadline_at}</p>
      <p className="break-all text-xs">Parent {state.parent_id} · attempt {state.attempt_id}</p>
      <ul className="my-2 text-xs">{state.children.map((child) => <li key={child.job_id} className="break-all">{child.job_id}: {child.status}{child.reason ? ` · ${child.reason}` : ""}{child.lease_present ? " · worker lease present" : ""}</li>)}</ul>
      <ul className="my-2 text-xs">{state.costs.map((cost) => <li key={cost.operation_id}>{cost.state} · reserved {cost.bound_microusd} microUSD · actual {cost.actual_cost_microusd ?? "unresolved"}{cost.reason ? ` · ${cost.reason}` : ""}</li>)}</ul>
      <p className="text-xs">{state.recovery_limit}</p>
      <p className="text-xs">Unknown contact cost remains held. Use Settings → Model Fabric → Accounting for explicit cost evidence recovery.</p>
    </>}
    <div className="mt-2 flex flex-wrap gap-2">
      <button type="button" className="cockpit-feedback-button" onClick={() => void refresh()} disabled={busy}>Refresh research</button>
      <button type="button" className="cockpit-feedback-button" onClick={() => control("recover")} disabled={busy || readOnly || Boolean(pending || storageError) || !state?.recoverable}>Recover original research</button>
      <button type="button" className="cockpit-feedback-button" onClick={() => control("cancel")} disabled={busy || readOnly || Boolean(pending || storageError) || !state?.cancel_available}>Cancel original research</button>
      {pending?.kind === "control" && <button type="button" className="cockpit-feedback-button" onClick={() => void submit(pending)} disabled={busy || readOnly || Boolean(storageError)}>Retry exact retained {pending.action}</button>}
      <button type="button" className="cockpit-feedback-button" disabled={busy || !state?.report_available} onClick={() => {
        const version = generation.current;
        void readResearchReport(task.task_id).then((text) => { if (version === generation.current) setReport(text); }).catch(() => { if (version === generation.current) setError("The actual verified plain-text dossier is unavailable."); });
      }}>Read verified dossier</button>
    </div>
    {(error || storageError) && <p role="alert" className="mt-2 text-xs text-amber-200">{storageError ?? error}</p>}
    {report !== null && <pre className="mt-3 whitespace-pre-wrap break-words text-xs" aria-label="Literal research dossier">{report}</pre>}
  </section>;

  return <div className="fixed inset-0 z-[220] flex items-center justify-center bg-black/70 p-4" role="dialog" aria-modal="true" aria-label="Create finite research dossier">
    <form className="max-h-[90vh] w-full max-w-3xl overflow-y-auto rounded border border-white/20 bg-slate-950 p-4" onSubmit={create}>
      <h2 className="font-semibold">Create finite research dossier</h2>
      <p className="my-2 text-xs">One parent, at most two research perspectives and four explicit sources. One provider contact per perspective; parent synthesis is deterministic. Original deadline at most 300 seconds. No tools, account access or learning.</p>
      <p className="my-2 text-xs">Existing governed OpenRouter consent, capability proof, budget and serial priority gates apply. Source acknowledgement below does not approve model egress; configure that separately in Settings → Model Fabric.</p>
      <fieldset disabled={busy || Boolean(pending || storageError)} className="grid gap-3">
        <label className="text-xs">Current Goal<select aria-label="Research Goal" className="cockpit-input mt-1 w-full" value={goalId} onChange={(event) => setGoalId(event.target.value)}><option value="">Choose Goal</option>{goals.filter((item) => item.ownership_access !== "recovered_read_only" && item.status === "active" && item.revision).map((item) => <option key={item.id} value={item.id}>{item.title} · revision {item.revision}</option>)}</select></label>
        <label className="text-xs">Title<input className="cockpit-input mt-1 w-full" value={title} onChange={(event) => setTitle(event.target.value)} maxLength={100} /></label>
        <label className="text-xs">Research question · at most 2 KiB<textarea aria-label="Research question" className="cockpit-input mt-1 w-full" value={question} onChange={(event) => setQuestion(event.target.value)} maxLength={2048} /></label>
        {sources.map((source, index) => <fieldset key={index} className="rounded border border-white/10 p-2">
          <legend className="text-xs">Source slot {index}</legend>
          <select aria-label={`Research source ${index} kind`} className="cockpit-input w-full" value={source.kind} onChange={(event) => setSources(sources.map((item, slot) => slot !== index ? item : event.target.value === "public_https_text" ? sourceDraft() : { kind: "completed_board_artifact", producer_task_ref: "", producer_attempt_ref: "", source_sha256: "", first_line: 1, last_line: 1 }))}>
            <option value="public_https_text">Explicit public HTTPS UTF-8 text</option><option value="completed_board_artifact">Verified completed Board artifact</option>
          </select>
          {source.kind === "public_https_text" ? <label className="text-xs">Exact HTTPS URL<input aria-label={`Research source ${index} URL`} className="cockpit-input my-1 w-full" value={source.url} onChange={(event) => setSources(sources.map((item, slot) => slot === index ? { ...source, url: event.target.value } : item))} maxLength={2048} /></label> : (["producer_task_ref", "producer_attempt_ref", "source_sha256"] as const).map((field) => <label className="block text-xs" key={field}>{field}<input className="cockpit-input my-1 w-full" value={source[field]} onChange={(event) => setSources(sources.map((item, slot) => slot === index ? { ...source, [field]: event.target.value } : item))} maxLength={128} /></label>)}
          <div className="flex gap-2">{(["first_line", "last_line"] as const).map((field) => <label className="text-xs" key={field}>{field}<input aria-label={`Research source ${index} ${field}`} type="number" min={1} max={65536} className="cockpit-input w-full" value={source[field]} onChange={(event) => setSources(sources.map((item, slot) => slot === index ? { ...source, [field]: Number(event.target.value) } : item))} /></label>)}</div>
        </fieldset>)}
        {sources.length < 4 && <button type="button" className="cockpit-feedback-button" onClick={() => setSources([...sources, sourceDraft()])}>Add explicit source</button>}
        {perspectives.map((perspective, index) => <fieldset className="rounded border border-white/10 p-2" key={index}>
          <legend className="text-xs">Perspective {index+1}</legend>
          <label className="text-xs">Instruction · at most 1 KiB<textarea aria-label={`Research perspective ${index+1}`} className="cockpit-input my-1 w-full" value={perspective.instruction} maxLength={1024} onChange={(event) => setPerspectives(perspectives.map((item, slot) => slot === index ? { ...item, instruction: event.target.value } : item))} /></label>
          <label className="text-xs">Source slots · one or two comma-separated indices<input aria-label={`Perspective ${index+1} source slots`} className="cockpit-input mt-1 w-full" value={perspective.sourceSlots} maxLength={5} onChange={(event) => setPerspectives(perspectives.map((item, slot) => slot === index ? { ...item, sourceSlots: event.target.value } : item))} /></label>
        </fieldset>)}
        {perspectives.length < 2 && <button type="button" className="cockpit-feedback-button" onClick={() => setPerspectives([...perspectives, { instruction: "", sourceSlots: "0" }])}>Add second perspective</button>}
        <label className="flex gap-2 text-xs"><input type="checkbox" checked={sourceAcknowledged} onChange={(event) => setSourceAcknowledged(event.target.checked)} />I selected these sources and acknowledge quoted source text will go to the separately approved model route.</label>
        <label className="flex gap-2 text-xs"><input type="checkbox" checked={limitAcknowledged} onChange={(event) => setLimitAcknowledged(event.target.checked)} disabled={limit === null} />I acknowledge the current Goal runtime ceiling {limit ?? "(checking)"} seconds, one synthesis per perspective, existing trusted cost allowance, and retained Unknown or actual overrun liability.</label>
      </fieldset>
      {(error || storageError) && <p role="alert" className="my-2 text-xs text-amber-200">{storageError ?? error}</p>}
      {pending && <p className="my-2 text-xs">A retained request is awaiting exact readback. Edits are locked until its receipt is confirmed.</p>}
      <div className="mt-3 flex justify-end gap-2"><button type="button" className="cockpit-feedback-button" onClick={onClose}>Close</button><button type="submit" className="cockpit-feedback-button" disabled={busy || Boolean(storageError) || (!pending && (!goal || !question || !sourceAcknowledged || !limitAcknowledged || limit === null))}>{busy ? "Awaiting research receipt…" : pending ? "Retry exact retained creation" : "Create bounded research"}</button></div>
    </form>
  </div>;
}
