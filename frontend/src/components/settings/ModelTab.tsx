import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { ModelCard, dollars, modelName } from "./ModelCard";
import { NEAR_TEE_VISION_MODELS } from "../../lib/modelFabric";
import type { ModelFabricSettingsStatus, NearTeeSlotStatus } from "../../lib/modelFabric";
import {
  NearTeeRequestError, nearTeeSlotFields, nearTeeSlotSavePayload, postNearTeeCapabilityCheck, postNearTeeCatalogCheck,
} from "../../lib/nearTeeActions";
import type { NearTeeSlotFields } from "../../lib/nearTeeActions";
import { openSettingsSection } from "../../lib/settingsNavigation";
import { SettingsRow, settingsActionClass, settingsLinkClass } from "./settingsLayout";
import type { SettingsTone } from "./settingsLayout";
import { useArtifactStorageSettings, useModelFabric } from "./settingsData";
import type { ArtifactSettingsSource, ArtifactStorageSettings } from "./settingsData";

/**
 * The Model tab (#1280 PR 3): the Model card, then one row each for screenshot
 * analysis and memory search. Each row says one plain thing and offers at most
 * one action. Everything raw moved to the Diagnostics tab. Seraph keeps no
 * spending limit (ADR-037), so there is no budget row.
 */

const isProofGap = (value: string | null | undefined) => /^proof_(missing|expired)/.test(value ?? "");

// ---- Screenshot analysis

export type ScreenshotRowState = "loading" | "unreadable" | "missing_key" | "off" | "needs_ok" | "needs_test" | "blocked" | "on";

/** Mechanics checks a vision slot needs before it is ready (same list the NEAR slot editor runs). */
export const VISION_CHECKS = ["vision", "structured_output", "health", "latency_ms"] as const;
const VISION_SLOT_DEFAULTS = { maxInput: 16384, maxOutput: 2048, timeout: 30, ttl: 24 };
const SCREENSHOT_TEXT_CONSENT = "I understand NEAR sees these requests as plain text: no end-to-end encryption, and Seraph does not verify NEAR's secure hardware.";
const SCREENSHOT_CONSENT = "I agree to send my screenshots to NEAR for analysis. This is separate from the chat OK.";

type ScreenSettings = Pick<ArtifactStorageSettings["screen"], "analysis_enabled" | "provider">;

/** First state that applies. Screen settings from the placeholder fallback are never read as truth. */
export function screenshotRowState(
  screen: ScreenSettings | null,
  source: ArtifactSettingsSource,
  settings: ModelFabricSettingsStatus | null,
): ScreenshotRowState {
  if (!screen || source === "fallback" || !settings) return "loading";
  if (settings.near_tee_metadata_unavailable
    || (settings.configuration_status != null && settings.configuration_status !== "ready")) return "unreadable";
  if (!(screen.analysis_enabled && screen.provider === "near_tee")) return "off";
  if (!settings.near_tee?.key_present) return "missing_key";
  const slot = settings.near_tee.slots.vision ?? null;
  if (!slot || !slot.enabled || !slot.consent_current || settings.egress_revoked) return "needs_ok";
  if (!slot.catalog_receipt_current || isProofGap(slot.reason_code)) return "needs_test";
  if (slot.status === "ready" && !slot.reason_code) return "on";
  return "blocked";
}

function errorText(reason: unknown): string {
  return (reason instanceof Error ? reason.message : String(reason ?? "unknown")).slice(0, 512);
}

interface ScreenshotRowProps {
  screen: ScreenSettings | null;
  source: ArtifactSettingsSource;
  settings: ModelFabricSettingsStatus | null;
  /** Last-known model settings: nothing can be saved. */
  stale: boolean;
  savingScreen: boolean;
  onSave: (payload: Record<string, unknown>) => Promise<ModelFabricSettingsStatus>;
  onSettings: (next: ModelFabricSettingsStatus) => void;
  /** PUT /api/settings/screen-analysis; resolves false when the save failed. */
  onScreenPatch: (patch: Record<string, unknown>) => Promise<boolean>;
  onRefresh: () => Promise<void>;
}

