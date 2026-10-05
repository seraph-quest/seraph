import { useEffect, useRef, useState } from "react";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import {
  normalizeModelFabricSettings,
  type ModelFabricSettingsStatus,
  type OpenRouterPurpose,
  type OpenRouterRouteValue,
  type OpenRouterSetupStatus,
  type OpenRouterSetupValue,
} from "../../lib/modelFabric";

const PURPOSES = ["text", "vision", "embedding"] as const;
const CAPABILITIES = {
  text: ["text", "tool_use", "structured_output", "streaming"],
  vision: ["text", "vision", "structured_output"],
  embedding: ["embedding"],
};
interface RouteDraft {
  enabled: boolean;
  modelId: string;
  capabilities: string[];
  temperature: string;
  maxOutputTokens: string;
  timeoutSeconds: string;
  allowedUpstreams: string;
  zeroDataRetention: boolean;
  requestCostBoundMicrousd: string;
}
interface SetupDraft {
  routes: Record<OpenRouterPurpose, RouteDraft>;
  egressClass: string;
  cloudEgressAcknowledged: boolean;
  spendCeilingMicrousd: string;
  maxQueued: string;
  maxOutstandingPerOwner: string;
  maxRetries: string;
}
interface OpenRouterSetupPanelProps {
  setup: OpenRouterSetupStatus | null | undefined;
  stale: boolean;
  onSave: (payload: Record<string, unknown>) => Promise<ModelFabricSettingsStatus>;
  policyRevision?: number;
  policyRevoked?: boolean;
}
function routeDraft(route: OpenRouterRouteValue | null | undefined, slot: OpenRouterPurpose): RouteDraft {
  return {
    enabled: route?.enabled ?? false,
    modelId: route?.model_id ?? "",
    capabilities: route ? [...route.capabilities] : slot === "vision" ? ["text", "vision", "structured_output"] : slot === "embedding" ? ["embedding"] : ["text", "structured_output"],
    temperature: String(route?.temperature ?? 0.7),
    maxOutputTokens: String(route?.max_output_tokens ?? 4096),
    timeoutSeconds: String(route?.timeout_seconds ?? 120),
    allowedUpstreams: route?.allowed_upstreams.join(", ") ?? "",
    zeroDataRetention: route?.zero_data_retention ?? false,
    requestCostBoundMicrousd: route ? String(route.request_cost_bound_microusd) : "",
  };
}
function draftFromSetup(setup: OpenRouterSetupStatus | null | undefined, revoked = false): SetupDraft {
  return {
    routes: { text: routeDraft(setup?.routes.text, "text"), vision: routeDraft(setup?.routes.vision, "vision"), embedding: routeDraft(setup?.routes.embedding, "embedding") },
    egressClass: setup?.egress_class ?? "cloud_allowed_full",
    cloudEgressAcknowledged: !revoked && (setup?.cloud_egress_acknowledged ?? false),
    spendCeilingMicrousd: setup?.spend_ceiling_microusd == null ? "" : String(setup.spend_ceiling_microusd),
    maxQueued: String(setup?.max_queued ?? 64),
    maxOutstandingPerOwner: String(setup?.max_outstanding_per_owner ?? 16),
    maxRetries: String(setup?.max_retries ?? 2),
  };
}
function splitList(value: string): string[] {
  return value.split(",").map((item) => item.trim()).filter(Boolean);
}
function boundedNumber(value: string, label: string, min: number, max: number, integer = false): number {
  const parsed = Number(value);
  if (!value.trim() || !Number.isFinite(parsed) || parsed < min || parsed > max || (integer && !Number.isInteger(parsed))) {
    throw new Error(label + " must be a finite value between " + min + " and " + max + (integer ? " (whole number)." : "."));
  }
  return parsed;
}
function routeValue(draft: RouteDraft, slot: OpenRouterPurpose, ceiling: number): OpenRouterRouteValue | null {
  // An untouched absent purpose stays absent and does not block saving text.
  if (!draft.enabled && !draft.modelId.trim()) return null;
  const model = draft.modelId.trim();
  if (!/^[^\s,\/]+\/[^\s,]+$/.test(model)) throw new Error(slot + ": select one qualified OpenRouter model ID.");
  const upstreams = splitList(draft.allowedUpstreams);
  if (!upstreams.length) throw new Error(slot + ": an explicit upstream allow-list is required.");
  const required = slot === "vision" ? ["text", "vision"] : [slot];
  if (!required.every((capability) => draft.capabilities.includes(capability))) throw new Error(slot + ": required capabilities are " + required.join(", ") + ".");
  if (slot !== "text" && draft.enabled && !draft.zeroDataRetention) throw new Error(slot + ": zero data retention is required.");
  return {
    model_id: model, enabled: draft.enabled, capabilities: [...draft.capabilities], allowed_upstreams: upstreams,
    temperature: boundedNumber(draft.temperature, slot + " temperature", 0, 2),
    max_output_tokens: boundedNumber(draft.maxOutputTokens, slot + " max output tokens", 1, 131072, true),
    timeout_seconds: boundedNumber(draft.timeoutSeconds, slot + " timeout", 1, 120),
    zero_data_retention: draft.zeroDataRetention,
    request_cost_bound_microusd: boundedNumber(draft.requestCostBoundMicrousd, slot + " request cost bound", 1, ceiling, true),
  };
}
function unchangedRoute(draft: RouteDraft, route: OpenRouterRouteValue | null | undefined): boolean {
  if (!route?.enabled) return false;
  const sameList = (a: string[], b: string[]) => [...a].sort().join("\n") === [...b].sort().join("\n");
  return draft.modelId.trim() === route.model_id && draft.enabled === route.enabled
    && sameList(draft.capabilities, route.capabilities) && sameList(splitList(draft.allowedUpstreams), route.allowed_upstreams)
    && Number(draft.temperature) === route.temperature && Number(draft.maxOutputTokens) === route.max_output_tokens
    && Number(draft.timeoutSeconds) === route.timeout_seconds && draft.zeroDataRetention === route.zero_data_retention
    && Number(draft.requestCostBoundMicrousd) === route.request_cost_bound_microusd;
}
const inputClass = "min-w-0 border border-retro-text/20 bg-retro-bg px-1 py-0.5 text-retro-text disabled:opacity-40";

