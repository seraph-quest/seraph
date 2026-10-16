import { useEffect, useRef, useState } from "react";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import type { GoalInfo } from "../../types";

type PairingStatus = { state_revision: number; pairing_state: string; owner_principal_id: string; operator_session_id: string; live_transport: boolean };

export function TelegramCaptureControl({ ownerPrincipalId, ownerSessionId, goals }: {
  ownerPrincipalId: string | null; ownerSessionId: string | null; goals: GoalInfo[];
}) {
  const [pairing, setPairing] = useState<PairingStatus | null>(null);
  const [goalId, setGoalId] = useState("");
  const [budget, setBudget] = useState("0");
  const [calls, setCalls] = useState("0");
  const [wall, setWall] = useState("900");
  const [egress, setEgress] = useState(false);
  const [enabled, setEnabled] = useState(false);
  const [documentAcquisition, setDocumentAcquisition] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const scope = `${ownerPrincipalId ?? ""}:${ownerSessionId ?? ""}`;
  const currentScope = useRef(scope); currentScope.current = scope;
  const currentGoals = useRef(goals); currentGoals.current = goals;
  const request = useRef<AbortController | null>(null);
  const ownedGoals = goals.filter(goal => goal.status === "active" && goal.owner_session_id === ownerSessionId && goal.revision && goal.ownership_access !== "recovered_read_only");
  const goal = ownedGoals.find(row => row.id === goalId);
  useEffect(() => {
    setPairing(null); setGoalId(""); setBudget("0"); setCalls("0"); setEgress(false); setEnabled(false); setDocumentAcquisition(false); setNotice(null); setBusy(false);
    return () => { request.current?.abort(); request.current = null; };
  }, [scope]);
  useEffect(() => { setEnabled(false); setEgress(false); setDocumentAcquisition(false); }, [goalId, goal?.revision]);

  async function act(save: boolean) {
    if (busy || !ownerPrincipalId || !ownerSessionId) return;
    const controller = new AbortController(); request.current?.abort(); request.current = controller;
    const originalScope = scope, originalGoal = goal;
    const current = () => request.current === controller && !controller.signal.aborted && currentScope.current === originalScope;
    setBusy(true); setNotice(null);
    try {
      let body: Record<string, unknown> | undefined;
      if (save) {
        if (!pairing || !originalGoal?.revision) throw new Error("Refresh the current pairing and select an active owned Goal.");
        const maxCost = Number(budget), maxCalls = Number(calls), seconds = Number(wall);
        if (!Number.isSafeInteger(maxCost) || maxCost < 0 || maxCost > 1e9 || !Number.isSafeInteger(maxCalls) || maxCalls < 0 || maxCalls > 12 || !Number.isSafeInteger(seconds) || seconds < 1 || seconds > 900) throw new Error("Use finite original capture limits: cost 0–1000000000, calls 0–12, seconds 1–900.");
        if (maxCalls > 0 && (!egress || maxCost < 1)) throw new Error("Model calls require a positive separate allowance and explicit Task model consent.");
        body = { expected_revision: pairing.state_revision, enabled, goal_id: originalGoal.id, goal_revision: originalGoal.revision,
          requested_output: { type: "object", properties: { result: { type: "string" } }, required: ["result"], additionalProperties: false },
          limits: { max_steps: 16, max_inference_calls: maxCalls, wall_seconds: seconds, depth: 0, max_outstanding_children: 2, max_cost_microusd: maxCost }, inference_egress_acknowledged: egress,
          document_acquisition: documentAcquisition && enabled ? { action: "acquire_one_original_task_document", max_sources: 1,
            source_cap_bytes: 16777216, docx_cap_bytes: 10485760, formats: ["pdf", "docx", "xlsx", "csv"], no_learning: true } : null };
      }
      const response = await apiFetch(`${API_URL}/api/telegram/${save ? "capture-selection" : "status"}`, { method: save ? "PUT" : "GET", signal: controller.signal,
        ...(body ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {}) });
      const value: unknown = await response.json();
      if (!current()) return;
      if (!response.ok) { setPairing(null); setEnabled(false); throw new Error("Current pairing or Goal is unavailable. Refresh before another explicit selection."); }
      if (save) {
        if (currentGoals.current.find(row => row.id === originalGoal?.id)?.revision !== originalGoal?.revision) { setEnabled(false); setPairing(null); throw new Error("Goal changed while saving. Refresh the current selection."); }
        const receipt = value as { state_revision?: unknown };
        if (!Number.isSafeInteger(receipt.state_revision)) throw new Error("Capture selection receipt is incomplete. Refresh to inspect current state.");
        setPairing(previous => previous ? { ...previous, state_revision: receipt.state_revision as number } : null);
        setNotice(enabled ? "Capture selected. A new /task message prepares a review-only Task with its original limits; it does not execute a plan." : "Task capture disabled.");
      } else {
        const status = value as Partial<PairingStatus>;
        if (!Number.isSafeInteger(status.state_revision) || status.owner_principal_id !== ownerPrincipalId || status.operator_session_id !== ownerSessionId || status.pairing_state !== "active") throw new Error("No current authenticated Telegram pairing is available.");
        setPairing(status as PairingStatus);
        setNotice(status.live_transport ? "Current pairing loaded. Capture selection remains explicit." : "Current pairing loaded. Live Telegram delivery is unverified; the injected transport is available for review.");
      }
    } catch (error) {
      if (current()) setNotice(error instanceof Error ? error.message : "Selection outcome is uncertain. Refresh current state; do not renew an original event.");
    } finally { if (current()) setBusy(false); }
  }
  return <section aria-label="Telegram Task capture" className="cockpit-card">
    <h3>Telegram Task capture</h3>
    <p>Disabled by default. Select a current paired source and Goal before sending /task. Ordinary messages remain chat.</p>
    <button type="button" disabled={busy || !ownerSessionId} onClick={() => void act(false)}>Refresh current Telegram pairing</button>
    <label>Goal<select aria-label="Telegram capture Goal" value={goalId} disabled={busy} onChange={event => setGoalId(event.target.value)}><option value="">Choose active Goal</option>{ownedGoals.map(row => <option key={row.id} value={row.id}>{row.title} · revision {row.revision}</option>)}</select></label>
    <label>Allowance<input aria-label="Telegram capture allowance" type="number" min="0" max="1000000000" value={budget} disabled={busy} onChange={event => setBudget(event.target.value)} /></label>
    <label>Model calls<input aria-label="Telegram capture model calls" type="number" min="0" max="12" value={calls} disabled={busy} onChange={event => setCalls(event.target.value)} /></label>
    <label>Seconds<input aria-label="Telegram capture seconds" type="number" min="1" max="900" value={wall} disabled={busy} onChange={event => setWall(event.target.value)} /></label>
    <label><input type="checkbox" checked={egress} disabled={busy} onChange={event => setEgress(event.target.checked)} />Allow captured intent to enter separately reviewed Task model planning</label>
    <label><input type="checkbox" checked={enabled} disabled={busy} onChange={event => setEnabled(event.target.checked)} />Enable Task capture for this selected Goal</label>
    <label><input type="checkbox" checked={documentAcquisition} disabled={busy || !enabled} onChange={event => setDocumentAcquisition(event.target.checked)} />Acquire one future original /task document from Telegram into private local quarantine (PDF, DOCX, XLSX or CSV; 16 MiB, DOCX 10 MiB). This permits no parsing, model inference, learning or public content delivery.</label>
    <button type="button" disabled={busy || !pairing || !goal} onClick={() => void act(true)}>Save Telegram capture selection</button>
    {notice && <p role="status">{notice}</p>}
  </section>;
}
