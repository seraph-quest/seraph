import { useEffect, useState } from "react";
import type { ModelFabricSettingsStatus, OpenRouterSetupStatus } from "../../lib/modelFabric";

const CAPABILITIES = [
  "text",
  "vision",
  "tool_use",
  "structured_output",
  "streaming",
  "embedding",
] as const;

interface OpenRouterSetupDraft {
  modelIds: string;
  capabilities: string[];
  temperature: string;
  maxOutputTokens: string;
  timeoutSeconds: string;
  allowedUpstreams: string;
  zeroDataRetention: boolean;
  egressClass: string;
  cloudEgressAcknowledged: boolean;
  spendCeilingMicrousd: string;
  maxQueued: string;
  maxInflight: string;
  maxOutstandingPerOwner: string;
  maxRetries: string;
}

interface OpenRouterSetupPanelProps {
  setup: OpenRouterSetupStatus | null | undefined;
  stale: boolean;
  onSave: (payload: Record<string, unknown>) => Promise<ModelFabricSettingsStatus>;
}

function draftFromSetup(setup: OpenRouterSetupStatus | null | undefined): OpenRouterSetupDraft {
  return {
    modelIds: setup?.model_ids.join(", ") ?? "z-ai/glm-5.3-flash",
    capabilities: setup?.capabilities.length ? [...setup.capabilities] : ["text", "structured_output"],
    temperature: String(setup?.temperature ?? 0.7),
    maxOutputTokens: String(setup?.max_output_tokens ?? 4096),
    timeoutSeconds: String(setup?.timeout_seconds ?? 120),
    allowedUpstreams: setup?.allowed_upstreams.join(", ") ?? "z-ai",
    zeroDataRetention: setup?.zero_data_retention ?? false,
    egressClass: setup?.egress_class ?? "cloud_allowed_full",
    cloudEgressAcknowledged: setup?.cloud_egress_acknowledged ?? false,
    spendCeilingMicrousd: setup?.spend_ceiling_microusd == null ? "" : String(setup.spend_ceiling_microusd),
    maxQueued: String(setup?.max_queued ?? 64),
    maxInflight: String(setup?.max_inflight ?? 1),
    maxOutstandingPerOwner: String(setup?.max_outstanding_per_owner ?? 16),
    maxRetries: String(setup?.max_retries ?? 2),
  };
}

function splitList(value: string): string[] {
  return value.split(",").map((item) => item.trim()).filter(Boolean);
}

function boundedNumber(value: string, label: string, min: number, max: number, integer = false): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed < min || parsed > max || (integer && !Number.isInteger(parsed))) {
    throw new Error(`${label} must be a finite value between ${min} and ${max}${integer ? " (whole number)" : ""}.`);
  }
  return parsed;
}

function inputClass(): string {
  return "min-w-0 border border-retro-text/20 bg-retro-bg px-1 py-0.5 text-retro-text disabled:opacity-40";
}

