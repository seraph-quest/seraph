import { useEffect, useRef, useState } from "react";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import {
  NEAR_TEE_API_BASE, NEAR_TEE_DISCLOSURE, NEAR_TEE_SCREENSHOT_DISCLOSURE, NEAR_TEE_TEXT_MODELS, NEAR_TEE_VISION_MODELS,
  normalizeModelFabricSettings,
} from "../../lib/modelFabric";
import type { ModelFabricSettingsStatus, NearTeeSetupInput, NearTeeSetupStatus, NearTeeSlotName, NearTeeSlotStatus } from "../../lib/modelFabric";
import { NearTeeEmbeddingSection } from "./NearTeeEmbeddingSection";

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

interface SlotSpec {
  name: NearTeeSlotName;
  title: string;
  /** Prefix for accessible labels; the text slot keeps its original labels. */
  prefix: string;
  models: readonly string[];
  checks: readonly string[];
  defaults: { maxInput: number; maxOutput: number };
}
const SLOTS: readonly SlotSpec[] = [
  { name: "text", title: "text slot", prefix: "NEAR TEE", models: NEAR_TEE_TEXT_MODELS,
    checks: ["text", "health", "latency_ms"], defaults: { maxInput: 8192, maxOutput: 1024 } },
  { name: "vision", title: "vision slot · screenshots", prefix: "NEAR TEE vision", models: NEAR_TEE_VISION_MODELS,
    checks: ["vision", "structured_output", "health", "latency_ms"], defaults: { maxInput: 16384, maxOutput: 2048 } },
];
/** Default background job budget (120 s) minus the 16 s billing window and a 10 s margin. */
const DEFAULT_VISION_MAX_TIMEOUT = 94;
/** Fallback for the backend's reasoning-model output guidance (#1167). */
const DEFAULT_REASONING_MAX_OUTPUT = 4096;
const inputClass = "min-w-0 border border-retro-text/20 bg-retro-bg px-1 py-0.5 text-retro-text disabled:opacity-40";

