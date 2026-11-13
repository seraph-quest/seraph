import { useEffect, useRef, useState } from "react";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import { NEAR_TEE_API_BASE, NEAR_TEE_DISCLOSURE, NEAR_TEE_TEXT_MODELS, normalizeModelFabricSettings } from "../../lib/modelFabric";
import type { ModelFabricSettingsStatus, NearTeeSetupInput, NearTeeSetupStatus, NearTeeSlotStatus } from "../../lib/modelFabric";

interface Props {
  setup?: NearTeeSetupStatus | null;
  stale: boolean;
  policyRevision?: number;
  policyRevoked?: boolean;
  laneCeilingMicrousd?: number | null;
  onSave: (payload: Record<string, unknown>) => Promise<ModelFabricSettingsStatus>;
  onRefreshed?: (next: ModelFabricSettingsStatus) => void;
}
interface Draft { enabled: boolean; model: string; maxInput: string; maxOutput: string; timeout: string; reserveUsd: string; ttl: string }

const CHECK_CAPABILITIES = ["text", "health", "latency_ms"] as const;
const inputClass = "min-w-0 border border-retro-text/20 bg-retro-bg px-1 py-0.5 text-retro-text disabled:opacity-40";

function usd(value: number) { return (value / 1_000_000).toFixed(6).replace(/0+$/, "").replace(/\.$/, ""); }
function draftFrom(slot?: NearTeeSlotStatus | null): Draft {
  return { enabled: slot?.enabled ?? false, model: slot?.model_id ?? "", maxInput: String(slot?.max_input_tokens ?? 8192),
    maxOutput: String(slot?.max_output_tokens ?? 1024), timeout: String(slot?.timeout_seconds ?? 30),
    reserveUsd: usd(slot?.request_cost_bound_microusd ?? 20_000), ttl: String(slot?.catalog_ttl_hours ?? 24) };
}
function money(value: string): number {
  if (!/^\d+(?:\.\d{1,6})?$/.test(value)) throw new Error("Per-request reserve requires a USD amount with at most six decimal places.");
  const [whole, fraction = ""] = value.split(".");
  const parsed = Number(whole) * 1_000_000 + Number(fraction.padEnd(6, "0"));
  if (!Number.isSafeInteger(parsed) || parsed < 1 || parsed > 1_000_000_000) throw new Error("Per-request reserve must be between $0.000001 and $1000.");
  return parsed;
}
function number(value: string, label: string, max: number, integer = true): number {
  const parsed = Number(value);
  if (!value.trim() || !Number.isFinite(parsed) || parsed < 1 || parsed > max || (integer && !Number.isInteger(parsed))) throw new Error(label + " must be between 1 and " + max + (integer ? " as a whole number." : "."));
  return parsed;
}
function readable(value: string | null | undefined) { return (value ?? "").replace(/_/g, " "); }