export function OpenRouterSetupPanel({ setup, stale, onSave }: OpenRouterSetupPanelProps) {
  const [draft, setDraft] = useState<OpenRouterSetupDraft>(() => draftFromSetup(setup));
  const [credential, setCredential] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setDraft(draftFromSetup(setup));
    // A successful response never contains the key. Clear the write-only
    // field whenever server metadata replaces the draft.
    setCredential("");
    setError(null);
  }, [setup]);

  const update = <K extends keyof OpenRouterSetupDraft>(field: K, value: OpenRouterSetupDraft[K]) => {
    setDraft((current) => ({ ...current, [field]: value }));
  };

  const toggleCapability = (capability: string) => {
    setDraft((current) => {
      const selected = current.capabilities.includes(capability)
        ? current.capabilities.filter((item) => item !== capability)
        : [...current.capabilities, capability];
      return {
        ...current,
        // The backend keeps embeddings as a separate transport contract.
        capabilities: capability === "embedding" && selected.includes("embedding")
          ? ["embedding"]
          : selected.filter((item) => item !== "embedding"),
      };
    });
  };

  async function save() {
    setSaving(true);
    setError(null);
    try {
      const modelIds = splitList(draft.modelIds);
      const allowedUpstreams = splitList(draft.allowedUpstreams);
      if (!modelIds.length) throw new Error("At least one OpenRouter model ID is required.");
      if (!draft.capabilities.length) throw new Error("Select at least one OpenRouter capability.");
      if (!allowedUpstreams.length) throw new Error("At least one upstream is required for the allow-list.");
      if (!draft.spendCeilingMicrousd.trim()) throw new Error("A finite spend ceiling is required.");
      if (!draft.cloudEgressAcknowledged) throw new Error("Acknowledge cloud egress before saving.");
      if (hasVisionOrEmbedding && !draft.zeroDataRetention) throw new Error("Zero data retention is required for vision and embedding.");
      const temperature = boundedNumber(draft.temperature, "Temperature", 0, 2);
      const maxOutputTokens = boundedNumber(draft.maxOutputTokens, "Max output tokens", 1, 131072, true);
      const timeoutSeconds = boundedNumber(draft.timeoutSeconds, "Timeout", 1, 120);
      const spendCeilingMicrousd = boundedNumber(draft.spendCeilingMicrousd, "Spend ceiling", 1, 1_000_000_000, true);
      const maxQueued = boundedNumber(draft.maxQueued, "Max queued", 1, 64, true);
      const maxInflight = boundedNumber(draft.maxInflight, "Max in flight", 1, 1, true);
      const maxOutstandingPerOwner = boundedNumber(draft.maxOutstandingPerOwner, "Max outstanding per owner", 1, 16, true);
      const maxRetries = boundedNumber(draft.maxRetries, "Max retries", 0, 2, true);
      const payload: Record<string, unknown> = {
        openrouter: {
          model_ids: modelIds,
          capabilities: draft.capabilities,
          temperature,
          max_output_tokens: maxOutputTokens,
          timeout_seconds: timeoutSeconds,
          allowed_upstreams: allowedUpstreams,
          allow_fallbacks: false,
          require_parameters: true,
          data_collection: "deny",
          data_retention_policy: "deny",
          zero_data_retention: draft.zeroDataRetention,
          egress_class: draft.egressClass,
          cloud_egress_acknowledged: draft.cloudEgressAcknowledged,
          spend_ceiling_microusd: spendCeilingMicrousd,
          max_queued: maxQueued,
          max_inflight: maxInflight,
          max_outstanding_per_owner: maxOutstandingPerOwner,
          max_retries: maxRetries,
          ...(credential.trim() ? { api_key: credential } : {}),
        },
      };
      const nextSettings = await onSave(payload);
      setCredential("");
      setDraft(draftFromSetup(nextSettings.openrouter_setup));
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : "OpenRouter setup could not be saved.");
    } finally {
      setSaving(false);
    }
  }

  const status = setup?.status ?? "configuration_required";
  const hasVisionOrEmbedding = draft.capabilities.includes("vision") || draft.capabilities.includes("embedding");

  return (
    <div className="mt-2 border border-retro-text/10 px-2 py-2" data-testid="openrouter-setup-panel">
      <div className="flex items-center justify-between gap-2 mb-1">
        <div className="text-[10px] text-retro-text">OpenRouter setup</div>
        <div className={`text-[9px] uppercase tracking-wider ${
          status === "configured_unverified" ? "text-yellow-400" : "text-red-400"
        }`}>
          {stale ? "stale retained" : status.replace(/_/g, " ")}
        </div>
      </div>
      <div className="mb-2 text-[9px] text-retro-text/50">
        Fixed route https://openrouter.ai/api/v1 · save and status never call a provider. Manual canary remains explicit.
      </div>
      <div className="grid grid-cols-[120px_minmax(0,1fr)] gap-2 text-[9px]">
        <label className="text-retro-text/40 uppercase tracking-wider" htmlFor="openrouter-model-ids">Model IDs</label>
        <input
          id="openrouter-model-ids"
          aria-label="OpenRouter model IDs"
          value={draft.modelIds}
          onChange={(event) => update("modelIds", event.target.value)}
          className={inputClass()}
          placeholder="provider/model"
        />
        <div className="text-retro-text/40 uppercase tracking-wider">Capabilities</div>
        <div className="flex flex-wrap gap-x-2 gap-y-1">
          {CAPABILITIES.map((capability) => (
            <label key={capability} className="flex items-center gap-1 text-retro-text/70">
              <input
                type="checkbox"
                aria-label={`OpenRouter ${capability} capability`}
                checked={draft.capabilities.includes(capability)}
                onChange={() => toggleCapability(capability)}
              />
              {capability.replace(/_/g, " ")}
            </label>
          ))}
        </div>
        <label className="text-retro-text/40 uppercase tracking-wider" htmlFor="openrouter-temperature">Temperature</label>
        <input id="openrouter-temperature" aria-label="OpenRouter temperature" type="number" min="0" max="2" step="0.1" value={draft.temperature} onChange={(event) => update("temperature", event.target.value)} className={inputClass()} />
        <label className="text-retro-text/40 uppercase tracking-wider" htmlFor="openrouter-max-output">Max output tokens</label>
        <input id="openrouter-max-output" aria-label="OpenRouter max output tokens" type="number" min="1" max="131072" value={draft.maxOutputTokens} onChange={(event) => update("maxOutputTokens", event.target.value)} className={inputClass()} />
        <label className="text-retro-text/40 uppercase tracking-wider" htmlFor="openrouter-timeout">Timeout seconds</label>
        <input id="openrouter-timeout" aria-label="OpenRouter timeout seconds" type="number" min="1" max="120" value={draft.timeoutSeconds} onChange={(event) => update("timeoutSeconds", event.target.value)} className={inputClass()} />
        <label className="text-retro-text/40 uppercase tracking-wider" htmlFor="openrouter-upstreams">Upstream allow-list *</label>
        <input id="openrouter-upstreams" aria-label="OpenRouter upstream allow-list" value={draft.allowedUpstreams} onChange={(event) => update("allowedUpstreams", event.target.value)} className={inputClass()} placeholder="z-ai" />
        <div className="text-retro-text/40 uppercase tracking-wider">Data policy</div>
        <div className="text-retro-text/70">collection deny · retention deny · fallbacks blocked</div>
        <label className="text-retro-text/40 uppercase tracking-wider" htmlFor="openrouter-egress">Cloud egress</label>
        <select id="openrouter-egress" aria-label="OpenRouter cloud egress" value={draft.egressClass} onChange={(event) => update("egressClass", event.target.value)} className={inputClass()}>
          <option value="cloud_allowed_full">cloud allowed full</option>
          <option value="cloud_allowed_redacted">cloud allowed redacted</option>
        </select>
        <div className="text-retro-text/40 uppercase tracking-wider">Consent and retention</div>
        <div className="flex flex-wrap gap-x-2 gap-y-1 text-retro-text/70">
          <label className="flex items-center gap-1">
            <input type="checkbox" aria-label="Acknowledge OpenRouter cloud egress" checked={draft.cloudEgressAcknowledged} onChange={(event) => update("cloudEgressAcknowledged", event.target.checked)} />
            acknowledge cloud egress
          </label>
          <label className="flex items-center gap-1">
            <input type="checkbox" aria-label="Enable OpenRouter zero data retention" checked={draft.zeroDataRetention} onChange={(event) => update("zeroDataRetention", event.target.checked)} />
            zero data retention{hasVisionOrEmbedding ? " (required)" : ""}
          </label>
        </div>
        <label className="text-retro-text/40 uppercase tracking-wider" htmlFor="openrouter-spend">Spend ceiling (micro USD)</label>
        <input id="openrouter-spend" aria-label="OpenRouter spend ceiling" type="number" min="1" max="1000000000" value={draft.spendCeilingMicrousd} onChange={(event) => update("spendCeilingMicrousd", event.target.value)} className={inputClass()} placeholder="required" />
        <div className="text-retro-text/40 uppercase tracking-wider">Queue bounds</div>
        <div className="grid grid-cols-4 gap-1">
          <input aria-label="OpenRouter max queued" type="number" min="1" max="64" value={draft.maxQueued} onChange={(event) => update("maxQueued", event.target.value)} className={inputClass()} title="max queued" />
          <input aria-label="OpenRouter max in flight" type="number" min="1" max="1" value={draft.maxInflight} onChange={(event) => update("maxInflight", event.target.value)} className={inputClass()} title="max in flight" />
          <input aria-label="OpenRouter max outstanding per owner" type="number" min="1" max="16" value={draft.maxOutstandingPerOwner} onChange={(event) => update("maxOutstandingPerOwner", event.target.value)} className={inputClass()} title="owner outstanding" />
          <input aria-label="OpenRouter max retries" type="number" min="0" max="2" value={draft.maxRetries} onChange={(event) => update("maxRetries", event.target.value)} className={inputClass()} title="max retries" />
        </div>
        <label className="text-retro-text/40 uppercase tracking-wider" htmlFor="openrouter-api-key">API key</label>
        <input
          id="openrouter-api-key"
          aria-label="OpenRouter API key (write-only)"
          type="password"
          autoComplete="new-password"
          value={credential}
          onChange={(event) => setCredential(event.target.value)}
          className={inputClass()}
          placeholder={setup?.credential_configured ? "write-only · leave blank to keep existing" : "write-only · optional until later"}
        />
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <button
          type="button"
          disabled={saving}
          onClick={() => void save()}
          className="border border-retro-text/20 px-2 py-1 text-[9px] uppercase tracking-wider text-retro-text/70 hover:text-retro-text disabled:opacity-40"
        >
          {saving ? "Saving" : "Save OpenRouter setup"}
        </button>
        <span className="text-[9px] text-retro-text/40">
          {setup?.credential_configured ? `key configured · fingerprint ${setup.credential_fingerprint ?? "available"}` : "configuration required · no key stored"}
        </span>
      </div>
      {error && <div role="alert" className="mt-1 text-[9px] text-red-400">{error}</div>}
    </div>
  );
}
