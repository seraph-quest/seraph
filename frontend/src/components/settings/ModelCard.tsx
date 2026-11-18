import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { NEAR_TEE_TEXT_MODELS } from "../../lib/modelFabric";
import type { ModelFabricSettingsStatus, NearTeeSlotStatus } from "../../lib/modelFabric";
import {
  NearTeeRequestError, isNearUnavailable, nearTeeKeySavePayload, nearTeeSlotFields, nearTeeSlotSavePayload,
  postNearTeeCapabilityCheck, postNearTeeCatalogCheck,
} from "../../lib/nearTeeActions";
import type { NearTeeSlotFields } from "../../lib/nearTeeActions";
import { SETTINGS_MODEL_CARD_ID } from "../../lib/settingsNavigation";

/**
 * The Model card (#1280): one plain status line saying whether chat works and,
 * when it does not, the one control that fixes it. Everything technical sits
 * under a closed "Details". Consent is only ever the operator's own tick; no
 * paid check runs unless it was announced on screen before the click. Seraph
 * keeps no spending limit (#1284): those are set in the NEAR account.
 */

/** The chat route from /api/runtime/status.effective_runtime. */
export interface ChatRouteReadiness {
  inference_ready: boolean;
  reasons: string[];
}

export type ModelCardState =
  | "loading" | "unreadable" | "missing_key" | "not_set_up" | "needs_ok"
  | "near_down" | "needs_test" | "route_unknown" | "ready" | "blocked";

/** Chat checks in the order the Model card runs them; structured output is last and optional. */
export const CHAT_REQUIRED_CHECKS = ["health", "latency_ms", "text", "streaming", "tool_use"] as const;
export const CHAT_OPTIONAL_CHECKS = ["structured_output"] as const;
const ALL_CHAT_CHECKS = [...CHAT_REQUIRED_CHECKS, ...CHAT_OPTIONAL_CHECKS];

const MODEL_NAMES: Record<string, string> = {
  "deepseek-ai/DeepSeek-V4.1-Flash-1017": "DeepSeek V4.1 Flash",
  "z-ai/glm-5.3-flash": "GLM 5.3 Flash",
  "Qwen/Qwen3.8-27B": "Qwen3.8 27B",
  "Qwen/Qwen3.6-35B-A3B-FP8": "Qwen3.6 35B A3B",
};
export function modelName(id: string | null | undefined): string {
  if (!id) return "No model";
  return MODEL_NAMES[id] ?? id.split("/").pop() ?? id;
}

/** Plain dollars: "$1,000", "$0.12", "less than $0.01". */
export function dollars(microusd: number): string {
  if (microusd > 0 && microusd < 5_000) return "less than $0.01";
  const value = microusd / 1_000_000;
  const whole = Number.isInteger(value);
  return "$" + value.toLocaleString("en-US", { minimumFractionDigits: whole ? 0 : 2, maximumFractionDigits: whole ? 0 : 2 });
}

const CONSENT_CODES = new Set(["provider_policy_revoked", "near_tee_purpose_consent_required"]);
const UNREADABLE_CODES = new Set(["configuration_unreadable", "provider_policy_continuity_unavailable"]);
const isProofGap = (value: string | null | undefined) => /^proof_(missing|expired)/.test(value ?? "");

function blockReasons(settings: ModelFabricSettingsStatus, route: ChatRouteReadiness | null): string[] {
  const slot = settings.near_tee?.slots.text;
  return [...(route?.reasons ?? []), slot?.reason_code, settings.near_tee?.reason_code, settings.inference_block_reason]
    .filter((item): item is string => typeof item === "string" && item.length > 0);
}

/**
 * Checks the stored slot still needs, or null when that cannot be known yet
 * (the catalog check has to run first, so the card shows the upper bound).
 */
export function missingChatChecks(slot: NearTeeSlotStatus | null | undefined): { required: string[]; optional: string[] } | null {
  if (!slot || !slot.catalog_receipt_current) return null;
  const required: string[] = [];
  // The slot reports only the first missing status check, so all three run.
  if (isProofGap(slot.reason_code)) required.push("health", "latency_ms", "text");
  for (const name of ["streaming", "tool_use"]) if (isProofGap(slot.chat_capabilities?.[name])) required.push(name);
  return { required, optional: isProofGap(slot.structured_output_capability) ? ["structured_output"] : [] };
}