export function OpenRouterSetupPanel({ setup, stale, onSave, policyRevision, policyRevoked }: OpenRouterSetupPanelProps) {
  const [metadata, setMetadata] = useState(setup);
  const [revision, setRevision] = useState(policyRevision);
  const [revoked, setRevoked] = useState(policyRevoked ?? false);
  const [draft, setDraft] = useState(() => draftFromSetup(setup, policyRevoked));
  const [purposeAcks, setPurposeAcks] = useState({ vision: false, embedding: false });
  const [credential, setCredential] = useState("");
  const [saving, setSaving] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refreshMessage, setRefreshMessage] = useState<string | null>(null);
  const [metadataRetained, setMetadataRetained] = useState(stale);
  const dirty = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  useEffect(() => {
    // Partial metadata must not erase last-known controls or unsaved edits.
    if (stale) { setMetadataRetained(true); return; }
    // Empty initial setup still has an authoritative outer revision. Later
    // partial setup metadata retains the controls with a visible stale label.
    if (policyRevision !== undefined) setRevision(policyRevision);
    if (policyRevoked !== undefined) setRevoked(policyRevoked);
    setPurposeAcks({ vision: false, embedding: false });
    if (policyRevoked) setDraft((current) => ({ ...current, cloudEgressAcknowledged: false }));
    if (setup == null) { setMetadataRetained(Boolean(metadata)); return; }
    setMetadata(setup);
    setMetadataRetained(false);
    if (!dirty.current) setDraft(draftFromSetup(setup, policyRevoked));
  }, [setup, stale, policyRevision, policyRevoked]);
  function update<K extends Exclude<keyof SetupDraft, "routes">>(field: K, value: SetupDraft[K]) {
    dirty.current = true;
    setDraft((current) => ({ ...current, [field]: value }));
    setPurposeAcks({ vision: false, embedding: false });
  }
  function updateRoute<K extends keyof RouteDraft>(slot: OpenRouterPurpose, field: K, value: RouteDraft[K]) {
    dirty.current = true;
    setDraft((current) => ({ ...current, routes: { ...current.routes, [slot]: { ...current.routes[slot], [field]: value } } }));
    if (slot !== "text") setPurposeAcks((current) => ({ ...current, [slot]: false }));
  }
  function needsAck(slot: "vision" | "embedding"): boolean {
    return draft.routes[slot].enabled && (revoked || metadata?.slot_statuses[slot].error_code === "purpose_consent_stale"
      || !unchangedRoute(draft.routes[slot], metadata?.routes[slot]));
  }
  function safeError(value: unknown): string {
    const message = value instanceof Error ? value.message : "OpenRouter setup could not be saved.";
    return (credential ? message.split(credential).join("[redacted]") : message).slice(0, 512);
  }
  async function refresh() {
    setRefreshing(true);
    setRefreshMessage(null);
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 5_000);
    try {
      const response = await apiFetch(API_URL + "/api/settings/model-fabric", { signal: controller.signal });
      if (!response.ok) throw new Error("Settings refresh failed: " + response.status);
      const next = normalizeModelFabricSettings(await response.json());
      if (!next || !Number.isSafeInteger(next.egress_revision) || (next.egress_revision ?? 0) < 1) throw new Error("Current settings revision is unavailable.");
      if (!mounted.current) return;
      // Read-only refresh preserves edits. Review and save remain explicit.
      setMetadata(next.openrouter_setup ?? metadata);
      setMetadataRetained(!next.openrouter_setup && Boolean(metadata));
      setRevision(next.egress_revision);
      setRevoked(next.egress_revoked ?? false);
      setPurposeAcks({ vision: false, embedding: false });
      if (next.egress_revoked) setDraft((current) => ({ ...current, cloudEgressAcknowledged: false }));
      setRefreshMessage("Current settings loaded; edits retained. Review routes and acknowledgments before saving.");
    } catch (refreshError) {
      if (mounted.current) setRefreshMessage(safeError(refreshError));
    } finally {
      window.clearTimeout(timeout);
      if (mounted.current) setRefreshing(false);
    }
  }
  async function save() {
    setSaving(true);
    setError(null);
    try {
      if (!Number.isSafeInteger(revision) || (revision ?? 0) < 1) throw new Error("Refresh current settings to obtain the policy revision before saving.");
      const ceiling = boundedNumber(draft.spendCeilingMicrousd, "Spend ceiling", 1, 1_000_000_000, true);
      if (!draft.cloudEgressAcknowledged) throw new Error("Acknowledge cloud egress before saving.");
      const routes = { text: routeValue(draft.routes.text, "text", ceiling), vision: routeValue(draft.routes.vision, "vision", ceiling), embedding: routeValue(draft.routes.embedding, "embedding", ceiling) };
      for (const slot of ["vision", "embedding"] as const) {
        if (needsAck(slot) && !purposeAcks[slot]) throw new Error("Acknowledge " + slot + " egress for the changed route before saving.");
      }
      const value: OpenRouterSetupValue = {
        schema_version: "seraph.openrouter.setup.v2", routes, egress_class: draft.egressClass,
        cloud_egress_acknowledged: true, spend_ceiling_microusd: ceiling,
        max_queued: boundedNumber(draft.maxQueued, "Max queued", 1, 64, true), max_inflight: 1,
        max_outstanding_per_owner: boundedNumber(draft.maxOutstandingPerOwner, "Max outstanding per owner", 1, 16, true),
        max_retries: boundedNumber(draft.maxRetries, "Max retries", 0, 2, true),
        data_collection: "deny", data_retention_policy: "deny", allow_fallbacks: false, require_parameters: true,
        ...(credential.trim() ? { api_key: credential } : {}),
        ...(needsAck("vision") && purposeAcks.vision ? { vision_egress_acknowledged: true as const } : {}),
        ...(needsAck("embedding") && purposeAcks.embedding ? { embedding_egress_acknowledged: true as const } : {}),
      };
      const next = await onSave({ expected_policy_revision: revision, openrouter_setup: value });
      if (!mounted.current) return;
      setCredential("");
      if (!next.openrouter_setup) throw new Error("Setup saved without complete metadata; refresh settings to verify the result.");
      dirty.current = false;
      setMetadata(next.openrouter_setup);
      setMetadataRetained(false);
      setRevision(next.egress_revision);
      setRevoked(next.egress_revoked ?? false);
      setDraft(draftFromSetup(next.openrouter_setup, next.egress_revoked));
      setPurposeAcks({ vision: false, embedding: false });
      setRefreshMessage(null);
    } catch (saveError) {
      if (mounted.current) setError(safeError(saveError));
    } finally {
      if (mounted.current) setSaving(false);
    }
  }
  return (
    <div className="mt-2 border border-retro-text/10 px-2 py-2" data-testid="openrouter-setup-panel">
      <div className="flex items-center justify-between gap-2 mb-1">
        <div className="text-[10px] text-retro-text">OpenRouter setup</div>
        <div className="text-[9px] text-retro-text/60">{metadataRetained ? "stale retained" : (metadata?.status ?? "configuration_required").replace(/_/g, " ")}</div>
      </div>
      <div className="mb-2 text-[9px] text-retro-text/50">
        Fixed route https://openrouter.ai/api/v1 · save and status never call a provider. Manual canary remains explicit.
        All purposes share one deployment ceiling and one in-flight request. Readiness reflects capability proof only.
      </div>
      {metadata?.error_code && <div className="mb-1 text-[9px] text-red-400">{metadata.error_code}</div>}
      {PURPOSES.map((slot) => {
        const route = draft.routes[slot];
        const status = metadata?.slot_statuses[slot] ?? { status: "configuration_required", error_code: "route_missing", proof_expires_at: null };
        return (
          <fieldset key={slot} className="mb-2 border border-retro-text/10 px-2 py-1 text-[9px]" aria-label={"OpenRouter " + slot + " route"}>
            <legend className="text-retro-text uppercase">{slot}</legend>
            <div data-testid={"openrouter-" + slot + "-status"} className={status.status === "ready" ? "text-green-400" : "text-yellow-400"}>
              {status.status.replace(/_/g, " ")}{status.error_code ? " · " + status.error_code : ""}{status.proof_expires_at ? " · proof expires " + status.proof_expires_at : ""}
            </div>
            <label className="flex items-center gap-1 my-1 text-retro-text/70">
              <input type="checkbox" aria-label={"Enable OpenRouter " + slot + " route"} checked={route.enabled} onChange={(event) => updateRoute(slot, "enabled", event.target.checked)} /> enable {slot}
            </label>
            <div className="grid grid-cols-[120px_minmax(0,1fr)] gap-2">
              <label htmlFor={"openrouter-" + slot + "-model"} className="text-retro-text/40">Model ID</label>
              <input id={"openrouter-" + slot + "-model"} aria-label={"OpenRouter " + slot + " model ID"} value={route.modelId} onChange={(event) => updateRoute(slot, "modelId", event.target.value)} className={inputClass} placeholder="provider/model" />
              <label htmlFor={"openrouter-" + slot + "-upstreams"} className="text-retro-text/40">Upstream allow-list</label>
              <input id={"openrouter-" + slot + "-upstreams"} aria-label={"OpenRouter " + slot + " upstream allow-list"} value={route.allowedUpstreams} onChange={(event) => updateRoute(slot, "allowedUpstreams", event.target.value)} className={inputClass} placeholder="explicit upstreams" />
              <div className="text-retro-text/40">Capabilities</div>
              <div className="flex flex-wrap gap-2 text-retro-text/70">{CAPABILITIES[slot].map((capability) => (
                <label key={capability} className="flex items-center gap-1">
                  <input type="checkbox" aria-label={"OpenRouter " + slot + " " + capability + " capability"} checked={route.capabilities.includes(capability)} onChange={() => updateRoute(slot, "capabilities", route.capabilities.includes(capability) ? route.capabilities.filter((item) => item !== capability) : [...route.capabilities, capability])} />{capability.replace(/_/g, " ")}
                </label>
              ))}</div>
              {([
                ["temperature", "Temperature", 0, 2],
                ["maxOutputTokens", "Max output tokens", 1, 131072],
                ["timeoutSeconds", "Timeout seconds", 1, 120],
                ["requestCostBoundMicrousd", "Request cost bound", 1, 1000000000],
              ] as const).map(([field, label, min, max]) => (
                <div key={field} className="contents">
                  <label htmlFor={"openrouter-" + slot + "-" + field} className="text-retro-text/40">{label}</label>
                  <input id={"openrouter-" + slot + "-" + field} aria-label={"OpenRouter " + slot + " " + label.toLowerCase()} type="number" min={min} max={max} step={field === "temperature" ? "0.1" : "1"} value={route[field]} onChange={(event) => updateRoute(slot, field, event.target.value)} className={inputClass} />
                </div>
              ))}
              <div className="text-retro-text/40">Retention</div>
              <label className="flex items-center gap-1 text-retro-text/70">
                <input type="checkbox" aria-label={"Enable OpenRouter " + slot + " zero data retention"} checked={route.zeroDataRetention} onChange={(event) => updateRoute(slot, "zeroDataRetention", event.target.checked)} />zero data retention{slot !== "text" ? " (required when enabled)" : ""}
              </label>
            </div>
            {slot !== "text" && needsAck(slot) && <label className="flex items-center gap-1 mt-2 text-retro-text/70">
              <input type="checkbox" aria-label={"Acknowledge OpenRouter " + slot + " egress"} checked={purposeAcks[slot]} onChange={(event) => setPurposeAcks((current) => ({ ...current, [slot]: event.target.checked }))} />
              acknowledge {slot} egress for this route; source permissions remain required
            </label>}
          </fieldset>
        );
      })}
      <div className="grid grid-cols-[120px_minmax(0,1fr)] gap-2 text-[9px]">
        <div className="text-retro-text/40">Shared data policy</div>
        <div className="text-retro-text/70">collection deny · retention deny · fallbacks blocked</div>
        <label htmlFor="openrouter-egress" className="text-retro-text/40">Cloud egress</label>
        <select id="openrouter-egress" aria-label="OpenRouter cloud egress" value={draft.egressClass} onChange={(event) => update("egressClass", event.target.value)} className={inputClass}>
          <option value="cloud_allowed_full">cloud allowed full</option>
          <option value="cloud_allowed_redacted">cloud allowed redacted</option>
        </select>
        <div className="text-retro-text/40">Cloud consent</div>
        <label className="flex items-center gap-1 text-retro-text/70"><input type="checkbox" aria-label="Acknowledge OpenRouter cloud egress" checked={draft.cloudEgressAcknowledged} onChange={(event) => update("cloudEgressAcknowledged", event.target.checked)} />acknowledge cloud egress</label>
        <label htmlFor="openrouter-spend" className="text-retro-text/40">Monthly ceiling (micro USD)</label>
        <input id="openrouter-spend" aria-label="OpenRouter spend ceiling" type="number" min="1" max="1000000000" value={draft.spendCeilingMicrousd} onChange={(event) => update("spendCeilingMicrousd", event.target.value)} className={inputClass} />
        <div className="text-retro-text/40">Shared queue bounds</div>
        <div className="grid grid-cols-4 gap-1">
          <input aria-label="OpenRouter max queued" title="max queued" type="number" min="1" max="64" value={draft.maxQueued} onChange={(event) => update("maxQueued", event.target.value)} className={inputClass} />
          <input aria-label="OpenRouter max in flight" title="max in flight" type="number" value="1" readOnly className={inputClass} />
          <input aria-label="OpenRouter max outstanding per owner" title="owner outstanding" type="number" min="1" max="16" value={draft.maxOutstandingPerOwner} onChange={(event) => update("maxOutstandingPerOwner", event.target.value)} className={inputClass} />
          <input aria-label="OpenRouter max retries" title="max retries" type="number" min="0" max="2" value={draft.maxRetries} onChange={(event) => update("maxRetries", event.target.value)} className={inputClass} />
        </div>
        <label htmlFor="openrouter-api-key" className="text-retro-text/40">Shared API key</label>
        <input id="openrouter-api-key" aria-label="OpenRouter API key (write-only)" type="password" autoComplete="new-password" value={credential} onChange={(event) => { dirty.current = true; setCredential(event.target.value); }} className={inputClass} placeholder={metadata?.credential_configured ? "leave blank to keep existing" : "write-only · optional until later"} />
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-2 text-[9px]">
        <button type="button" disabled={saving || refreshing} onClick={() => void save()} className="border border-retro-text/20 px-2 py-1 text-retro-text/70 disabled:opacity-40">{saving ? "Saving" : revoked ? "Review and re-grant OpenRouter egress" : "Save OpenRouter setup"}</button>
        <button type="button" disabled={saving || refreshing} onClick={() => void refresh()} className="border border-retro-text/20 px-2 py-1 text-retro-text/70 disabled:opacity-40">{refreshing ? "Refreshing" : "Refresh current OpenRouter settings"}</button>
        <span className="text-retro-text/40">{metadata?.credential_configured ? "key configured · fingerprint " + (metadata.credential_fingerprint ?? "unavailable") : "configuration required · no key stored"} · revision {revision ?? "unavailable"}</span>
      </div>
      {error && <div role="alert" className="mt-1 text-[9px] text-red-400">{error}{error.includes("409") ? " · Settings changed. Refresh current settings, review retained edits, then save explicitly." : ""}</div>}
      {refreshMessage && <div role="status" className="mt-1 text-[9px] text-retro-text/70">{refreshMessage}</div>}
    </div>
  );
}