function usd(value: number) { return (value / 1_000_000).toFixed(6).replace(/0+$/, "").replace(/\.$/, ""); }
function draftFrom(spec: SlotSpec, slot?: NearTeeSlotStatus | null): Draft {
  return { enabled: slot?.enabled ?? false, model: slot?.model_id ?? "", maxInput: String(slot?.max_input_tokens ?? spec.defaults.maxInput),
    maxOutput: String(slot?.max_output_tokens ?? spec.defaults.maxOutput), timeout: String(slot?.timeout_seconds ?? 30),
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
async function detail(response: Response) {
  try { const body = await response.json(); const value = body?.detail; return typeof value === "string" ? value : JSON.stringify(value ?? response.status); }
  catch { return String(response.status); }
}

export function NearTeePanel({ setup, stale, policyRevision, policyRevoked = false, laneCeilingMicrousd, onSave, onRefreshed }: Props) {
  const [revision, setRevision] = useState(policyRevision);
  const [credential, setCredential] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => { if (!stale && policyRevision !== undefined) setRevision(policyRevision); }, [setup, stale, policyRevision]);
  function safeError(reason: unknown, fallback: string) {
    const text = reason instanceof Error ? reason.message : fallback;
    return (credential ? text.split(credential).join("[redacted]") : text).slice(0, 512);
  }
  const anyEnabled = Boolean(setup?.slots.text?.enabled || setup?.slots.vision?.enabled || setup?.slots.embedding?.enabled);
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
      const slots = next?.near_tee?.slots;
      const checked = ([["text", slots?.text], ["vision", slots?.vision], ["embedding", slots?.embedding]] as const)
        .filter(([, item]) => item?.enabled && item.catalog_receipt);
      setMessage(checked.length === 0 ? "Catalog check returned no receipt." : checked.map(([name, item]) =>
        item?.catalog_receipt?.outcome === "passed" ? `${name} slot: catalog check passed.`
          : `${name} slot: catalog check failed (${readable(item?.catalog_receipt?.reason_code ?? "unknown")}); the slot stays blocked.`,
      ).join(" ") + " This reads NEAR's catalog only; it is not a capability check.");
    } catch (reason) { if (mounted.current) setError(safeError(reason, "Catalog check failed.")); }
    finally { if (mounted.current) setBusy(null); }
  }
  return <section className="mt-2 border border-retro-text/10 px-2 py-2" aria-label="NEAR TEE settings">
    <div className="flex justify-between gap-2 text-[10px]"><span>NEAR TEE</span><span>{stale ? "stale retained" : readable(setup?.status ?? "disabled")}</span></div>
    <p className="mt-1 text-[9px] text-retro-text/60">{NEAR_TEE_DISCLOSURE}</p>
    <p className="mt-1 text-[9px] text-retro-text/60">Endpoint {NEAR_TEE_API_BASE}. Requests use the canonical model id with no aliasing. Saving and refreshing never call NEAR. Streaming chat and agent tools use the text slot once its streaming and tool_use capability checks pass.</p>
    <fieldset disabled={busy !== null} className="mt-2 grid gap-2 text-[10px] sm:grid-cols-2">
      <div className="sm:col-span-2">NEAR TEE key: {setup?.key_present ? "present" : "not configured"}</div>
      <label className="sm:col-span-2">NEAR TEE API key · write only<input className={inputClass + " mt-1 w-full"} aria-label="NEAR TEE API key" type="password" autoComplete="new-password" value={credential} onChange={e => setCredential(e.target.value)} placeholder="Blank keeps the existing key" /></label>
    </fieldset>
    {SLOTS.map(spec => <SlotEditor key={spec.name} spec={spec} slot={setup?.slots[spec.name] ?? null} stale={stale}
      revision={revision} setRevision={setRevision} policyRevoked={policyRevoked} laneCeilingMicrousd={laneCeilingMicrousd}
      maxTimeout={spec.name === "vision" ? Math.floor(setup?.vision_max_timeout_seconds ?? DEFAULT_VISION_MAX_TIMEOUT) : 120}
      reasoningMaxOutput={setup?.reasoning_recommended_max_output_tokens ?? DEFAULT_REASONING_MAX_OUTPUT}
      credential={credential} clearCredential={() => setCredential("")} busy={busy} setBusy={setBusy} setError={setError}
      setMessage={setMessage} safeError={safeError} mounted={mounted} onSave={onSave} />)}
    {error && <div role="alert" className="mt-2 text-[10px] text-red-400">{error}</div>}
    {message && <div role="status" className="mt-2 text-[10px]">{message}</div>}
    <div className="mt-2 flex flex-wrap gap-2 text-[10px]">
      <button type="button" disabled={busy !== null || !anyEnabled} onClick={() => void catalogCheck()}>{busy === "catalog" ? "Checking catalog…" : "Run catalog check"}</button>
    </div>
    <NearTeeEmbeddingSection setup={setup} stale={stale} revision={revision} policyRevoked={policyRevoked}
      laneCeilingMicrousd={laneCeilingMicrousd} onSave={onSave} onRefreshed={onRefreshed} />
  </section>;
}

interface SlotEditorProps {
  spec: SlotSpec;
  slot: NearTeeSlotStatus | null;
  stale: boolean;
  revision?: number;
  setRevision: (revision: number | undefined) => void;
  policyRevoked: boolean;
  laneCeilingMicrousd?: number | null;
  maxTimeout: number;
  reasoningMaxOutput: number;
  credential: string;
  clearCredential: () => void;
  busy: string | null;
  setBusy: (value: string | null) => void;
  setError: (value: string | null) => void;
  setMessage: (value: string | null) => void;
  safeError: (reason: unknown, fallback: string) => string;
  mounted: { current: boolean };
  onSave: (payload: Record<string, unknown>) => Promise<ModelFabricSettingsStatus>;
}