export function ScreenshotAnalysisRow({ screen, source, settings, stale, savingScreen, onSave, onSettings, onScreenPatch, onRefresh }: ScreenshotRowProps) {
  const [asking, setAsking] = useState(false);
  const [textOk, setTextOk] = useState(false);
  const [screenshotOk, setScreenshotOk] = useState(false);
  const [pickedModel, setPickedModel] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);

  const slot: NearTeeSlotStatus | null = settings?.near_tee?.slots.vision ?? null;
  const revision = settings?.egress_revision;
  // A tick never outlives the settings it was given for.
  useEffect(() => { setTextOk(false); setScreenshotOk(false); }, [revision, slot?.model_id, slot?.enabled, slot?.consent_current]);

  const state = screenshotRowState(screen, source, settings);
  const disabled = busy !== null || stale || savingScreen || !Number.isSafeInteger(revision);
  /** Whether turning on needs a new OK: the stored slot's consent cannot carry. */
  const needsConsent = !slot || !slot.enabled || !slot.consent_current || Boolean(settings?.egress_revoked);
  const model = slot?.model_id || pickedModel;

  async function run(label: string, body: () => Promise<void>) {
    setBusy(label); setFailure(null);
    try { await body(); }
    catch (reason) { if (mounted.current) setFailure(errorText(reason)); }
    finally {
      // Always re-read: the row renders whatever the backend now says.
      try { await onRefresh(); } catch { /* the Model card shows its own metadata warning */ }
      if (mounted.current) { setBusy(null); setTextOk(false); setScreenshotOk(false); setAsking(false); }
    }
  }

  /** Free catalog check; returns the vision slot it reported. */
  async function catalogCheck(startRevision: number): Promise<NearTeeSlotStatus | null> {
    const next = await postNearTeeCatalogCheck(startRevision).catch(reason => {
      throw new Error("Catalog check: " + (reason instanceof NearTeeRequestError ? reason.message : errorText(reason)));
    });
    if (next) onSettings(next);
    return next?.near_tee?.slots.vision ?? null;
  }

  function turnOn() {
    if (revision === undefined) return;
    if (needsConsent && (!textOk || !screenshotOk || !model)) return;
    void run("Turning screenshot analysis on…", async () => {
      let current = revision;
      if (needsConsent) {
        const fields: NearTeeSlotFields = slot ? { ...nearTeeSlotFields(slot), enabled: true, model_id: model }
          : { model_id: model, enabled: true, max_input_tokens: VISION_SLOT_DEFAULTS.maxInput, max_output_tokens: VISION_SLOT_DEFAULTS.maxOutput,
            timeout_seconds: Math.min(VISION_SLOT_DEFAULTS.timeout, Math.floor(settings?.near_tee?.vision_max_timeout_seconds ?? VISION_SLOT_DEFAULTS.timeout)),
            catalog_ttl_hours: VISION_SLOT_DEFAULTS.ttl };
        // Both OKs come from the operator's own ticks in this render.
        const saved = await onSave(nearTeeSlotSavePayload({ slot: "vision", fields, acknowledged: textOk, screenshot: screenshotOk ? "grant" : null, revision }));
        if (!Number.isSafeInteger(saved.egress_revision)) throw new Error("Saved, but no settings revision came back.");
        current = saved.egress_revision as number;
      }
      if (!(screen?.analysis_enabled && screen.provider === "near_tee")) {
        const ok = await onScreenPatch({ enabled: true, provider: "near_tee" });
        if (!ok) throw new Error("Screenshot analysis settings were not saved.");
      }
      // A save rebinds the catalog receipt to the new revision; the check is free.
      if (needsConsent || !slot?.catalog_receipt_current) await catalogCheck(current);
    });
  }

  function runTest(paidAllowed: number) {
    if (revision === undefined) return;
    void run("Running test…", async () => {
      let checked = slot;
      if (!checked?.catalog_receipt_current) {
        checked = await catalogCheck(revision);
        if (!checked?.catalog_receipt_current) throw new Error("Catalog check: " + (checked?.catalog_receipt?.reason_code ?? checked?.reason_code ?? "no receipt"));
      }
      if (!isProofGap(checked.reason_code)) return;
      // Never more paid requests than the cost line showed.
      for (const capability of VISION_CHECKS.slice(0, paidAllowed)) {
        const result = await postNearTeeCapabilityCheck(checked, capability);
        if (result.outcome !== "passed") throw new Error(`Check ${capability}: ${result.error_code ?? result.outcome}`);
      }
    });
  }

  function turnOff() {
    void run("Turning screenshot analysis off…", async () => {
      const ok = await onScreenPatch({ enabled: false });
      if (!ok) throw new Error("Screenshot analysis settings were not saved.");
    });
  }

  const price = slot?.catalog_receipt?.required_cost_microusd;
  const about = typeof price === "number" && Number.isFinite(price) && price > 0
    ? (() => { const total = dollars(Math.ceil(VISION_CHECKS.length * price)); return total.startsWith("less than") ? `, ${total} in total` : `, about ${total} in total`; })()
    : "";
  const testLine = `It sends ${VISION_CHECKS.length} tiny paid requests to NEAR${about}.`;

  let tone: SettingsTone = "warn";
  let status = "";
  let detail: ReactNode = null;
  let action: ReactNode = null;

  const consentForm = (
    <div className="flex flex-col gap-2 text-[10px]">
      {!slot?.model_id && (
        <select aria-label="Screenshot model" className="min-w-0 border border-retro-text/20 bg-retro-bg px-1 py-0.5 text-[10px] text-retro-text"
          value={pickedModel} disabled={disabled} onChange={event => { setPickedModel(event.target.value); setTextOk(false); setScreenshotOk(false); }}>
          <option value="">Choose a model</option>
          {NEAR_TEE_VISION_MODELS.map(id => <option key={id} value={id}>{modelName(id)}</option>)}
        </select>
      )}
      <label className="flex items-start gap-2">
        <input type="checkbox" aria-label={SCREENSHOT_TEXT_CONSENT} checked={textOk} disabled={disabled} onChange={event => setTextOk(event.target.checked)} />
        <span>{SCREENSHOT_TEXT_CONSENT}</span>
      </label>
      <label className="flex items-start gap-2">
        <input type="checkbox" aria-label={SCREENSHOT_CONSENT} checked={screenshotOk} disabled={disabled} onChange={event => setScreenshotOk(event.target.checked)} />
        <span>{SCREENSHOT_CONSENT}</span>
      </label>
      <p className="text-retro-text/60">Turning it on sends nothing paid. A short paid test follows as its own step.</p>
      <div className="flex flex-wrap items-center gap-3">
        <button type="button" className={settingsActionClass} disabled={disabled || !textOk || !screenshotOk || !model} onClick={turnOn}>Allow and turn on</button>
        <button type="button" className={settingsLinkClass} onClick={() => { setAsking(false); setTextOk(false); setScreenshotOk(false); }}>Cancel</button>
      </div>
    </div>
  );
  const turnOnButton = (
    <button type="button" className={settingsActionClass} disabled={disabled}
      onClick={() => (needsConsent ? setAsking(true) : turnOn())}>Turn on</button>
  );

  if (busy) {
    status = busy;
  } else switch (state) {
    case "loading":
      tone = "off"; status = "Checking screenshot analysis…"; break;
    case "unreadable":
      tone = "bad"; status = "Seraph can't read its screenshot settings.";
      action = <button type="button" className={settingsActionClass} onClick={() => openSettingsSection("near-slots")}>Open Diagnostics</button>;
      break;
    case "off":
      tone = "off"; status = "Off.";
      detail = screen?.provider === "openrouter" ? "It still points at the retired OpenRouter route." : null;
      if (!settings?.near_tee?.key_present) detail = "Add your NEAR key in the Model card first.";
      else action = asking ? null : turnOnButton;
      break;
    case "missing_key":
      status = "Paused. Seraph needs a NEAR API key.";
      detail = "Add your NEAR key in the Model card above.";
      break;
    case "needs_ok":
      status = "Paused. Screenshot analysis needs your OK.";
      action = asking ? null : turnOnButton;
      break;
    case "needs_test":
      status = "One quick test left.";
      detail = <p className="text-[10px] text-retro-text/70" data-testid="screenshot-row-cost">{slot?.catalog_receipt_current
        ? testLine : `It checks NEAR's catalog for free. If a test is still needed, ${testLine.charAt(0).toLowerCase()}${testLine.slice(1)}`}</p>;
      action = <button type="button" className={settingsActionClass} disabled={disabled} onClick={() => runTest(VISION_CHECKS.length)}>Run test</button>;
      break;
    case "blocked":
      tone = "bad"; status = "Screenshot analysis is blocked.";
      detail = "The reason is in Diagnostics, under NEAR slots.";
      action = <button type="button" className={settingsActionClass} onClick={() => openSettingsSection("near-slots")}>Open Diagnostics</button>;
      break;
    case "on":
      tone = "good"; status = "On.";
      detail = `Screenshots are analysed by ${modelName(slot?.model_id)}, hosted by NEAR.`;
      action = <button type="button" className={settingsActionClass} disabled={disabled} onClick={turnOff}>Turn off</button>;
      break;
  }

  return (
    <SettingsRow label="Screenshot analysis" status={status} detail={detail} tone={tone} action={action}>
      {asking && !busy && (state === "off" || state === "needs_ok") && consentForm}
      {failure && <p role="alert" className="text-[10px] text-red-400" data-testid="screenshot-row-failure">That did not finish: {failure}</p>}
    </SettingsRow>
  );
}

