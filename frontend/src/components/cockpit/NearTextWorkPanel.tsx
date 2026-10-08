import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { NEAR_TEXT_DISCLOSURE, NEAR_TEXT_MODEL } from "../../lib/modelFabric";
import type { NearTextSetupStatus } from "../../lib/modelFabric";
import { createNearTextTask, NEAR_TEXT_CAPABILITY, nearTextGoalEligible, nearTextOutputEligible, readNearTextOutput, readNearTextSetup, validateNearQuestion } from "../../lib/nearText";
import type { NearTextOutput } from "../../lib/nearText";
import type { GoalInfo, WorkBoardTask } from "../../types";

interface Props {
  ownerPrincipalId?: string | null; ownerSessionId?: string | null;
  task?: WorkBoardTask; goals?: GoalInfo[]; onClose?: () => void;
  onCreated?: (task: WorkBoardTask) => void | Promise<void>;
  onOpenAccounting?: () => void; onOpenApprovals?: () => void;
}
export function NearTextWorkPanel({ ownerPrincipalId, ownerSessionId, task, goals = [], onClose, onCreated, onOpenAccounting, onOpenApprovals }: Props) {
  const scope = JSON.stringify([ownerPrincipalId, ownerSessionId, task?.task_id ?? "create", task?.task_revision,
    task?.latest_attempt?.attempt_id, task?.latest_attempt?.workflow_run_id, task?.latest_attempt?.outcome,
    task?.status, task?.readback_status, task?.verification_status, task?.typed_input_digest, task?.block_kind, task?.block_reason]);
  const [loadedScope, setLoadedScope] = useState<string | null>(null);
  const [setup, setSetup] = useState<NearTextSetupStatus | null>(null);
  const [goalId, setGoalId] = useState("");
  const [question, setQuestion] = useState("");
  const [maxTokens, setMaxTokens] = useState("256");
  const [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [output, setOutput] = useState<{ scope: string; value: NearTextOutput } | null>(null);
  const generation = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const owned = Boolean(ownerPrincipalId && ownerSessionId && (!task || (task.owner_principal_id === ownerPrincipalId && task.owner_session_id === ownerSessionId && task.ownership_access !== "recovered_read_only")));
  const readySetup = loadedScope === scope ? setup : null;
  const goal = goals.find(item => item.id === goalId && nearTextGoalEligible(item, ownerSessionId));
  const eligible = Boolean(owned && task && nearTextOutputEligible(task));
  const liability = Boolean(task && (task.block_kind === "cost_liability" || task.block_reason?.includes("cost_liability")
    || (task.status === "blocked" && task.block_reason === "near_cost_readback_required")));
  useEffect(() => {
    const version = ++generation.current;
    controller.current?.abort(); controller.current = null;
    setLoadedScope(scope); setSetup(null); setGoalId(""); setQuestion(""); setMaxTokens("256");
    setAcknowledged(false); setBusy(false); setUncertain(false); setError(null); setOutput(null);
    const abort = new AbortController();
    if (owned && !task) void readNearTextSetup(abort.signal).then(value => {
      if (version === generation.current) { setSetup(value); if (value) setMaxTokens(String(Math.min(256, value.max_output_tokens))); }
    }).catch(() => { if (version === generation.current && !abort.signal.aborted) setError("Current NEAR settings are unavailable. The question stays in this window; refresh settings before submitting."); });
    return () => { ++generation.current; abort.abort(); controller.current?.abort(); };
  }, [scope, owned, Boolean(task)]);
  async function refreshSettings() {
    const version = generation.current;
    try { const value = await readNearTextSetup(); if (version === generation.current) { setSetup(value); setError(null); } }
    catch { if (version === generation.current) setError("NEAR settings could not be refreshed. Your draft is retained; no question was sent."); }
  }
  async function submit() {
    if (!owned || !ownerPrincipalId || !ownerSessionId || !goal || !readySetup || readySetup.status !== "configured" || !readySetup.enabled || !readySetup.key_present || !readySetup.consent_current || busy || uncertain) return;
    try { validateNearQuestion(question, maxTokens, readySetup.max_output_tokens); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Question limits are invalid."); return; }
    if (!acknowledged) { setError("Acknowledge plaintext access for this question before submitting."); return; }
    const version = generation.current;
    const abort = new AbortController(); controller.current = abort;
    const timeout = window.setTimeout(() => abort.abort(), 30_000);
    setBusy(true); setError(null);
    try {
      const created = await createNearTextTask({ goal, question, maxOutputTokens: maxTokens, configuredCap: readySetup.max_output_tokens, ownerPrincipalId, ownerSessionId, signal: abort.signal });
      if (version !== generation.current) return;
      setQuestion(""); setAcknowledged(false); await onCreated?.(created);
    } catch {
      if (version === generation.current) {
        setUncertain(true); setQuestion(""); setAcknowledged(false);
        setError("The task request was not confirmed. Inspect Work and current Goal approval before creating another question. This window will not resend it.");
      }
    } finally { window.clearTimeout(timeout); if (version === generation.current) setBusy(false); }
  }
  async function readAnswer() {
    if (!eligible || !task || busy) return;
    const version = generation.current;
    const abort = new AbortController(); controller.current = abort;
    const timeout = window.setTimeout(() => abort.abort(), 15_000);
    setBusy(true); setOutput(null); setError(null);
    try { const value = await readNearTextOutput(task, abort.signal); if (version === generation.current) setOutput({ scope, value }); }
    catch { if (version === generation.current) { setOutput(null); setError("The answer failed current ownership, settled-cost or local readback checks. No answer is displayed."); } }
    finally { window.clearTimeout(timeout); if (version === generation.current) setBusy(false); }
  }
  if (task && task.capability_id !== NEAR_TEXT_CAPABILITY) return null;
  const contents = <section aria-label="NEAR text question" className="grid gap-3 rounded border border-white/15 bg-slate-950 p-4 text-slate-100">
    <div className="flex justify-between gap-2"><h3 className="font-semibold">NEAR text question</h3>{onClose && <button type="button" onClick={onClose} disabled={busy}>Close NEAR question</button>}</div>
    <p className="text-xs">{NEAR_TEXT_DISCLOSURE} Private artifacts restrict local access. This uses HTTPS with {NEAR_TEXT_MODEL}; TEE verification and end-to-end encryption are not provided.</p>
    <p className="text-xs">One request, one attempt, no automatic retry or learning. A settled provider charge and local readback are required before an answer can be shown. A charge may exceed the local reserve.</p>
    {!owned && <div role="alert">A current owned operator session is required.</div>}
    {error && <div role="alert">{error}</div>}
    {!task && <>
      <div role="status">NEAR settings: {readySetup?.status.replace(/_/g, " ") ?? "not configured"}{readySetup?.reason_code ? ` · ${readySetup.reason_code}` : ""}. Configured does not prove provider availability.</div>
      <button type="button" disabled={!owned || busy} onClick={() => void refreshSettings()}>Refresh NEAR settings</button>
      <fieldset disabled={!owned || busy || uncertain} className="grid gap-3 text-sm">
        <label>Question Goal<select aria-label="NEAR question Goal" className="cockpit-input mt-1 w-full" value={goalId} onChange={e => { setGoalId(e.target.value); setAcknowledged(false); }}><option value="">Choose a current owned Goal</option>{goals.filter(item => nearTextGoalEligible(item, ownerSessionId)).map(item => <option key={item.id} value={item.id}>{item.title}</option>)}</select></label>
        <p className="text-xs">The Goal must have current finite approval for this capability. The server checks its current permissions and request limits.</p>
        <label>Private question<textarea aria-label="NEAR private question" className="cockpit-input mt-1 w-full" rows={5} maxLength={8192} value={question} onChange={e => { setQuestion(e.target.value); setAcknowledged(false); }} /></label>
        <p className="text-xs">Question limit: 8192 UTF-8 bytes. The draft stays in memory and is cleared when this window closes. It is excluded from the public task title and description.</p>
        <label>Maximum answer tokens<input aria-label="NEAR question maximum answer tokens" className="cockpit-input mt-1 w-full" type="number" min={1} max={readySetup?.max_output_tokens ?? 1024} value={maxTokens} onChange={e => { setMaxTokens(e.target.value); setAcknowledged(false); }} /></label>
        <label className="flex items-start gap-2"><input type="checkbox" aria-label="Acknowledge sending this NEAR question" checked={acknowledged} onChange={e => setAcknowledged(e.target.checked)} /><span>I acknowledge NEAR's plaintext access to this question and the configured shared budget.</span></label>
      </fieldset>
      {onOpenApprovals && <button type="button" onClick={onOpenApprovals}>Open Goal approvals</button>}
      <button type="button" disabled={!owned || busy || uncertain || !goal?.revision || readySetup?.status !== "configured" || !readySetup.enabled || !readySetup.key_present || !readySetup.consent_current} onClick={() => void submit()}>{busy ? "Creating question task…" : "Create NEAR question task"}</button>
    </>}
    {task && <>
      <div role="status">Work: {task.status} · local readback: {task.readback_status} · no_learning</div>
      {liability && <div role="alert">The answer is unavailable and cannot be previewed or recovered. Any received answer was discarded. Inspect the original accounting for any remaining debt. Settlement does not resend the question or restore its answer.</div>}
      {!eligible && !liability && <p className="text-xs">An answer is available only after the original job succeeds, its charge settles and current local readback passes. Human review remains separate.</p>}
      {onOpenAccounting && (liability || task.status === "blocked") && <button type="button" onClick={onOpenAccounting}>Open existing cost settlement</button>}
      <button type="button" disabled={busy || !eligible} onClick={() => void readAnswer()}>Read NEAR answer</button>
      {eligible && output?.scope === scope && <>
        <pre aria-label="Literal NEAR answer" className="whitespace-pre-wrap break-all text-sm">{output.value.text}</pre>
        <div className="break-all text-xs">Settled provider charge: ${(output.value.receipt.cost_microusd / 1_000_000).toFixed(6)} · source NEAR billing · {output.value.receipt.cost_reference}</div>
        <p className="text-xs">Local readback passed. Answer accuracy and TEE execution are not verified. Human review remains separate; no learning was recorded.</p>
        <details><summary>Content-safe answer receipt</summary><pre className="whitespace-pre-wrap break-all text-xs">{JSON.stringify(output.value.receipt, null, 2)}</pre></details>
      </>}
    </>}
  </section>;
  return task ? contents : createPortal(<div className="fixed inset-0 z-[1100] overflow-auto bg-black/70 p-6" role="dialog" aria-modal="true" aria-label="Create NEAR text question"><div className="mx-auto max-w-2xl">{contents}</div></div>, document.body);
}
