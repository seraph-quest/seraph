import { useEffect, useRef, useState } from "react";
import { dollarsToMicrousd, microusdToDollars } from "../../lib/modelFabric";
import type { InferenceLaneStatus, InferenceLaneValue, ModelFabricSettingsStatus } from "../../lib/modelFabric";

interface Props {
  lane?: InferenceLaneStatus | null;
  stale: boolean;
  policyRevision?: number;
  /** Fallback ceiling when no lane exists yet (fresh deployment). */
  ledgerCeilingMicrousd?: number | null;
  onSave: (payload: Record<string, unknown>) => Promise<ModelFabricSettingsStatus>;
}
interface Draft { ceilingUsd: string; maxQueued: string; maxOutstanding: string; maxRetries: string }

function draftFrom(lane?: InferenceLaneStatus | null, ledgerCeiling?: number | null): Draft {
  const ceiling = lane?.spend_ceiling_microusd ?? ledgerCeiling ?? null;
  return { ceilingUsd: ceiling ? microusdToDollars(ceiling) : "", maxQueued: String(lane?.max_queued ?? 64),
    maxOutstanding: String(lane?.max_outstanding_per_owner ?? 16), maxRetries: String(lane?.max_retries ?? 2) };
}
function whole(value: string, label: string, min: number, max: number): number {
  const parsed = Number(value);
  if (!value.trim() || !Number.isSafeInteger(parsed) || parsed < min || parsed > max) throw new Error(label + " must be a whole number between " + min + " and " + max + ".");
  return parsed;
}
const inputClass = "min-w-0 border border-retro-text/20 bg-retro-bg px-1 py-0.5 text-retro-text disabled:opacity-40";

/**
 * Provider-neutral shared inference lane (ADR-035): one deployment ceiling and
 * one serial lane for every provider. Saving never contacts a provider and
 * never resets the ledger or existing liabilities.
 */
export function InferenceLanePanel({ lane, stale, policyRevision, ledgerCeilingMicrousd, onSave }: Props) {
  const [draft, setDraft] = useState(() => draftFrom(lane, ledgerCeilingMicrousd));
  const [revision, setRevision] = useState(policyRevision);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const dirty = useRef(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    // Metadata failure keeps the last-known values and edits usable.
    if (stale) return;
    if (policyRevision !== undefined) setRevision(policyRevision);
    if (!dirty.current) setDraft(draftFrom(lane, ledgerCeilingMicrousd));
  }, [lane, stale, policyRevision, ledgerCeilingMicrousd]);
  function update<K extends keyof Draft>(field: K, value: Draft[K]) {
    dirty.current = true;
    setDraft((current) => ({ ...current, [field]: value }));
  }
  async function save() {
    setBusy(true); setError(null); setMessage(null);
    try {
      if (!Number.isSafeInteger(revision) || (revision ?? 0) < 1) throw new Error("Refresh current settings to obtain the policy revision before saving.");
      const ceiling = dollarsToMicrousd(draft.ceilingUsd);
      if (ceiling === null || ceiling < 1 || ceiling > 1_000_000_000) throw new Error("Shared ceiling must be a dollar amount between $0.000001 and $1000.");
      const value: InferenceLaneValue = {
        spend_ceiling_microusd: ceiling,
        max_queued: whole(draft.maxQueued, "Max queued", 1, 64),
        max_inflight: 1,
        max_outstanding_per_owner: whole(draft.maxOutstanding, "Max outstanding per owner", 1, 16),
        max_retries: whole(draft.maxRetries, "Max retries", 0, 2),
      };
      const next = await onSave({ expected_policy_revision: revision, inference_lane: value });
      if (!mounted.current) return;
      dirty.current = false;
      setRevision(next.egress_revision);
      setDraft(draftFrom(next.inference_lane, next.inference_accounting?.ceiling_microusd));
      setMessage("Shared lane saved. The ledger and existing liabilities are unchanged.");
    } catch (reason) {
      if (mounted.current) setError((reason instanceof Error ? reason.message : "Shared lane could not be saved.").slice(0, 512));
    } finally {
      if (mounted.current) setBusy(false);
    }
  }
  const state = stale ? "stale retained" : !lane ? "not configured" : lane.published ? "published" : "migration pending · save to publish";
  return (
    <section className="mt-2 border border-retro-text/10 px-2 py-2" aria-label="Shared inference lane" data-testid="inference-lane-panel">
      <div className="flex justify-between gap-2 text-[10px]"><span>Shared inference lane</span><span>{state}</span></div>
      <p className="mt-1 text-[9px] text-retro-text/60">
        One deployment ceiling and one in-flight request for every provider. Saving never calls a provider and never resets the ledger or existing liabilities.
      </p>
      <fieldset disabled={busy} className="mt-2 grid grid-cols-[120px_minmax(0,1fr)] gap-2 text-[9px]">
        <label htmlFor="inference-lane-ceiling" className="text-retro-text/40">Monthly ceiling ($)</label>
        <input id="inference-lane-ceiling" aria-label="Shared inference ceiling (USD)" inputMode="decimal" value={draft.ceilingUsd} onChange={(event) => update("ceilingUsd", event.target.value)} className={inputClass + " w-24"} placeholder="e.g. 5" />
        <div className="text-retro-text/40">Lane bounds</div>
        <div className="grid grid-cols-4 gap-1">
          <input aria-label="Shared lane max queued" title="max queued" type="number" min="1" max="64" value={draft.maxQueued} onChange={(event) => update("maxQueued", event.target.value)} className={inputClass} />
          <input aria-label="Shared lane max in flight" title="max in flight" type="number" value="1" readOnly className={inputClass} />
          <input aria-label="Shared lane max outstanding per owner" title="owner outstanding" type="number" min="1" max="16" value={draft.maxOutstanding} onChange={(event) => update("maxOutstanding", event.target.value)} className={inputClass} />
          <input aria-label="Shared lane max retries" title="max retries" type="number" min="0" max="2" value={draft.maxRetries} onChange={(event) => update("maxRetries", event.target.value)} className={inputClass} />
        </div>
      </fieldset>
      <div className="mt-2 flex flex-wrap items-center gap-2 text-[9px]">
        <button type="button" disabled={busy} onClick={() => void save()} className="border border-retro-text/20 px-2 py-1 text-retro-text/70 disabled:opacity-40">{busy ? "Saving" : "Save shared lane"}</button>
        <span className="text-retro-text/40">revision {revision ?? "unavailable"}</span>
      </div>
      {error && <div role="alert" className="mt-1 text-[9px] text-red-400">{error}</div>}
      {message && <div role="status" className="mt-1 text-[9px] text-retro-text/70">{message}</div>}
    </section>
  );
}