// ---- Memory search

export type MemoryRowState = "loading" | "unreadable" | "no_model" | "not_indexed" | "on";

const EVIDENCE_REASON = "near_tee_embedding_evidence_required";

export function memoryRowState(settings: ModelFabricSettingsStatus | null): MemoryRowState {
  if (!settings) return "loading";
  if (settings.near_tee_metadata_unavailable) return "unreadable";
  const index = settings.near_tee?.embedding_index ?? null;
  if (index?.recall_mode === "semantic") return "on";
  const slot = settings.near_tee?.slots.embedding ?? null;
  if (!slot || !slot.enabled || slot.evidence_required || slot.status !== "ready"
    || index?.slot_reason_code === EVIDENCE_REASON || Boolean(index?.slot_reason_code)) return "no_model";
  return "not_indexed";
}

/**
 * Memory search has no embedding model since OpenRouter was retired, and the
 * NEAR embedding slot stays shut until ADR-035's evidence gate opens. The row
 * says so plainly and offers nothing that would pretend to fix it.
 */
export function MemorySearchRow({ settings }: { settings: ModelFabricSettingsStatus | null }) {
  const state = memoryRowState(settings);
  const details = <button type="button" className={settingsLinkClass} onClick={() => openSettingsSection("near-slots")}>Details</button>;
  const gated = Boolean(settings?.near_tee?.slots.embedding?.evidence_required
    || settings?.near_tee?.embedding_index?.slot_reason_code === EVIDENCE_REASON);
  switch (state) {
    case "loading":
      return <SettingsRow label="Memory search" tone="off" status="Checking memory search…" />;
    case "unreadable":
      return <SettingsRow label="Memory search" tone="bad" status="Seraph can't read its memory search settings." action={details} />;
    case "on":
      return <SettingsRow label="Memory search" tone="good" status="On." detail="Memory search finds notes by meaning." />;
    case "not_indexed":
      return <SettingsRow label="Memory search" tone="warn" status="Off. Your memories aren't indexed yet."
        detail="Until they are, memory search matches words, not meaning." action={details} />;
    case "no_model":
      return <SettingsRow label="Memory search" tone="off" status="Off. There is no embedding model to use right now."
        detail={gated
          ? "Memory search matches words, not meaning. NEAR's embedding model stays off until Seraph can confirm NEAR serves exactly the model it asks for. There is nothing to turn on yet."
          : "Memory search matches words, not meaning."}
        action={details} />;
  }
}