/** Appendix B of the #1280 design: the first state that applies. */
export function modelCardState(settings: ModelFabricSettingsStatus | null, route: ChatRouteReadiness | null, nearDown = false): ModelCardState {
  if (!settings) return "loading";
  const reasons = blockReasons(settings, route);
  const near = settings.near_tee;
  const slot = near?.slots.text ?? null;
  if ((settings.configuration_status != null && settings.configuration_status !== "ready")
    || UNREADABLE_CODES.has(settings.error_code ?? "") || settings.near_tee_metadata_unavailable) return "unreadable";
  if (!near?.key_present || reasons.includes("near_tee_credential_required")) return "missing_key";
  if (!slot || !slot.enabled) return "not_set_up";
  if (settings.egress_revoked || !slot.consent_current || reasons.some(code => CONSENT_CODES.has(code))) return "needs_ok";
  // Session-local: the last check in this session found NEAR not answering.
  if (nearDown) return "near_down";
  const missing = missingChatChecks(slot);
  if (!missing || missing.required.length > 0) return "needs_test";
  // Without the runtime route the card cannot tell; it never guesses green.
  if (!route) return "route_unknown";
  // Ready needs both the route and the slot itself: the route alone does not
  // carry proof-metadata blocks.
  if (route.inference_ready && slot.status === "ready" && !slot.reason_code) return "ready";
  return "blocked";
}

/** "2026-12" -> "December 2026" (UTC month ids from the accounting owner). */
export function monthName(periodId: string | null | undefined): string {
  const match = /^(\d{4})-(0[1-9]|1[0-2])$/.exec(periodId ?? "");
  if (!match) return "this month";
  const names = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  return `${names[Number(match[2]) - 1]} ${match[1]}`;
}

/** The ready line's spend sentence: display only, never compared with anything. */
export function spendLine(accounting: ModelFabricSettingsStatus["inference_accounting"]): string {
  if (!accounting || accounting.status === "unavailable") return "Spend this month is not available.";
  const spent = accounting.spent_microusd ?? 0;
  const unpriced = accounting.unpriced_requests ?? 0;
  const base = spent > 0 ? `${dollars(spent)} used this month.` : "Nothing spent this month.";
  return unpriced > 0 ? `${base} ${unpriced} request${unpriced === 1 ? " is" : "s are"} not priced by NEAR yet.` : base;
}

interface Props {
  settings: ModelFabricSettingsStatus | null;
  route: ChatRouteReadiness | null;
  /** Last-known settings shown while fresh ones cannot be read; nothing can be saved. */
  stale: boolean;
  onSave: (payload: Record<string, unknown>) => Promise<ModelFabricSettingsStatus>;
  /** Publish settings returned by the catalog check. */
  onSettings: (next: ModelFabricSettingsStatus) => void;
  onRefresh: () => Promise<void>;
  onOpenDiagnostics: () => void;
}

interface Step { label: string; done: boolean }
interface CheckRecord { capability: string; outcome: string; code: string | null }
type Tone = "good" | "warn" | "bad";

const TEXT_SLOT_DEFAULTS = { maxInput: 8192, timeout: 30, ttl: 24 };
const buttonClass = "border border-retro-highlight px-3 py-1 text-[10px] uppercase tracking-wider text-retro-highlight disabled:border-retro-text/20 disabled:text-retro-text/30";
const inputClass = "min-w-0 border border-retro-text/20 bg-retro-bg px-1 py-0.5 text-[10px] text-retro-text disabled:opacity-40";
const CONSENT_TEXT = "I understand NEAR sees my messages as plain text: no end-to-end encryption, and Seraph does not verify NEAR's secure hardware.";

function errorText(reason: unknown): string {
  return (reason instanceof Error ? reason.message : String(reason ?? "unknown")).slice(0, 512);
}

