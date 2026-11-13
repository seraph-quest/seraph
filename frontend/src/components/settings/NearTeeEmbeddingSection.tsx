import { useEffect, useRef, useState } from "react";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import { NEAR_TEE_API_BASE, NEAR_TEE_EMBEDDING_MODELS, normalizeModelFabricSettings } from "../../lib/modelFabric";
import type { ModelFabricSettingsStatus, NearTeeSetupInput, NearTeeSetupStatus, NearTeeSlotStatus } from "../../lib/modelFabric";

interface Props {
  setup?: NearTeeSetupStatus | null;
  stale: boolean;
  revision?: number;
  policyRevoked?: boolean;
  laneCeilingMicrousd?: number | null;
  onSave: (payload: Record<string, unknown>) => Promise<ModelFabricSettingsStatus>;
  onRefreshed?: (next: ModelFabricSettingsStatus) => void;
}
interface Draft { enabled: boolean; model: string; maxInput: string; batch: string; timeout: string; reserveUsd: string; ttl: string }

const CHECKS = ["embedding", "health", "latency_ms"] as const;
const EVIDENCE_REASON = "near_tee_embedding_evidence_required";
const inputClass = "min-w-0 border border-retro-text/20 bg-retro-bg px-1 py-0.5 text-retro-text disabled:opacity-40";

function usd(value: number) { return (value / 1_000_000).toFixed(6).replace(/0+$/, "").replace(/\.$/, ""); }
function readable(value: string | null | undefined) { return (value ?? "").replace(/_/g, " "); }
function money(value: string, label: string): number {
  if (!/^\d+(?:\.\d{1,6})?$/.test(value)) throw new Error(label + " requires a USD amount with at most six decimal places.");
  const [whole, fraction = ""] = value.split(".");
  const parsed = Number(whole) * 1_000_000 + Number(fraction.padEnd(6, "0"));
  if (!Number.isSafeInteger(parsed) || parsed < 1 || parsed > 1_000_000_000) throw new Error(label + " must be between $0.000001 and $1000.");
  return parsed;
}
function number(value: string, label: string, max: number, integer = true): number {
  const parsed = Number(value);
  if (!value.trim() || !Number.isFinite(parsed) || parsed < 1 || parsed > max || (integer && !Number.isInteger(parsed))) throw new Error(label + " must be between 1 and " + max + (integer ? " as a whole number." : "."));
  return parsed;
}
function draftFrom(slot?: NearTeeSlotStatus | null): Draft {
  return { enabled: slot?.enabled ?? false, model: slot?.model_id ?? "", maxInput: String(slot?.max_input_tokens ?? 8192),
    batch: String(slot?.max_batch_size ?? 16), timeout: String(slot?.timeout_seconds ?? 30),
    reserveUsd: usd(slot?.request_cost_bound_microusd ?? 5_000), ttl: String(slot?.catalog_ttl_hours ?? 24) };
}
function blockedText(reason: string | null | undefined) {
  if (reason === EVIDENCE_REASON) return "Blocked: NEAR embeddings stay off until source evidence shows the embeddings and billing endpoints honour x-no-aliasing (ADR-035). Saving, the catalog check and capability checks cannot clear this.";
  return reason ? "Blocked: " + readable(reason) : null;
}

