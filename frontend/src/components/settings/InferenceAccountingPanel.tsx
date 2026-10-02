import { useState } from "react";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import { normalizeInferenceAccounting, type InferenceAccountingStatus } from "../../lib/modelFabric";

interface Props {
  accounting: InferenceAccountingStatus | null | undefined;
  stale: boolean;
  onRefresh: () => Promise<void>;
}

export function InferenceAccountingPanel({ accounting, stale, onRefresh }: Props) {
  const [selected, setSelected] = useState<string | null>(null);
  const [amount, setAmount] = useState("");
  const [evidence, setEvidence] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [periodAcknowledged, setPeriodAcknowledged] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [latest, setLatest] = useState<{ source: typeof accounting; value: InferenceAccountingStatus | null } | null>(null);
  const view = latest && latest.source === accounting ? latest.value : accounting;
  const operation = view?.operations.find((row) => row.operation_id === selected);
  const control = operation?.controls?.find((entry) => entry.action === "settle");

  async function acknowledgePeriod() {
    const review = view?.period_review;
    if (!review || !periodAcknowledged || stale || saving) return;
    setSaving(true); setError(null);
    try {
      const response = await apiFetch(`${API_URL}${review.endpoint}`, { method: "POST",
        headers: { "Content-Type": "application/json" }, signal: AbortSignal.timeout(15_000),
        body: JSON.stringify({ period_id: review.period_id, expected_revision: review.expected_revision }) });
      if (!response.ok) throw new Error(`Period review failed (${response.status}); refresh before retrying.`);
      const readback = await apiFetch(`${API_URL}/api/settings/model-fabric/accounting`, { signal: AbortSignal.timeout(5_000) });
      if (!readback.ok) throw new Error("Period review saved; refresh accounting before further action.");
      setLatest({ source: accounting, value: normalizeInferenceAccounting(await readback.json()) });
      setPeriodAcknowledged(false);
      await onRefresh();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Period review is unavailable.");
    } finally { setSaving(false); }
  }

  async function settle() {
    if (!operation || !control || stale || saving) return;
    setError(null);
    const value = Number(amount);
    if (!amount.trim() || !Number.isSafeInteger(value) || value < 0 || value > 1_000_000_000 || !/^[0-9a-f]{64}$/.test(evidence) || !acknowledged) {
      setError("Enter a finite whole micro USD amount, evidence SHA-256, and acknowledge this exact operation.");
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
      if (!response.ok) throw new Error(`Settlement failed (${response.status}); refresh the operation before retrying.`);
      const readback = await apiFetch(`${API_URL}/api/settings/model-fabric/accounting`, { signal: AbortSignal.timeout(5_000) });
      if (!readback.ok) throw new Error("Settlement saved; accounting readback is unavailable. Refresh before further action.");
      setLatest({ source: accounting, value: normalizeInferenceAccounting(await readback.json()) });
      setSelected(null); setAmount(""); setEvidence(""); setAcknowledged(false);
      await onRefresh();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Accounting settlement is unavailable.");
    } finally { setSaving(false); }
  }

  return <div className="mt-2 border border-retro-text/10 p-2 text-[9px]" aria-label="Inference accounting">
    <div className="flex justify-between gap-2">
      <span className="uppercase tracking-wider text-retro-text/70">OpenRouter account usage · deployment budget</span>
      <button type="button" onClick={() => { setLatest(null); void onRefresh(); }} disabled={saving}>Refresh accounting</button>
    </div>
    <div className="text-retro-text/50">UTC month {view?.period_id ?? "unavailable"} · settings revision {view?.settings_revision ?? "unavailable"}{stale ? " · retained, stale" : ""}</div>
    <div className="mt-1">Committed {view?.committed_microusd ?? "unknown"} · reserved {view?.reserved_microusd ?? "unknown"} · unknown {view?.unknown_microusd ?? "unknown"} · remaining {view?.remaining_microusd ?? "unavailable"} micro USD</div>
    {view?.status !== "ready" && <div role="status" className="text-yellow-400">Paid inference blocked: {view?.reason_code ?? "accounting continuity unavailable"}. {view?.accounting_continuity_verified ? "Review the exact owning recovery control; settled and unresolved cost stays retained." : "Restore the retained ledger and lifecycle witness; creating a fresh budget cannot recover it."}</div>}
    {view?.period_review && <div className="mt-2 border border-yellow-400/30 p-2">
      <div>Review observed UTC month {view.period_review.period_id} · accounting revision {view.period_review.expected_revision}</div>
      <p>Every month change needs an exact review. Clock correction keeps the high-water month {view.period_high_water} and conservatively counts future-attributed charges. This cannot resume work or re-grant provider access.</p>
      <label><input type="checkbox" checked={periodAcknowledged} onChange={(event) => setPeriodAcknowledged(event.target.checked)} /> I acknowledge this exact month and retained liabilities.</label>
      <button type="button" disabled={stale || saving || !periodAcknowledged} onClick={() => void acknowledgePeriod()}>Acknowledge observed UTC month</button>
    </div>}
    <div className="mt-1 text-retro-text/50">Unresolved liabilities remain held across restart, login, recovery, and month rollover. Upstream BYOK invoices and credit purchase fees are outside this account-usage budget.</div>
    <div className="text-retro-text/50">These are local admission ceilings and conservative reservations. A provider may charge more than the declared request bound; the full account charge remains recorded and further admission blocks until the bound is reviewed.</div>
    {view?.operations_truncated && <div role="status">Showing the first 128 of {view.operation_count} accounting operations. Open the owning task inspector to inspect its exact operation.</div>}
    {view?.operations.map((row) => <div key={row.operation_id} className="mt-1 border-t border-retro-text/10 pt-1">
      <div>{row.runtime_path} · {row.state} · held bound {row.bound_microusd} · original month {row.period_id}</div>
      <div className="break-all text-retro-text/40">Job {row.job_id} · owner {row.owner_id}</div>
      {row.recovery_reason && <div className="text-yellow-400">{row.recovery_reason.replace(/_/g, " ")}</div>}
      {row.controls?.some((entry) => entry.action === "settle") && <button type="button" disabled={stale || saving || view.status !== "ready" && !view.accounting_continuity_verified}
        onClick={() => { setSelected(row.operation_id); setAmount(""); setEvidence(""); setAcknowledged(false); setError(null); }}>Reconcile exact cost operation</button>}
    </div>)}
    {operation && control && <div className="mt-2 border border-yellow-400/30 p-2">
      <div className="break-all">Settlement for {operation.operation_id} · revision {operation.revision}</div>
      <p className="text-yellow-400">Manual settlement records your declaration and remains externally unverified. It changes deployment accounting only; it cannot resume the job, adopt output, or restore a grant.</p>
      <label className="block">Declared account charge (micro USD)
        <input aria-label="Declared account charge" type="number" min="0" max="1000000000" value={amount} onChange={(event) => setAmount(event.target.value)} className="ml-2 border border-retro-text/20 bg-retro-bg" /></label>
      <label className="block">Evidence SHA-256
        <input aria-label="Settlement evidence SHA-256" value={evidence} onChange={(event) => setEvidence(event.target.value)} className="ml-2 border border-retro-text/20 bg-retro-bg" /></label>
      <label className="block"><input type="checkbox" checked={acknowledged} onChange={(event) => setAcknowledged(event.target.checked)} /> I acknowledge the remaining uncertainty for this exact operation.</label>
      <button type="button" disabled={saving || stale || !acknowledged} onClick={() => void settle()}>{saving ? "Recording settlement" : "Record declared settlement"}</button>
      <button type="button" disabled={saving} onClick={() => setSelected(null)} className="ml-2">Cancel</button>
    </div>}
    {error && <div role="alert" className="text-red-400">{error}</div>}
  </div>;
}