function SlotEditor({ spec, slot, stale, revision, setRevision, policyRevoked, laneCeilingMicrousd, maxTimeout, reasoningMaxOutput, credential, clearCredential,
  busy, setBusy, setError, setMessage, safeError, mounted, onSave }: SlotEditorProps) {
  const vision = spec.name === "vision";
  const [draft, setDraft] = useState(() => draftFrom(spec, slot));
  const [acknowledged, setAcknowledged] = useState(false);
  const [screenshotAcknowledged, setScreenshotAcknowledged] = useState(false);
  const dirty = useRef(false);
  useEffect(() => {
    if (stale) return;
    setAcknowledged(false); setScreenshotAcknowledged(false);
    if (!dirty.current) setDraft(draftFrom(spec, slot));
  }, [slot, stale, spec]);
  function update<K extends keyof Draft>(field: K, value: Draft[K]) {
    dirty.current = true;
    setDraft(current => ({ ...current, [field]: value }));
    setAcknowledged(false); setScreenshotAcknowledged(false);
  }
  const unchanged = Boolean(slot && draft.enabled === slot.enabled && draft.model === slot.model_id
    && Number(draft.maxInput) === slot.max_input_tokens && Number(draft.maxOutput) === slot.max_output_tokens
    && Number(draft.timeout) === slot.timeout_seconds && Number(draft.ttl) === slot.catalog_ttl_hours
    && (() => { try { return money(draft.reserveUsd) === slot.request_cost_bound_microusd; } catch { return false; } })());
  const carryConsent = Boolean(slot?.enabled && slot.consent_current && !policyRevoked && unchanged);
  const saveLabel = vision ? "Save NEAR TEE vision slot" : "Save NEAR TEE settings";
  async function save(withdrawScreenshots = false) {
    setBusy("save:" + spec.name); setError(null); setMessage(null);
    try {
      if (!Number.isSafeInteger(revision) || (revision ?? 0) < 1) throw new Error("Refresh current settings to obtain the policy revision before saving.");
      if (!laneCeilingMicrousd) throw new Error("Publish the shared inference lane ceiling first.");
      if (!spec.models.includes(draft.model)) throw new Error(`Choose a ${spec.name} model from the NEAR TEE allowlist.`);
      const reserve = money(draft.reserveUsd);
      if (reserve > laneCeilingMicrousd) throw new Error("Per-request reserve cannot exceed the shared lane ceiling.");
      if (!withdrawScreenshots && draft.enabled && !carryConsent && !acknowledged) throw new Error("Acknowledge that NEAR receives requests in plaintext before enabling or changing this slot.");
      if (vision && !withdrawScreenshots && draft.enabled && !carryConsent && !screenshotAcknowledged) throw new Error("Acknowledge that screenshots are sent to NEAR in plaintext before enabling or changing the vision slot.");
      const slotInput = {
        model_id: draft.model, enabled: draft.enabled,
        max_input_tokens: number(draft.maxInput, "Maximum input tokens", 1_048_576),
        max_output_tokens: number(draft.maxOutput, "Maximum output tokens", 65_536),
        timeout_seconds: number(draft.timeout, vision ? "Request timeout (vision: background job budget)" : "Request timeout", maxTimeout, false),
        request_cost_bound_microusd: reserve,
        catalog_ttl_hours: number(draft.ttl, "Catalog receipt lifetime", 24),
        plaintext_provider_egress_acknowledged: draft.enabled && acknowledged,
        // Sent only to grant (true) or to withdraw (false); omitted, an
        // unchanged slot keeps its current screenshot consent.
        ...(vision && withdrawScreenshots ? { screenshot_plaintext_egress_acknowledged: false }
          : vision && draft.enabled && screenshotAcknowledged ? { screenshot_plaintext_egress_acknowledged: true } : {}),
      };
      // Only this slot is sent; the backend keeps the other stored slot.
      const value: NearTeeSetupInput = {
        schema_version: "seraph.near.tee.setup.v1", api_base: NEAR_TEE_API_BASE,
        slots: vision ? { vision: slotInput } : { text: slotInput },
        ...(credential.trim() ? { api_key: credential } : {}),
      };
      const next = await onSave({ expected_policy_revision: revision, near_tee: value });
      if (!mounted.current) return;
      clearCredential();
      dirty.current = false; setRevision(next.egress_revision); setAcknowledged(false); setScreenshotAcknowledged(false);
      setDraft(draftFrom(spec, next.near_tee?.slots[spec.name]));
      setMessage(withdrawScreenshots ? "Screenshot consent withdrawn; the vision slot is blocked until you consent again."
        : `NEAR TEE ${spec.name} slot saved. Saving never calls NEAR; run the catalog check next.`);
    } catch (reason) { if (mounted.current) setError(safeError(reason, "NEAR TEE settings could not be saved.")); }
    finally { if (mounted.current) setBusy(null); }
  }
  async function capabilityCheck() {
    setBusy("capability:" + spec.name); setError(null); setMessage(null);
    try {
      if (!slot) throw new Error(`Save the ${spec.name} slot first.`);
      for (const capability of spec.checks) {
        const response = await apiFetch(API_URL + "/api/settings/model-fabric/canary", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ profile_id: slot.profile_id, capability, timeout_seconds: slot.capability_check_timeout_seconds, proof_ttl_seconds: 86400 }),
        });
        if (!response.ok) throw new Error("Capability check refused (" + capability + "): " + await detail(response));
        const body = await response.json();
        if (body?.outcome !== "passed") throw new Error("Capability check failed (" + capability + "): " + readable(body?.error_code ?? "unknown"));
      }
      if (mounted.current) setMessage(`Capability check passed (mechanics only; ${spec.checks.length} billed NEAR requests). Refresh to see readiness.`);
    } catch (reason) { if (mounted.current) setError(safeError(reason, "Capability check failed.")); }
    finally { if (mounted.current) setBusy(null); }
  }
  /** One structured_output check (#1167): the text slot's JSON callers, or the vision json_schema mode. */
  async function structuredCheck(mode?: "json_schema" | "prompt_json") {
    setBusy("structured:" + spec.name); setError(null); setMessage(null);
    try {
      if (!slot) throw new Error(`Save the ${spec.name} slot first.`);
      const response = await apiFetch(API_URL + "/api/settings/model-fabric/canary", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ profile_id: slot.profile_id, capability: "structured_output", timeout_seconds: slot.capability_check_timeout_seconds,
          proof_ttl_seconds: 86400, ...(vision && mode ? { structured_mode: mode } : {}) }),
      });
      if (!response.ok) throw new Error("Capability check refused (structured_output): " + await detail(response));
      const body = await response.json();
      if (body?.outcome !== "passed") throw new Error("Capability check failed (structured_output): " + readable(body?.error_code ?? "unknown")
        + (mode === "json_schema" ? ". JSON schema requests are withdrawn; the vision slot stays blocked until a prompt-JSON check passes (Run vision capability check)." : "."));
      if (mounted.current) setMessage(mode === "json_schema" ? "JSON-schema structured output check passed (one billed NEAR request). Refresh to see the request mode."
        : mode === "prompt_json" ? "Prompt-JSON structured output check passed (one billed NEAR request). Refresh to see the request mode."
        : "Structured output check passed (one billed NEAR request). Refresh to see readiness.");
    } catch (reason) { if (mounted.current) setError(safeError(reason, "Capability check failed.")); }
    finally { if (mounted.current) setBusy(null); }
  }
  const receipt = slot?.catalog_receipt;
  const passedReceipt = receipt?.outcome === "passed";
  const structuredBasis = slot?.structured_output_basis ?? (receipt?.structured_outputs ? "catalog" : null);
  const reasoningModel = Boolean(passedReceipt && receipt?.features?.includes("reasoning"));
  // Reserve (microUSD) a reasoning-sized maximum output needs at the receipt's catalog prices (#1167).
  const reasoningReserveMicrousd = (() => {
    const input = Number(receipt?.input_price_usd_per_million), output = Number(receipt?.output_price_usd_per_million);
    const image = vision ? Number(receipt?.image_price_usd ?? 0) : 0;
    const maxInput = Number(draft.maxInput);
    if (!receipt?.input_price_usd_per_million || !receipt?.output_price_usd_per_million
      || ![input, output, image, maxInput].every(Number.isFinite)) return null;
    return Math.ceil(maxInput * input + reasoningMaxOutput * output + image * 1_000_000);
  })();
  const label = (text: string) => spec.prefix + " " + text;
  return <div className="mt-3 border-t border-retro-text/10 pt-2" aria-label={"NEAR TEE " + spec.title}>
    <div className="flex justify-between gap-2 text-[10px]"><span>NEAR TEE · {spec.title}</span><span>{stale ? "stale retained" : readable(slot?.status ?? "disabled")}</span></div>
    {vision && <p className="mt-1 text-[9px] text-retro-text/60">{NEAR_TEE_SCREENSHOT_DISCLOSURE} The daemon makes no cloud call: it uploads captures to this backend, which analyses them on this slot.</p>}
    {slot?.reason_code && <p className="mt-1 text-[9px]">Blocked: {readable(slot.reason_code)}</p>}
    <p className="mt-1 text-[9px]">{slot?.tee_hosting_label ?? (receipt ? `Catalog receipt ${receipt.outcome} at ${receipt.checked_at_iso ?? "unknown time"}${receipt.reason_code ? " · " + readable(receipt.reason_code) : ""}` : "No catalog receipt yet.")}</p>
    {passedReceipt && receipt?.readiness_basis === "absent_flag_attested" && <p className="mt-1 text-[9px]">Readiness inferred: NEAR's catalog omits isReady; the record is vllm, attestation-supported and verifiable. An explicit not-ready flag would block.</p>}
    {vision && passedReceipt && <p className="mt-1 text-[9px]">{structuredBasis === "catalog" ? "Structured outputs listed in the catalog: JSON schema requests."
      : structuredBasis === "capability_proof" ? "Catalog omits structured outputs; a current capability check established JSON schema requests."
      : "No structured outputs in the catalog and no current JSON-schema check: prompt-JSON requests, strictly validated."}</p>}
    {!vision && passedReceipt && slot?.structured_output_capability && <p className="mt-1 text-[9px]">{slot.structured_output_capability === "ready"
      ? "Structured output (JSON callers): ready, established by a current capability check."
      : `Structured output (JSON callers): unavailable until a structured output check passes (${readable(slot.structured_output_capability)}).`}</p>}
    {reasoningModel && Number(draft.maxOutput) < reasoningMaxOutput && <p className="mt-1 text-[9px]">Reasoning model: reasoning tokens count against the maximum output. Use at least {reasoningMaxOutput} so answers are not cut off (near tee output truncated).{reasoningReserveMicrousd !== null ? ` At NEAR's catalog prices that needs a per-request reserve of at least $${usd(reasoningReserveMicrousd)} with the current maximum input (reserve now $${draft.reserveUsd}).` : ""}</p>}
    <fieldset disabled={busy !== null} className="mt-2 grid gap-2 text-[10px] sm:grid-cols-2">
      <label className="flex gap-2"><input type="checkbox" aria-label={`Enable NEAR TEE ${spec.name} slot`} checked={draft.enabled} onChange={e => update("enabled", e.target.checked)} />Enable {spec.name} slot</label>
      <label className="sm:col-span-2">{vision ? "Vision model" : "Text model"}<select className={inputClass + " mt-1 w-full"} aria-label={`NEAR TEE ${spec.name} model`} value={draft.model} onChange={e => update("model", e.target.value)}>
        <option value="">Choose an allowlisted model</option>
        {spec.models.map(model => <option key={model} value={model}>{model}</option>)}
      </select></label>
      <label>Maximum input tokens{vision ? " · image counted conservatively" : ""}<input className={inputClass + " mt-1 w-full"} aria-label={label("maximum input tokens")} type="number" min={1} value={draft.maxInput} onChange={e => update("maxInput", e.target.value)} /></label>
      <label>Maximum output tokens<input className={inputClass + " mt-1 w-full"} aria-label={label("maximum output tokens")} type="number" min={1} value={draft.maxOutput} onChange={e => update("maxOutput", e.target.value)} /></label>
      <label>Request timeout · seconds{vision ? ` · at most ${maxTimeout} (background job budget)` : ""}<input className={inputClass + " mt-1 w-full"} aria-label={label("request timeout")} type="number" min={1} max={maxTimeout} value={draft.timeout} onChange={e => update("timeout", e.target.value)} /></label>
      <label>Per-request reserve · USD<input className={inputClass + " mt-1 w-full"} aria-label={label("per-request reserve USD")} inputMode="decimal" value={draft.reserveUsd} onChange={e => update("reserveUsd", e.target.value)} /></label>
      <label>Catalog receipt lifetime · hours<input className={inputClass + " mt-1 w-full"} aria-label={label("catalog receipt hours")} type="number" min={1} max={24} value={draft.ttl} onChange={e => update("ttl", e.target.value)} /></label>
      <p className="sm:col-span-2 text-[9px] text-retro-text/60">Uses the shared inference lane ceiling{laneCeilingMicrousd ? ` ($${usd(laneCeilingMicrousd)})` : " (not published yet)"}. The reserve must cover maximum input and output{vision ? " and the image" : ""} at NEAR's catalog prices.</p>
      {draft.enabled && !carryConsent && <label className="sm:col-span-2 flex items-start gap-2"><input type="checkbox" aria-label={"Acknowledge " + label("plaintext access")} checked={acknowledged} onChange={e => setAcknowledged(e.target.checked)} /><span>I acknowledge that NEAR receives these requests in plaintext. Seraph does not verify attestation and there is no end-to-end encryption.</span></label>}
      {vision && draft.enabled && !carryConsent && <label className="sm:col-span-2 flex items-start gap-2"><input type="checkbox" aria-label="Acknowledge screenshots sent to NEAR in plaintext" checked={screenshotAcknowledged} onChange={e => setScreenshotAcknowledged(e.target.checked)} /><span>I consent to sending my screenshots to the NEAR gateway in plaintext for analysis. This is separate from the text slot consent.</span></label>}
      {draft.enabled && carryConsent && <p className="sm:col-span-2 text-[9px]">Current plaintext consent applies to this unchanged slot.</p>}
    </fieldset>
    <div className="mt-2 flex flex-wrap gap-2 text-[10px]">
      <button type="button" disabled={busy !== null} onClick={() => void save()}>{busy === "save:" + spec.name ? "Saving…" : saveLabel}</button>
      {vision && slot?.enabled && slot.consent_current && <button type="button" disabled={busy !== null} onClick={() => void save(true)}>Withdraw screenshot consent</button>}
      <button type="button" disabled={busy !== null || !slot?.enabled || !slot.catalog_receipt_current} onClick={() => void capabilityCheck()}>{busy === "capability:" + spec.name ? "Running capability check…" : (vision ? "Run vision capability check" : "Run capability check")}</button>
      {!vision && <button type="button" disabled={busy !== null || !slot?.enabled || !slot.catalog_receipt_current} onClick={() => void structuredCheck()}>{busy === "structured:" + spec.name ? "Running structured output check…" : "Run structured output check"}</button>}
      {vision && passedReceipt && !receipt?.structured_outputs && (structuredBasis === "capability_proof"
        ? <button type="button" disabled={busy !== null || !slot?.enabled || !slot.catalog_receipt_current} onClick={() => void structuredCheck("prompt_json")}>{busy === "structured:" + spec.name ? "Running structured output check…" : "Run prompt-JSON check"}</button>
        : <button type="button" disabled={busy !== null || !slot?.enabled || !slot.catalog_receipt_current} onClick={() => void structuredCheck("json_schema")}>{busy === "structured:" + spec.name ? "Running structured output check…" : "Run JSON-schema structured output check"}</button>)}
    </div>
  </div>;
}