export function ModelCard({ settings, route, stale, onSave, onSettings, onRefresh, onOpenDiagnostics }: Props) {
  const [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [steps, setSteps] = useState<Step[]>([]);
  const [nearDown, setNearDown] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [checks, setChecks] = useState<CheckRecord[]>([]);
  const [keyDraft, setKeyDraft] = useState("");
  const [pickedModel, setPickedModel] = useState("");
  const [changingModel, setChangingModel] = useState(false);
  /** What the running action is doing, in plain words. */
  const [busyText, setBusyText] = useState("Working…");
  /** Whether the last action saved anything before it stopped. */
  const [savedInLastRun, setSavedInLastRun] = useState(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);

  const slot = settings?.near_tee?.slots.text ?? null;
  const revision = settings?.egress_revision;
  // A consent tick never outlives the settings it was given for.
  useEffect(() => { setAcknowledged(false); }, [revision, slot?.model_id, slot?.enabled, slot?.consent_current]);

  const state = busy ? null : modelCardState(settings, route, nearDown);
  const accounting = settings?.inference_accounting;
  /** NEAR's listed price for one capability check on this model, when the catalog gave one. */
  const checkPrice = slot?.catalog_receipt?.required_cost_microusd;
  const missing = missingChatChecks(slot);
  /** Paid checks the next click may run: exact when known, otherwise every chat check. */
  const paidCount = (consentChanges: boolean) => consentChanges || !missing ? ALL_CHAT_CHECKS.length : missing.required.length + missing.optional.length;
  const disabled = busy || stale || !Number.isSafeInteger(revision);

  /** ", about $X in total" from the catalog price; empty when NEAR listed no price. */
  function about(count: number, suffix: string): string {
    if (typeof checkPrice !== "number" || !Number.isFinite(checkPrice) || checkPrice <= 0) return "";
    const total = dollars(Math.ceil(count * checkPrice));
    return total.startsWith("less than") ? `, ${total}${suffix}` : `, about ${total}${suffix}`;
  }

  function costLine(count: number, exact: boolean): string {
    return exact
      ? `It sends ${count} tiny paid request${count === 1 ? "" : "s"}${about(count, " in total")}.`
      : `If a quick test is needed, it also sends up to ${count} tiny paid requests${about(count, " in total")}.`;
  }

  function markStep(label: string) {
    setSteps(current => current.map(step => step.label === label ? { ...step, done: true } : step));
  }

  /** Free catalog check, then at most `paidAllowed` billed checks; never more than the card showed. */
  async function checkAndTest(startRevision: number, paidAllowed: number): Promise<void> {
    let next: ModelFabricSettingsStatus | null;
    try {
      next = await postNearTeeCatalogCheck(startRevision);
    } catch (reason) {
      if (reason instanceof NearTeeRequestError && isNearUnavailable(null, reason.status)) setNearDown(true);
      throw new Error("Catalog check: " + errorText(reason));
    }
    if (next) onSettings(next);
    const checked = next?.near_tee?.slots.text ?? null;
    const receipt = checked?.catalog_receipt;
    if (!checked?.catalog_receipt_current) {
      if (isNearUnavailable(receipt?.reason_code)) setNearDown(true);
      throw new Error("Catalog check: " + (receipt?.reason_code ?? checked?.reason_code ?? "no receipt"));
    }
    setNearDown(false);
    markStep("Checking NEAR");
    const gaps = missingChatChecks(checked);
    const toRun = [...(gaps?.required ?? []), ...(gaps?.optional ?? [])];
    if (toRun.length === 0) return;
    // Never spend more than the cost line promised; the card then asks again.
    if (paidAllowed === 0) return;
    if (toRun.length > paidAllowed) throw new Error(`${toRun.length} paid checks are needed; ${paidAllowed} were shown.`);
    setSteps(current => current.some(step => step.label === "Testing chat") ? current : [...current, { label: "Testing chat", done: false }]);
    for (const capability of toRun) {
      const optional = (CHAT_OPTIONAL_CHECKS as readonly string[]).includes(capability);
      try {
        const result = await postNearTeeCapabilityCheck(checked, capability);
        setChecks(current => [...current, { capability, outcome: result.outcome, code: result.error_code }]);
        if (result.outcome !== "passed" && !optional) {
          if (isNearUnavailable(result.error_code)) setNearDown(true);
          throw new Error(`Check ${capability}: ${result.error_code ?? result.outcome}`);
        }
      } catch (reason) {
        if (reason instanceof NearTeeRequestError) {
          setChecks(current => [...current, { capability, outcome: "refused", code: reason.message }]);
          // An optional check never decides whether NEAR is answering chat.
          if (!optional && isNearUnavailable(null, reason.status)) setNearDown(true);
          if (!optional) throw new Error(`Check ${capability}: ${reason.message}`);
        } else if (!optional) throw reason;
      }
    }
    markStep("Testing chat");
  }

  async function run(text: string, stepLabels: string[], body: () => Promise<void>) {
    setBusy(true); setBusyText(text); setFailure(null); setChecks([]); setSavedInLastRun(false);
    setSteps(stepLabels.map(label => ({ label, done: false })));
    try {
      await body();
    } catch (reason) {
      if (mounted.current) setFailure(errorText(reason));
    } finally {
      // Always re-read: the card renders whatever the backend now says.
      try { await onRefresh(); } catch { /* the panel shows its own metadata warning */ }
      if (mounted.current) { setBusy(false); setSteps([]); setAcknowledged(false); setChangingModel(false); }
    }
  }

  /** Save a slot with the operator's tick, then check NEAR and test chat. */
  function allowAndTurnOn(fields: NearTeeSlotFields, paidAllowed: number) {
    if (!acknowledged || revision === undefined) return;
    const payload = nearTeeSlotSavePayload({ slot: "text", fields, acknowledged: true, revision });
    void run(fields.model_id !== slot?.model_id && slot?.enabled ? "Switching model…" : "Turning chat on…", ["Saving your OK", "Checking NEAR"], async () => {
      const saved = await onSave(payload);
      setSavedInLastRun(true);
      if (!mounted.current) return;
      setAcknowledged(false);
      markStep("Saving your OK");
      if (!Number.isSafeInteger(saved.egress_revision)) throw new Error("Saved, but no settings revision came back.");
      await checkAndTest(saved.egress_revision as number, paidAllowed);
    });
  }

  function testOnly(paidAllowed: number) {
    if (revision === undefined) return;
    void run(paidAllowed > 0 ? "Running test…" : "Checking NEAR…", ["Checking NEAR"], () => checkAndTest(revision, paidAllowed));
  }

  function saveKey() {
    if (revision === undefined || !keyDraft.trim()) return;
    const payload = nearTeeKeySavePayload(keyDraft, revision);
    void run("Saving key…", ["Saving your key"], async () => {
      try {
        const saved = await onSave(payload);
        setSavedInLastRun(true);
        markStep("Saving your key");
        const text = saved.near_tee?.slots.text;
        if (text?.enabled && text.consent_current && Number.isSafeInteger(saved.egress_revision)) {
          setSteps(current => [...current, { label: "Checking NEAR", done: false }]);
          await checkAndTest(saved.egress_revision as number, 0);
        }
      } finally {
        // The key never stays in the page, saved or not.
        if (mounted.current) setKeyDraft("");
      }
    });
  }

  function turnOff() {
    if (!slot || revision === undefined) return;
    const payload = nearTeeSlotSavePayload({ slot: "text", fields: { ...nearTeeSlotFields(slot), enabled: false }, acknowledged: false, revision });
    void run("Turning chat off…", ["Turning chat off"], async () => { await onSave(payload); setSavedInLastRun(true); markStep("Turning chat off"); });
  }

  function recheck() {
    void run("Checking chat…", [], async () => {});
  }

  // ---- what the card says
  const plainModel = `${modelName(slot?.model_id)}, hosted by NEAR`;
  const consentChoice = (
    <label className="flex items-start gap-2 text-[10px]">
      <input type="checkbox" aria-label={CONSENT_TEXT} checked={acknowledged} disabled={disabled}
        onChange={event => setAcknowledged(event.target.checked)} />
      <span>{CONSENT_TEXT}</span>
    </label>
  );
  const picker = (value: string, onChange: (next: string) => void) => (
    <select aria-label="Chat model" className={inputClass + " w-full"} value={value} disabled={disabled}
      onChange={event => { onChange(event.target.value); setAcknowledged(false); }}>
      <option value="">Choose a model</option>
      {NEAR_TEE_TEXT_MODELS.map(id => <option key={id} value={id}>{modelName(id)}</option>)}
    </select>
  );
  const costNote = (text: string) => <p className="text-[10px] text-retro-text/70" data-testid="model-card-cost">{text}</p>;

  let tone: Tone = "warn";
  let status = "";
  let second: ReactNode = null;
  let action: ReactNode = null;

  if (busy || state === null) {
    status = busyText;
    second = <ul className="text-[10px]" aria-label="Progress">{steps.map(step =>
      <li key={step.label}>{step.done ? "✓" : "…"} {step.label}</li>)}</ul>;
  } else if (changingModel && state === "ready" && slot) {
    const switching = pickedModel !== "" && pickedModel !== slot.model_id;
    status = "Pick a model.";
    second = <div className="flex flex-col gap-2">{picker(pickedModel, setPickedModel)}{switching && consentChoice}
      {switching && costNote(costLine(ALL_CHAT_CHECKS.length, false))}</div>;
    action = <div className="flex flex-wrap items-center gap-3">
      <button type="button" className={buttonClass} disabled={disabled || !switching || !acknowledged}
        onClick={() => allowAndTurnOn({ ...nearTeeSlotFields(slot), model_id: pickedModel }, ALL_CHAT_CHECKS.length)}>Allow and switch model</button>
      <button type="button" className="text-[10px] underline" onClick={() => { setChangingModel(false); setAcknowledged(false); }}>Cancel</button>
    </div>;
  } else switch (state) {
    case "loading":
      status = "Checking chat…"; break;
    case "unreadable":
      tone = "bad"; status = "Seraph can't read its model settings.";
      second = "Nothing was sent to NEAR.";
      action = <button type="button" className={buttonClass} onClick={onOpenDiagnostics}>Open Diagnostics</button>;
      break;
    case "missing_key":
      status = "Chat is off. Seraph needs a NEAR API key.";
      second = <label className="flex flex-col gap-1 text-[10px]">Paste your NEAR key. It is stored locally and never shown again.
        <input className={inputClass} type="password" autoComplete="new-password" aria-label="NEAR API key"
          value={keyDraft} disabled={disabled} onChange={event => setKeyDraft(event.target.value)} /></label>;
      action = <button type="button" className={buttonClass} disabled={disabled || !keyDraft.trim()} onClick={saveKey}>Save key</button>;
      break;
    case "not_set_up": {
      status = "Chat is off. Pick a model to turn it on.";
      second = <div className="flex flex-col gap-2">{picker(pickedModel, setPickedModel)}{consentChoice}
        {costNote(costLine(ALL_CHAT_CHECKS.length, false))}</div>;
      const fields: NearTeeSlotFields = slot ? { ...nearTeeSlotFields(slot), enabled: true, model_id: pickedModel }
        : { model_id: pickedModel, enabled: true, max_input_tokens: TEXT_SLOT_DEFAULTS.maxInput,
          max_output_tokens: settings?.near_tee?.reasoning_recommended_max_output_tokens ?? 4096,
          timeout_seconds: TEXT_SLOT_DEFAULTS.timeout, catalog_ttl_hours: TEXT_SLOT_DEFAULTS.ttl };
      action = <button type="button" className={buttonClass} disabled={disabled || !pickedModel || !acknowledged}
        onClick={() => allowAndTurnOn(fields, ALL_CHAT_CHECKS.length)}>Turn on chat</button>;
      break;
    }
    case "needs_ok":
      status = "Chat is paused. It needs your OK.";
      second = <div className="flex flex-col gap-2"><p className="text-[10px]">{plainModel}</p>{consentChoice}
        {costNote(costLine(paidCount(true), false))}</div>;
      action = slot && <button type="button" className={buttonClass} disabled={disabled || !acknowledged}
        onClick={() => allowAndTurnOn(nearTeeSlotFields(slot), paidCount(true))}>Allow and turn on chat</button>;
      break;
    case "route_unknown":
      status = "Can't check chat status right now.";
      second = "Your settings were read; the chat route could not be.";
      action = <button type="button" className={buttonClass} disabled={busy} onClick={recheck}>Try again</button>;
      break;
    case "near_down":
      tone = "bad"; status = "NEAR isn't answering right now.";
      second = <div className="flex flex-col gap-2"><p className="text-[10px]">{savedInLastRun
        ? "Your change was saved, but NEAR didn't answer the check that follows it."
        : "Your settings are fine. Nothing was changed."}</p>
        {paidCount(false) > 0 && costNote(costLine(paidCount(false), Boolean(missing)))}</div>;
      action = <button type="button" className={buttonClass} disabled={disabled} onClick={() => testOnly(paidCount(false))}>Try again</button>;
      break;
    case "needs_test": {
      const count = paidCount(false);
      status = "One quick test left.";
      second = costNote(missing ? costLine(count, true) : `It checks NEAR's catalog for free. ${costLine(count, false)}`);
      action = <button type="button" className={buttonClass} disabled={disabled} onClick={() => testOnly(count)}>Run test</button>;
      break;
    }
    case "ready":
      tone = "good"; status = "Chat is on.";
      second = `${plainModel}. ${spendLine(accounting)}`;
      action = <button type="button" className={buttonClass} disabled={disabled}
        onClick={() => { setPickedModel(slot?.model_id ?? ""); setChangingModel(true); setAcknowledged(false); }}>Change model</button>;
      break;
    case "blocked":
      tone = "bad"; status = "Chat is blocked.";
      second = "The reason is under Details.";
      action = <button type="button" className={buttonClass} onClick={onOpenDiagnostics}>Open Diagnostics</button>;
      break;
  }

  const dot = tone === "good" ? "bg-green-400" : tone === "bad" ? "bg-red-400" : "bg-yellow-400";
  const reasons = settings ? blockReasons(settings, route) : [];
  const receipt = slot?.catalog_receipt;
  return (
    <section id={SETTINGS_MODEL_CARD_ID} aria-label="Model card" data-state={busy ? "working" : state ?? "working"}
      className="mb-3 flex flex-col gap-2 border border-retro-text/20 px-3 py-3">
      <div className="flex items-start gap-2">
        <span aria-hidden="true" className={`mt-1 h-2 w-2 flex-shrink-0 rounded-full ${dot}`} />
        <div className="flex min-w-0 flex-col gap-1">
          <div role="status" className="text-[12px] text-retro-text">{status}</div>
          {typeof second === "string" ? <p className="text-[10px] text-retro-text/70">{second}</p> : second}
        </div>
      </div>
      {stale && <p className="text-[10px] text-retro-text/60">Showing the last settings Seraph could read. Changes wait until they load again.</p>}
      {failure && <p role="alert" className="text-[10px] text-red-400">That did not finish. The reason is under Details.</p>}
      {action && <div>{action}</div>}
      <details className="text-[10px]">
        <summary className="cursor-pointer text-retro-text/60">Details</summary>
        <dl className="mt-2 grid grid-cols-[minmax(0,10rem)_minmax(0,1fr)] gap-x-2 gap-y-1 text-[9px]">
          {slot && <>
            <dt>Model id</dt><dd className="break-all">{slot.model_id}</dd>
            <dt>Input / output limit</dt><dd>{slot.max_input_tokens} / {slot.max_output_tokens} tokens</dd>
            <dt>Timeout</dt><dd>{slot.timeout_seconds} s</dd>
            <dt>Catalog refresh</dt><dd>every {slot.catalog_ttl_hours} h (background)</dd>
            <dt>Catalog receipt</dt><dd>{receipt ? `${receipt.outcome}${receipt.checked_at_iso ? " at " + receipt.checked_at_iso : ""}${receipt.reason_code ? " · " + receipt.reason_code : ""}` : "none"}{slot.catalog_receipt_current ? "" : " (not current)"}</dd>
            <dt>Streaming / tool use</dt><dd>{slot.chat_capabilities?.streaming ?? "unknown"} / {slot.chat_capabilities?.tool_use ?? "unknown"}</dd>
            <dt>Structured output</dt><dd>{slot.structured_output_capability ?? "unknown"}</dd>
          </>}
          <dt>Spending limits</dt><dd>Spending limits are set in your NEAR account.</dd>
          <dt>Reason codes</dt><dd className="break-all">{reasons.length ? Array.from(new Set(reasons)).join(", ") : "none"}</dd>
          {checks.length > 0 && <><dt>Checks this session</dt><dd>{checks.map(item =>
            `${item.capability}: ${item.outcome}${item.code ? " (" + item.code + ")" : ""}`).join("; ")}</dd></>}
          {failure && <><dt>Last error</dt><dd className="break-all" data-testid="model-card-failure">{failure}</dd></>}
        </dl>
        {slot?.enabled && slot.catalog_receipt_current && <div className="mt-2 flex flex-col gap-1">
          <p className="text-[9px] text-retro-text/60">Structured output check: one paid request{about(1, "")}.</p>
          <button type="button" className="w-fit border border-retro-text/20 px-2 py-0.5 text-[9px]" disabled={disabled}
            onClick={() => void run("Running check…", ["Testing structured output"], async () => {
              const result = await postNearTeeCapabilityCheck(slot, "structured_output");
              setChecks([{ capability: "structured_output", outcome: result.outcome, code: result.error_code }]);
              markStep("Testing structured output");
            })}>Run structured output check</button>
        </div>}
        {slot?.enabled && <button type="button" className="mt-2 text-[9px] underline" disabled={disabled} onClick={turnOff}>Turn off chat</button>}
      </details>
    </section>
  );
}
