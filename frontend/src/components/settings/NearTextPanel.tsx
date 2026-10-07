import { useEffect, useRef, useState } from "react";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import { NEAR_TEXT_API_BASE, NEAR_TEXT_DISCLOSURE, NEAR_TEXT_MODEL, NEAR_TEXT_PROFILE, normalizeModelFabricSettings } from "../../lib/modelFabric";
import type { ModelFabricSettingsStatus, NearTextSetupInput, NearTextSetupStatus } from "../../lib/modelFabric";

interface Props {
  setup?: NearTextSetupStatus | null;
  stale: boolean;
  policyRevision?: number;
  policyRevoked?: boolean;
  sharedCeilingMicrousd?: number | null;
  sharedCeilingLocked?: boolean;
  onSave: (payload: Record<string, unknown>) => Promise<ModelFabricSettingsStatus>;
}
interface Draft { enabled: boolean; maxOutput: string; timeout: string; reserveUsd: string; ceilingUsd: string }
function usd(value: number) { return (value / 1_000_000).toFixed(6).replace(/0+$/, "").replace(/\.$/, ""); }
function draftFrom(setup?: NearTextSetupStatus | null, ceiling?: number | null): Draft {
  return { enabled: setup?.enabled ?? false, maxOutput: String(setup?.max_output_tokens ?? 1024),
    timeout: String(setup?.timeout_seconds ?? 45), reserveUsd: usd(setup?.request_cost_bound_microusd ?? 1000),
    ceilingUsd: ceiling ? usd(ceiling) : setup ? usd(setup.spend_ceiling_microusd) : "" };
}
function money(value: string, label: string): number {
  if (!/^\d+(?:\.\d{1,6})?$/.test(value)) throw new Error(label + " requires a USD amount with at most six decimal places.");
  const [whole, fraction = ""] = value.split(".");
  const parsed = Number(whole) * 1_000_000 + Number(fraction.padEnd(6, "0"));
  if (!Number.isSafeInteger(parsed) || parsed < 1 || parsed > 1_000_000_000) throw new Error(label + " must be between $0.000001 and $1000.");
  return parsed;
}
function sameMoney(value: string, amount: number) {
  try { return money(value, "Per-request reserve") === amount; } catch { return false; }
}
function number(value: string, label: string, max: number, integer = false): number {
  const parsed = Number(value);
  if (!value.trim() || !Number.isFinite(parsed) || parsed < 1 || parsed > max || (integer && !Number.isInteger(parsed))) throw new Error(label + " must be between 1 and " + max + (integer ? " as a whole number." : "."));
  return parsed;
}
const inputClass = "min-w-0 border border-retro-text/20 bg-retro-bg px-1 py-0.5 text-retro-text disabled:opacity-40";