export function NearTeePanel({ setup, stale, policyRevision, policyRevoked = false, laneCeilingMicrousd, onSave, onRefreshed }: Props) {
  const slot = setup?.slots.text ?? null;
  const [revision, setRevision] = useState(policyRevision);
  const [draft, setDraft] = useState(() => draftFrom(slot));
  const [credential, setCredential] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const dirty = useRef(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    if (stale) return;
    if (policyRevision !== undefined) setRevision(policyRevision);
    setAcknowledged(false);
    if (!dirty.current) setDraft(draftFrom(setup?.slots.text));
  }, [setup, stale, policyRevision]);
  function update<K extends keyof Draft>(field: K, value: Draft[K]) {
    dirty.current = true;
    setDraft(current => ({ ...current, [field]: value }));
    setAcknowledged(false);
  }
  const unchanged = Boolean(slot && draft.enabled === slot.enabled && draft.model === slot.model_id
    && Number(draft.maxInput) === slot.max_input_tokens && Number(draft.maxOutput) === slot.max_output_tokens
    && Number(draft.timeout) === slot.timeout_seconds && Number(draft.ttl) === slot.catalog_ttl_hours
    && (() => { try { return money(draft.reserveUsd) === slot.request_cost_bound_microusd; } catch { return false; } })());
  const carryConsent = Boolean(slot?.enabled && slot.consent_current && !policyRevoked && unchanged);
  function safeError(reason: unknown, fallback: string) {
    const text = reason instanceof Error ? reason.message : fallback;
    return (credential ? text.split(credential).join("[redacted]") : text).slice(0, 512);
  }
  async function detail(response: Response) {
    try { const body = await response.json(); const value = body?.detail; return typeof value === "string" ? value : JSON.stringify(value ?? response.status); }
    catch { return String(response.status); }
  }
  async function save() {
    setBusy("save"); setError(null); setMessage(null);
    try {
      if (!Number.isSafeInteger(revision) || (revision ?? 0) < 1) throw new Error("Refresh current settings to obtain the policy revision before saving.");
      if (!laneCeilingMicrousd) throw new Error("Publish the shared inference lane ceiling first.");
      if (!(NEAR_TEE_TEXT_MODELS as readonly string[]).includes(draft.model)) throw new Error("Choose a text model from the NEAR TEE allowlist.");
      const reserve = money(draft.reserveUsd);
      if (reserve > laneCeilingMicrousd) throw new Error("Per-request reserve cannot exceed the shared lane ceiling.");
      if (draft.enabled && !carryConsent && !acknowledged) throw new Error("Acknowledge that NEAR receives requests in plaintext before enabling or changing this slot.");
      const value: NearTeeSetupInput = {
        schema_version: "seraph.near.tee.setup.v1", api_base: NEAR_TEE_API_BASE,
        slots: { text: {
          model_id: draft.model, enabled: draft.enabled,
          max_input_tokens: number(draft.maxInput, "Maximum input tokens", 1_048_576),
          max_output_tokens: number(draft.maxOutput, "Maximum output tokens", 65_536),
          timeout_seconds: number(draft.timeout, "Request timeout", 120, false),
          request_cost_bound_microusd: reserve,
          catalog_ttl_hours: number(draft.ttl, "Catalog receipt lifetime", 24),
          plaintext_provider_egress_acknowledged: draft.enabled && acknowledged,
        } },
        ...(credential.trim() ? { api_key: credential } : {}),
      };
      const next = await onSave({ expected_policy_revision: revision, near_tee: value });
      if (!mounted.current) return;
      setCredential("");
      dirty.current = false; setRevision(next.egress_revision); setAcknowledged(false);
      setDraft(draftFrom(next.near_tee?.slots.text));
      setMessage("NEAR TEE settings saved. Saving never calls NEAR; run the catalog check next.");
    } catch (reason) { if (mounted.current) setError(safeError(reason, "NEAR TEE settings could not be saved.")); }
    finally { if (mounted.current) setBusy(null); }
  }
  async function catalogCheck() {
    setBusy("catalog"); setError(null); setMessage(null);
    try {
      if (!Number.isSafeInteger(revision) || (revision ?? 0) < 1) throw new Error("Refresh current settings before the catalog check.");
      const response = await apiFetch(API_URL + "/api/settings/model-fabric/near-tee/catalog-check", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ expected_policy_revision: revision }),
      });
      if (!response.ok) throw new Error("Catalog check refused: " + await detail(response));
      const next = normalizeModelFabricSettings(await response.json());
      if (!mounted.current) return;
      if (next) { onRefreshed?.(next); setRevision(next.egress_revision); }
      const receipt = next?.near_tee?.slots.text?.catalog_receipt;
      setMessage(receipt?.outcome === "passed" ? "Catalog check passed. This reads NEAR's catalog only; it is not a capability check."
        : "Catalog check failed: " + readable(receipt?.reason_code ?? "unknown") + ". The slot stays blocked.");
    } catch (reason) { if (mounted.current) setError(safeError(reason, "Catalog check failed.")); }
    finally { if (mounted.current) setBusy(null); }
  }
  async function capabilityCheck() {
    setBusy("capability"); setError(null); setMessage(null);
    try {
      if (!slot) throw new Error("Save the text slot first.");
      for (const capability of CHECK_CAPABILITIES) {
        const response = await apiFetch(API_URL + "/api/settings/model-fabric/canary", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ profile_id: slot.profile_id, capability, timeout_seconds: slot.capability_check_timeout_seconds, proof_ttl_seconds: 86400 }),
        });
        if (!response.ok) throw new Error("Capability check refused (" + capability + "): " + await detail(response));
        const body = await response.json();
        if (body?.outcome !== "passed") throw new Error("Capability check failed (" + capability + "): " + readable(body?.error_code ?? "unknown"));
      }
      if (mounted.current) setMessage("Capability check passed (mechanics only; three billed NEAR requests). Refresh to see readiness.");
    } catch (reason) { if (mounted.current) setError(safeError(reason, "Capability check failed.")); }
    finally { if (mounted.current) setBusy(null); }
  }
  const receipt = slot?.catalog_receipt;
  return <section className="mt-2 border border-retro-text/10 px-2 py-2" aria-label="NEAR TEE settings">
    <div className="flex justify-between gap-2 text-[10px]"><span>NEAR TEE · text slot</span><span>{stale ? "stale retained" : readable(slot?.status ?? setup?.status ?? "disabled")}</span></div>
    <p className="mt-1 text-[9px] text-retro-text/60">{NEAR_TEE_DISCLOSURE}</p>
    <p className="mt-1 text-[9px] text-retro-text/60">Endpoint {NEAR_TEE_API_BASE}. Requests use the canonical model id with no aliasing. Saving and refreshing never call NEAR. Streaming and tools are not available yet.</p>
    {slot?.reason_code && <p className="mt-1 text-[9px]">Blocked: {readable(slot.reason_code)}</p>}
    <p className="mt-1 text-[9px]">{slot?.tee_hosting_label ?? (receipt ? `Catalog receipt ${receipt.outcome} at ${receipt.checked_at_iso ?? "unknown time"}${receipt.reason_code ? " · " + readable(receipt.reason_code) : ""}` : "No catalog receipt yet.")}</p>
    <fieldset disabled={busy !== null} className="mt-2 grid gap-2 text-[10px] sm:grid-cols-2">
      <label className="flex gap-2"><input type="checkbox" aria-label="Enable NEAR TEE text slot" checked={draft.enabled} onChange={e => update("enabled", e.target.checked)} />Enable text slot</label>
      <div>NEAR TEE key: {setup?.key_present ? "present" : "not configured"}</div>
      <label className="sm:col-span-2">NEAR TEE API key · write only<input className={inputClass + " mt-1 w-full"} aria-label="NEAR TEE API key" type="password" autoComplete="new-password" value={credential} onChange={e => { setCredential(e.target.value); dirty.current = true; }} placeholder="Blank keeps the existing key" /></label>
      <label className="sm:col-span-2">Text model<select className={inputClass + " mt-1 w-full"} aria-label="NEAR TEE text model" value={draft.model} onChange={e => update("model", e.target.value)}>
        <option value="">Choose an allowlisted model</option>
        {NEAR_TEE_TEXT_MODELS.map(model => <option key={model} value={model}>{model}</option>)}
      </select></label>
      <label>Maximum input tokens<input className={inputClass + " mt-1 w-full"} aria-label="NEAR TEE maximum input tokens" type="number" min={1} value={draft.maxInput} onChange={e => update("maxInput", e.target.value)} /></label>
      <label>Maximum output tokens<input className={inputClass + " mt-1 w-full"} aria-label="NEAR TEE maximum output tokens" type="number" min={1} value={draft.maxOutput} onChange={e => update("maxOutput", e.target.value)} /></label>
      <label>Request timeout · seconds<input className={inputClass + " mt-1 w-full"} aria-label="NEAR TEE request timeout" type="number" min={1} max={120} value={draft.timeout} onChange={e => update("timeout", e.target.value)} /></label>
      <label>Per-request reserve · USD<input className={inputClass + " mt-1 w-full"} aria-label="NEAR TEE per-request reserve USD" inputMode="decimal" value={draft.reserveUsd} onChange={e => update("reserveUsd", e.target.value)} /></label>
      <label>Catalog receipt lifetime · hours<input className={inputClass + " mt-1 w-full"} aria-label="NEAR TEE catalog receipt hours" type="number" min={1} max={24} value={draft.ttl} onChange={e => update("ttl", e.target.value)} /></label>
      <p className="sm:col-span-2 text-[9px] text-retro-text/60">Uses the shared inference lane ceiling{laneCeilingMicrousd ? ` ($${usd(laneCeilingMicrousd)})` : " (not published yet)"}. The reserve must cover maximum input and output at NEAR's catalog prices.</p>
      {draft.enabled && !carryConsent && <label className="sm:col-span-2 flex items-start gap-2"><input type="checkbox" aria-label="Acknowledge NEAR TEE plaintext access" checked={acknowledged} onChange={e => setAcknowledged(e.target.checked)} /><span>I acknowledge that NEAR receives these requests in plaintext. Seraph does not verify attestation and there is no end-to-end encryption.</span></label>}
      {draft.enabled && carryConsent && <p className="sm:col-span-2 text-[9px]">Current plaintext consent applies to this unchanged slot.</p>}
    </fieldset>
    {error && <div role="alert" className="mt-2 text-[10px] text-red-400">{error}</div>}
    {message && <div role="status" className="mt-2 text-[10px]">{message}</div>}
    <div className="mt-2 flex flex-wrap gap-2 text-[10px]">
      <button type="button" disabled={busy !== null} onClick={() => void save()}>{busy === "save" ? "Saving…" : "Save NEAR TEE settings"}</button>
      <button type="button" disabled={busy !== null || !slot?.enabled} onClick={() => void catalogCheck()}>{busy === "catalog" ? "Checking catalog…" : "Run catalog check"}</button>
      <button type="button" disabled={busy !== null || !slot?.enabled || !slot.catalog_receipt_current} onClick={() => void capabilityCheck()}>{busy === "capability" ? "Running capability check…" : "Run capability check"}</button>
    </div>
  </section>;
}