// ---- The tab

export function ModelTab() {
  const fabric = useModelFabric();
  const artifacts = useArtifactStorageSettings();
  const refresh = () => fabric.refresh();
  return (
    <div className="flex flex-col gap-2 px-1">
      <ModelCard
        settings={fabric.modelFabric}
        route={fabric.chatRoute}
        stale={fabric.stale}
        onSave={fabric.save}
        onSettings={fabric.publish}
        onRefresh={refresh}
        onOpenDiagnostics={() => openSettingsSection("near-slots")}
      />
      <ScreenshotAnalysisRow
        screen={artifacts.settings?.screen ?? null}
        source={artifacts.source}
        settings={fabric.modelFabric}
        stale={fabric.stale}
        savingScreen={artifacts.saving}
        onSave={fabric.save}
        onSettings={fabric.publish}
        onScreenPatch={artifacts.updateScreenAnalysis}
        onRefresh={async () => { await Promise.all([fabric.refresh(), artifacts.fetchSettings()]); }}
      />
      <MemorySearchRow settings={fabric.modelFabric} />
      <button type="button" className={settingsLinkClass + " w-fit"} onClick={() => openSettingsSection("diagnostics")}>
        Raw model output and advanced settings are on the Diagnostics tab.
      </button>
    </div>
  );
}
