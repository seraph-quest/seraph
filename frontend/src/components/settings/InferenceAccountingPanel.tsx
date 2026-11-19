import { useState } from "react";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import { normalizeInferenceAccounting, type InferenceAccountingStatus } from "../../lib/modelFabric";
import { dollars, monthName } from "./ModelCard";

interface Props {
  accounting: InferenceAccountingStatus | null | undefined;
  stale: boolean;
  onRefresh: () => Promise<void>;
}

/** Plain label for a ledger row (#1284): what the request cost, not what it was allowed. */
export function spendStateLabel(state: string): string {
  if (state === "settled") return "priced";
  if (state === "unknown" || state === "contact_started") return "not priced yet";
  if (state === "reserved" || state === "released") return "not sent";
  return state.replace(/_/g, " ");
}

/** The note shown once a ledger mismatch was resealed from the rows present (#1284). */
export function historyGapText(since: string | null | undefined): string {
  const date = typeof since === "string" && /^\d{4}-\d{2}-\d{2}/.test(since) && Number.isFinite(Date.parse(since)) ? since.slice(0, 10) : null;
  return date ? `Spend history may be incomplete since ${date}.` : "Spend history may be incomplete.";
}

/**
 * Spend history (#1284). Seraph keeps no spending limit; NEAR's account does.
 * This only shows what was spent and lets the operator record a charge NEAR
 * has not priced. Nothing waits on it.
 */
export function InferenceAccountingPanel({ accounting, stale, onRefresh }: Props) {
  const [selected, setSelected] = useState<string | null>(null);
  const [amount, setAmount] = useState("");
  const [evidence, setEvidence] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [latest, setLatest] = useState<{ source: typeof accounting; value: InferenceAccountingStatus | null } | null>(null);
  const view = latest && latest.source === accounting ? latest.value : accounting;
  const operation = view?.operations.find((row) => row.operation_id === selected);
  const control = operation?.controls?.find((entry) => entry.action === "settle");
  const available = Boolean(view && view.status !== "unavailable");
  const unpriced = view?.unpriced_requests ?? 0;

  async function settle() {
    if (!operation || !control || stale || saving) return;
    setError(null);
    const value = Number(amount);
    if (!amount.trim() || !Number.isSafeInteger(value) || value < 0 || value > 1_000_000_000 || !/^[0-9a-f]{64}$/.test(evidence) || !acknowledged) {
      setError("Enter a finite whole micro USD amount, evidence SHA-256, and acknowledge this exact request.");
      return;
    }
    setSaving(true);
    try {
      const response = await apiFetch(`${API_URL}${control.endpoint}`, {
        method: control.method, headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ operation_id: operation.operation_id, job_id: operation.job_id,
          expected_revision: control.expected_revision, actual_cost_microusd: value,
          evidence_digest: evidence, idempotency_key: crypto.randomUUID() }),
        signal: AbortSignal.timeout(15_000),
      });
      if (!response.ok) throw new Error(`Recording the charge failed (${response.status}); refresh before retrying.`);
      const readback = await apiFetch(`${API_URL}/api/settings/model-fabric/accounting`, { signal: AbortSignal.timeout(5_000) });
      if (!readback.ok) throw new Error("Charge recorded; spend history could not be read back. Refresh to see it.");
      setLatest({ source: accounting, value: normalizeInferenceAccounting(await readback.json()) });
      setSelected(null); setAmount(""); setEvidence(""); setAcknowledged(false);
      await onRefresh();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Recording the charge is unavailable.");
    } finally { setSaving(false); }
  }

  return <div className="mt-2 border border-retro-text/10 p-2 text-[9px]" aria-label="Spend history">
    <div className="flex justify-between gap-2">
      <span className="uppercase tracking-wider text-retro-text/70">Spend history</span>
      <button type="button" onClick={() => { setLatest(null); void onRefresh(); }} disabled={saving}>Refresh spend history</button>
    </div>
    <div className="text-retro-text/50">Spending limits are set in your NEAR account. Seraph only records what each request cost.{stale ? " Retained, stale." : ""}</div>
    {available
      ? <div className="mt-1" data-testid="spend-total">{monthName(view?.period_id)}: {(view?.spent_microusd ?? 0) > 0 ? `${dollars(view?.spent_microusd ?? 0)} spent.` : "nothing spent."}{unpriced > 0 ? ` ${unpriced} request${unpriced === 1 ? " is" : "s are"} not priced by NEAR yet.` : ""}</div>
      : <div className="mt-1" role="status">Spend this month is not available.</div>}
    {available && view?.history_verified === false && <div role="note" data-testid="spend-history-gap">{historyGapText(view.history_gap_since)}</div>}
    {view?.operations_truncated && <div role="status">Showing the first {view.operations.length} of {view.operation_count} requests.</div>}
    {view?.operations.map((row) => <div key={row.operation_id} className="mt-1 border-t border-retro-text/10 pt-1" data-testid="spend-row">
      <div>{row.runtime_path} · {spendStateLabel(row.state)}{row.state === "settled" && row.actual_cost_microusd !== null ? ` · ${dollars(row.actual_cost_microusd)}` : ""} · month {row.period_id}</div>
      <div className="break-all text-retro-text/40">Job {row.job_id} · owner {row.owner_id}</div>
      {row.controls?.some((entry) => entry.action === "settle") && <button type="button" disabled={stale || saving}
        onClick={() => { setSelected(row.operation_id); setAmount(""); setEvidence(""); setAcknowledged(false); setError(null); }}>Record the charge</button>}
    </div>)}
    {operation && control && <div className="mt-2 border border-retro-text/20 p-2">
      <div className="break-all">Charge for {operation.operation_id} · revision {operation.revision}</div>
      <p className="text-retro-text/60">Optional. This records the charge you saw in your NEAR account; Seraph does not verify it, and nothing waits on it.</p>
      <label className="block">Charge (micro USD)
        <input aria-label="Declared account charge" type="number" min="0" max="1000000000" value={amount} onChange={(event) => setAmount(event.target.value)} className="ml-2 border border-retro-text/20 bg-retro-bg" /></label>
      <label className="block">Evidence SHA-256
        <input aria-label="Settlement evidence SHA-256" value={evidence} onChange={(event) => setEvidence(event.target.value)} className="ml-2 border border-retro-text/20 bg-retro-bg" /></label>
      <label className="block"><input type="checkbox" checked={acknowledged} onChange={(event) => setAcknowledged(event.target.checked)} /> I confirm this charge is for this exact request.</label>
      <button type="button" disabled={saving || stale || !acknowledged} onClick={() => void settle()}>{saving ? "Recording…" : "Save charge"}</button>
      <button type="button" disabled={saving} onClick={() => setSelected(null)} className="ml-2">Cancel</button>
    </div>}
    {error && <div role="alert" className="text-red-400">{error}</div>}
  </div>;
}