/** ADR-035 embedding slot and operator-started re-index (minimal controls). */
export function NearTeeEmbeddingSection({ setup, stale, revision, policyRevoked = false, laneCeilingMicrousd, onSave, onRefreshed }: Props) {
  const slot = setup?.slots.embedding ?? null;
  const index = setup?.embedding_index ?? null;
  const job = index?.job ?? null;
  const [draft, setDraft] = useState(() => draftFrom(slot));
  const [acknowledged, setAcknowledged] = useState(false);
  const [budgetUsd, setBudgetUsd] = useState("0.1");
  const [maxItems, setMaxItems] = useState("1000");
  const [reindexAck, setReindexAck] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const dirty = useRef(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    if (stale) return;
    setAcknowledged(false);
    if (!dirty.current) setDraft(draftFrom(setup?.slots.embedding));
  }, [setup, stale]);
  function update<K extends keyof Draft>(field: K, value: Draft[K]) {
    dirty.current = true;
    setDraft(current => ({ ...current, [field]: value }));
    setAcknowledged(false);
  }
  const unchanged = Boolean(slot && draft.enabled === slot.enabled && draft.model === slot.model_id
    && Number(draft.maxInput) === slot.max_input_tokens && Number(draft.batch) === slot.max_batch_size
    && Number(draft.timeout) === slot.timeout_seconds && Number(draft.ttl) === slot.catalog_ttl_hours
    && (() => { try { return money(draft.reserveUsd, "Reserve") === slot.request_cost_bound_microusd; } catch { return false; } })());
  const carryConsent = Boolean(slot?.enabled && slot.consent_current && !policyRevoked && unchanged);
  async function detail(response: Response) {
    try { const body = await response.json(); const value = body?.detail; return typeof value === "string" ? value : JSON.stringify(value ?? response.status); }
    catch { return String(response.status); }
  }
  async function refresh() {
    const response = await apiFetch(API_URL + "/api/settings/model-fabric");
    if (!response.ok) return;
    const next = normalizeModelFabricSettings(await response.json());
    if (next && mounted.current) onRefreshed?.(next);
  }
  async function run(kind: string, action: () => Promise<string>) {
    setBusy(kind); setError(null); setMessage(null);
    try { const text = await action(); if (mounted.current) setMessage(text); }
    catch (reason) { if (mounted.current) setError((reason instanceof Error ? reason.message : "Request failed.").slice(0, 512)); }
    finally { if (mounted.current) setBusy(null); }
  }
  const save = () => run("save", async () => {
    if (!Number.isSafeInteger(revision) || (revision ?? 0) < 1) throw new Error("Refresh current settings to obtain the policy revision before saving.");
    if (!laneCeilingMicrousd) throw new Error("Publish the shared inference lane ceiling first.");
    if (!(NEAR_TEE_EMBEDDING_MODELS as readonly string[]).includes(draft.model)) throw new Error("Choose an embedding model from the NEAR TEE allowlist.");
    const reserve = money(draft.reserveUsd, "Per-request reserve");
    if (reserve > laneCeilingMicrousd) throw new Error("Per-request reserve cannot exceed the shared lane ceiling.");
    if (draft.enabled && !carryConsent && !acknowledged) throw new Error("Acknowledge that memory text is sent to NEAR in plaintext before enabling or changing this slot.");
    const value: NearTeeSetupInput = { schema_version: "seraph.near.tee.setup.v1", api_base: NEAR_TEE_API_BASE,
      slots: { embedding: { model_id: draft.model, enabled: draft.enabled,
        max_input_tokens: number(draft.maxInput, "Maximum input tokens per request", 32_768),
        max_output_tokens: 1, max_batch_size: number(draft.batch, "Inputs per request", 64),
        timeout_seconds: number(draft.timeout, "Request timeout", 120, false), request_cost_bound_microusd: reserve,
        catalog_ttl_hours: number(draft.ttl, "Catalog receipt lifetime", 24),
        plaintext_provider_egress_acknowledged: draft.enabled && acknowledged } } };
    const next = await onSave({ expected_policy_revision: revision, near_tee: value });
    dirty.current = false;
    if (mounted.current) { setAcknowledged(false); setDraft(draftFrom(next.near_tee?.slots.embedding)); }
    return "Embedding slot saved. Saving never calls NEAR.";
  });
  const capabilityCheck = () => run("capability", async () => {
    if (!slot) throw new Error("Save the embedding slot first.");
    for (const capability of CHECKS) {
      const response = await apiFetch(API_URL + "/api/settings/model-fabric/canary", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ profile_id: slot.profile_id, capability, timeout_seconds: slot.capability_check_timeout_seconds, proof_ttl_seconds: 86400 }) });
      if (!response.ok) throw new Error("Capability check refused (" + capability + "): " + await detail(response));
      const body = await response.json();
      if (body?.outcome !== "passed") throw new Error("Capability check failed (" + capability + "): " + readable(body?.error_code ?? "unknown"));
    }
    await refresh();
    return "Embedding capability check passed (mechanics only; it measured the vector dimension; three billed NEAR requests).";
  });
  const startReindex = () => run("reindex", async () => {
    if (!reindexAck) throw new Error("Acknowledge that canonical memory text is sent to NEAR in plaintext for re-indexing.");
    const budget = money(budgetUsd, "Re-index budget");
    const items = number(maxItems, "Maximum memories", index?.max_items ?? 10_000);
    const response = await apiFetch(API_URL + "/api/settings/model-fabric/near-tee/embedding-reindex", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_policy_revision: revision, budget_microusd: budget, max_items: items,
        memory_egress_acknowledged: true, request_uuid: crypto.randomUUID() }) });
    if (!response.ok) throw new Error("Re-index refused: " + readable(await detail(response)));
    setReindexAck(false);
    await refresh();
    return "Re-index started at background priority (below chat). Refresh to see progress.";
  });
  const cancelReindex = () => run("cancel", async () => {
    if (!job) throw new Error("No re-index run to cancel.");
    const response = await apiFetch(API_URL + "/api/settings/model-fabric/near-tee/embedding-reindex/cancel", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ job_id: job.job_id }) });
    if (!response.ok) throw new Error("Cancel refused: " + readable(await detail(response)));
    await refresh();
    return "Re-index cancelled. A batch already sent finishes and is billed; start another run to resume.";
  });
  const blocked = blockedText(slot?.reason_code);
  const running = job?.status === "running" && job.live;
  return <div className="mt-3 border-t border-retro-text/10 pt-2" aria-label="NEAR TEE embedding slot">
    <div className="flex justify-between gap-2 text-[10px]"><span>NEAR TEE · embedding slot · memory recall</span><span>{stale ? "stale retained" : readable(slot?.status ?? "disabled")}</span></div>
    <p className="mt-1 text-[9px] text-retro-text/60">Embeddings send canonical memory text to NEAR in plaintext. Vectors are stored locally in a namespace for this provider, model and measured dimension; older vectors are never mixed in. Nothing re-indexes automatically.</p>
    {blocked && <p className="mt-1 text-[9px]">{blocked}</p>}
    <p role="note" className="mt-1 text-[9px]">{index?.recall_mode === "semantic"
      ? `Recall: semantic (NEAR namespace, dimension ${index.measured_dimension ?? "?"}).`
      : "Recall: lexical. " + (index?.notice ?? "Semantic recall is unavailable until the NEAR embedding re-index completes.")}</p>
    <fieldset disabled={busy !== null} className="mt-2 grid gap-2 text-[10px] sm:grid-cols-2">
      <label className="flex gap-2"><input type="checkbox" aria-label="Enable NEAR TEE embedding slot" checked={draft.enabled} onChange={e => update("enabled", e.target.checked)} />Enable embedding slot</label>
      <label>Embedding model<select className={inputClass + " mt-1 w-full"} aria-label="NEAR TEE embedding model" value={draft.model} onChange={e => update("model", e.target.value)}>
        <option value="">Choose an allowlisted model</option>
        {NEAR_TEE_EMBEDDING_MODELS.map(model => <option key={model} value={model}>{model}</option>)}
      </select></label>
      <label>Maximum input tokens per request<input className={inputClass + " mt-1 w-full"} aria-label="NEAR TEE embedding maximum input tokens" type="number" min={1} max={32768} value={draft.maxInput} onChange={e => update("maxInput", e.target.value)} /></label>
      <label>Inputs per request<input className={inputClass + " mt-1 w-full"} aria-label="NEAR TEE embedding batch size" type="number" min={1} max={64} value={draft.batch} onChange={e => update("batch", e.target.value)} /></label>
      <label>Request timeout · seconds<input className={inputClass + " mt-1 w-full"} aria-label="NEAR TEE embedding request timeout" type="number" min={1} max={120} value={draft.timeout} onChange={e => update("timeout", e.target.value)} /></label>
      <label>Per-request reserve · USD<input className={inputClass + " mt-1 w-full"} aria-label="NEAR TEE embedding per-request reserve USD" inputMode="decimal" value={draft.reserveUsd} onChange={e => update("reserveUsd", e.target.value)} /></label>
      <label>Catalog receipt lifetime · hours<input className={inputClass + " mt-1 w-full"} aria-label="NEAR TEE embedding catalog receipt hours" type="number" min={1} max={24} value={draft.ttl} onChange={e => update("ttl", e.target.value)} /></label>
      {draft.enabled && !carryConsent && <label className="sm:col-span-2 flex items-start gap-2"><input type="checkbox" aria-label="Acknowledge NEAR TEE memory plaintext access" checked={acknowledged} onChange={e => setAcknowledged(e.target.checked)} /><span>I acknowledge that NEAR receives memory text in plaintext for embeddings. Seraph does not verify attestation and there is no end-to-end encryption.</span></label>}
    </fieldset>
    <div className="mt-2 flex flex-wrap gap-2 text-[10px]">
      <button type="button" disabled={busy !== null} onClick={() => void save()}>{busy === "save" ? "Saving…" : "Save embedding slot"}</button>
      <button type="button" disabled={busy !== null || !slot?.enabled || !slot.catalog_receipt_current || slot.evidence_required} onClick={() => void capabilityCheck()}>{busy === "capability" ? "Running capability check…" : "Run embedding capability check"}</button>
    </div>
    <div className="mt-2 grid gap-2 text-[10px] sm:grid-cols-2" aria-label="NEAR TEE embedding re-index">
      <label>Re-index budget · USD<input className={inputClass + " mt-1 w-full"} aria-label="Re-index budget USD" inputMode="decimal" value={budgetUsd} onChange={e => setBudgetUsd(e.target.value)} /></label>
      <label>Maximum memories this run<input className={inputClass + " mt-1 w-full"} aria-label="Re-index maximum memories" type="number" min={1} value={maxItems} onChange={e => setMaxItems(e.target.value)} /></label>
      <label className="sm:col-span-2 flex items-start gap-2"><input type="checkbox" aria-label="Acknowledge re-index memory egress" checked={reindexAck} onChange={e => setReindexAck(e.target.checked)} /><span>Send canonical memory text to NEAR in plaintext to rebuild the embedding index, within this budget.</span></label>
      {job && <p className="sm:col-span-2 text-[9px]" role="status" aria-label="Re-index progress">Re-index {readable(job.status)}: {job.indexed_items} of {job.planned_items ?? "?"} memories indexed, {job.skipped_existing} already current, spent ${usd(job.spent_microusd)}{job.budget_microusd ? ` of $${usd(job.budget_microusd)}` : ""}{job.reason_code ? " · " + readable(job.reason_code) : ""}.</p>}
    </div>
    {error && <div role="alert" className="mt-2 text-[10px] text-red-400">{error}</div>}
    {message && <div role="status" className="mt-2 text-[10px]">{message}</div>}
    <div className="mt-2 flex flex-wrap gap-2 text-[10px]">
      <button type="button" disabled={busy !== null || slot?.status !== "ready" || running} onClick={() => void startReindex()}>{busy === "reindex" ? "Starting…" : job && job.status !== "succeeded" ? "Resume re-index" : "Start re-index"}</button>
      <button type="button" disabled={busy !== null || !job || !["running", "interrupted"].includes(job.status)} onClick={() => void cancelReindex()}>{busy === "cancel" ? "Cancelling…" : "Cancel re-index"}</button>
    </div>
  </div>;
}