export function NearTextPanel({ setup, stale, policyRevision, policyRevoked = false, sharedCeilingMicrousd, sharedCeilingLocked = false, onSave }: Props) {
  const [metadata, setMetadata] = useState(setup);
  const [revision, setRevision] = useState(policyRevision);
  const [revoked, setRevoked] = useState(policyRevoked);
  const [retained, setRetained] = useState(stale);
  const [draft, setDraft] = useState(() => draftFrom(setup, sharedCeilingMicrousd));
  const [credential, setCredential] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const dirty = useRef(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    if (stale) { setRetained(true); return; }
    if (policyRevision !== undefined) setRevision(policyRevision);
    setRevoked(policyRevoked);
    setAcknowledged(false);
    setMetadata(setup);
    setRetained(false);
    if (!dirty.current) setDraft(draftFrom(setup, sharedCeilingMicrousd));
    else if (sharedCeilingLocked && sharedCeilingMicrousd) setDraft(current => ({ ...current, ceilingUsd: usd(sharedCeilingMicrousd) }));
  }, [setup, stale, policyRevision, policyRevoked, sharedCeilingMicrousd, sharedCeilingLocked]);
  function update<K extends keyof Draft>(field: K, value: Draft[K]) {
    dirty.current = true;
    setDraft(current => ({ ...current, [field]: value }));
    if (field !== "ceilingUsd") setAcknowledged(false);
  }
  const carryConsent = Boolean(metadata?.enabled && metadata.consent_current && !revoked && !credential.trim()
    && draft.enabled === metadata.enabled && Number(draft.maxOutput) === metadata.max_output_tokens
    && Number(draft.timeout) === metadata.timeout_seconds && sameMoney(draft.reserveUsd, metadata.request_cost_bound_microusd));
  function safeError(reason: unknown) {
    const text = reason instanceof Error ? reason.message : "NEAR settings could not be saved.";
    return (credential ? text.split(credential).join("[redacted]") : text).slice(0, 512);
  }
  async function refresh() {
    setRefreshing(true); setMessage(null);
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 5000);
    try {
      const response = await apiFetch(API_URL + "/api/settings/model-fabric", { signal: controller.signal });
      if (!response.ok) throw new Error("Settings refresh failed: " + response.status);
      const next = normalizeModelFabricSettings(await response.json());
      if (!next || next.near_text_metadata_unavailable || !Number.isSafeInteger(next.egress_revision) || (next.egress_revision ?? 0) < 1) throw new Error("Current NEAR settings are unavailable; edits retained.");
      if (!mounted.current) return;
      setMetadata(next.near_text); setRevision(next.egress_revision); setRevoked(next.egress_revoked ?? false);
      setRetained(false); setAcknowledged(false);
      if (sharedCeilingLocked && next.openrouter_setup?.spend_ceiling_microusd) setDraft(current => ({ ...current, ceilingUsd: usd(next.openrouter_setup!.spend_ceiling_microusd!) }));
      setMessage("Current settings loaded; edits retained. Review plaintext access before saving.");
    } catch (reason) { if (mounted.current) { setRetained(true); setMessage(safeError(reason)); } }
    finally { window.clearTimeout(timeout); if (mounted.current) setRefreshing(false); }
  }
  async function save() {
    setBusy(true); setError(null); setMessage(null);
    try {
      if (!Number.isSafeInteger(revision) || (revision ?? 0) < 1) throw new Error("Refresh current settings to obtain the policy revision before saving.");
      const ceiling = money(draft.ceilingUsd, "Shared deployment ceiling");
      const reserve = money(draft.reserveUsd, "Per-request reserve");
      if (reserve > ceiling) throw new Error("Per-request reserve cannot exceed the shared deployment ceiling.");
      if (draft.enabled && !carryConsent && !acknowledged) throw new Error("Acknowledge that NEAR receives plaintext before enabling or changing this route.");
      const value: NearTextSetupInput = {
        schema_version: "seraph.near.text.v1", enabled: draft.enabled, profile_id: NEAR_TEXT_PROFILE,
        model_id: NEAR_TEXT_MODEL, api_base: NEAR_TEXT_API_BASE,
        max_output_tokens: number(draft.maxOutput, "Maximum answer tokens", 1024, true),
        timeout_seconds: number(draft.timeout, "Request timeout", 45),
        request_cost_bound_microusd: reserve, spend_ceiling_microusd: ceiling,
        plaintext_provider_egress_acknowledged: draft.enabled && acknowledged,
        ...(credential.trim() ? { api_key: credential } : {}),
      };
      const next = await onSave({ expected_policy_revision: revision, near_text: value });
      if (!mounted.current) return;
      setCredential("");
      if (!next.near_text || next.near_text_metadata_unavailable) throw new Error("Settings saved without complete NEAR metadata. Refresh to inspect the result.");
      dirty.current = false; setMetadata(next.near_text); setRevision(next.egress_revision);
      setRevoked(next.egress_revoked ?? false); setRetained(false); setAcknowledged(false);
      setDraft(draftFrom(next.near_text, next.openrouter_setup?.spend_ceiling_microusd));
      setMessage("NEAR settings saved. Configuration does not prove provider availability.");
    } catch (reason) { if (mounted.current) setError(safeError(reason)); }
    finally { if (mounted.current) setBusy(false); }
  }
  return <section className="mt-2 border border-retro-text/10 px-2 py-2" aria-label="NEAR text settings">
    <div className="flex justify-between gap-2 text-[10px]"><span>NEAR text · optional</span><span>{retained ? "stale retained" : (metadata?.status ?? "disabled").replace(/_/g, " ")}</span></div>
    <p className="mt-1 text-[9px] text-retro-text/60">{NEAR_TEXT_DISCLOSURE} Private artifacts protect local access; they do not hide the question from NEAR. TLS is used; TEE verification and end-to-end encryption are not provided.</p>
    <p className="mt-1 text-[9px] text-retro-text/60">Fixed model {NEAR_TEXT_MODEL} · {NEAR_TEXT_API_BASE}. Saving and refreshing never call a provider. Configured means local settings are ready.</p>
    {metadata?.reason_code && <p className="mt-1 text-[9px]">{metadata.reason_code}</p>}
    <fieldset disabled={busy} className="mt-2 grid gap-2 text-[10px] sm:grid-cols-2">
      <label className="flex gap-2"><input type="checkbox" aria-label="Enable NEAR text" checked={draft.enabled} onChange={e => update("enabled", e.target.checked)} />Enable NEAR text</label>
      <div>NEAR key: {metadata?.key_present ? "present" : "not configured"}</div>
      <label className="sm:col-span-2">NEAR API key · write only<input className={inputClass + " mt-1 w-full"} aria-label="NEAR API key" type="password" autoComplete="new-password" value={credential} onChange={e => { setCredential(e.target.value); setAcknowledged(false); dirty.current = true; }} placeholder="Blank keeps the existing NEAR key" /></label>
      <label>Maximum answer tokens<input className={inputClass + " mt-1 w-full"} aria-label="NEAR maximum answer tokens" type="number" min={1} max={1024} value={draft.maxOutput} onChange={e => update("maxOutput", e.target.value)} /></label>
      <label>Request timeout · seconds<input className={inputClass + " mt-1 w-full"} aria-label="NEAR request timeout" type="number" min={1} max={45} value={draft.timeout} onChange={e => update("timeout", e.target.value)} /></label>
      <label>Per-request reserve · USD<input className={inputClass + " mt-1 w-full"} aria-label="NEAR per-request reserve USD" inputMode="decimal" value={draft.reserveUsd} onChange={e => update("reserveUsd", e.target.value)} /></label>
      <label>Shared deployment ceiling · USD<input className={inputClass + " mt-1 w-full"} aria-label="NEAR shared deployment ceiling USD" inputMode="decimal" readOnly={sharedCeilingLocked} value={draft.ceilingUsd} onChange={e => update("ceilingUsd", e.target.value)} /></label>
      <p className="sm:col-span-2 text-[9px] text-retro-text/60">OpenRouter and NEAR use one shared budget. {sharedCeilingLocked ? "Change that ceiling in OpenRouter settings." : "This funds the shared deployment budget; no OpenRouter key is required."} A reserve is a local admission hold, not a provider-enforced spending maximum.</p>
      {draft.enabled && !carryConsent && <label className="sm:col-span-2 flex items-start gap-2"><input type="checkbox" aria-label="Acknowledge NEAR plaintext access" checked={acknowledged} onChange={e => setAcknowledged(e.target.checked)} /><span>I acknowledge that NEAR receives my question in plaintext over HTTPS. Global revocation affects both OpenRouter and NEAR.</span></label>}
      {draft.enabled && carryConsent && <p className="sm:col-span-2 text-[9px]">Current plaintext consent applies to this unchanged route.</p>}
    </fieldset>
    {error && <div role="alert" className="mt-2 text-[10px] text-red-400">{error}</div>}
    {message && <div role="status" className="mt-2 text-[10px]">{message}</div>}
    <div className="mt-2 flex flex-wrap gap-2 text-[10px]"><button type="button" disabled={busy || refreshing} onClick={() => void refresh()}>Refresh current NEAR settings</button><button type="button" disabled={busy} onClick={() => void save()}>{busy ? "Saving…" : revoked && draft.enabled ? "Review and re-grant NEAR text" : "Save NEAR text settings"}</button></div>
  </section>;
}
