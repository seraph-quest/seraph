import type { ModelFabricRuntimeStatus, ModelFabricSettingsStatus } from "../../lib/modelFabric";
import {
  ArtifactRow,
  localRuntimeProofTone,
  modelFabricProofLabel,
  modelFabricWorkloadLabel,
  modelFabricWorkloadTone,
  vlmReachabilityLabel,
  vlmReachabilityTone,
  type ArtifactStorageSettings,
} from "./settingsData";

interface Props {
  settings: ArtifactStorageSettings | null;
  modelFabric: ModelFabricSettingsStatus | null;
  modelFabricRuntime: ModelFabricRuntimeStatus | null;
  modelFabricStale: boolean;
  modelFabricError: string | null;
}

/**
 * Raw model-route output (#1280 PR 3): the inference gateway, model-fabric
 * topology, configured and excluded profiles, last text and VLM routes,
 * fallback policy, proof counts and the inactive local runtime. Moved
 * unchanged from the old first tab into a Diagnostics fold.
 */
export function ModelRoutesPanel({ settings, modelFabric, modelFabricRuntime, modelFabricStale, modelFabricError }: Props) {
  return (
    <div className="flex flex-col gap-1">
            {settings?.inference && (
              <div className="border-t border-retro-text/10 pt-2 mt-1">
                <div className="flex items-center justify-between gap-2 mb-1">
                  <div className="text-[10px] text-retro-text">Inference gateway</div>
                  <div className={`text-[9px] uppercase tracking-wider ${
                    settings.inference.status === "ready" ? "text-green-400" : "text-yellow-400"
                  }`}>
                    {settings.inference.status.replace(/_/g, " ")}
                  </div>
                </div>
                <ArtifactRow
                  label="Provider"
                  value={`${settings.inference.provider} · ${settings.inference.active_only ? "only active" : "phase disabled"}`}
                  tone={settings.inference.active_only ? "good" : "warn"}
                />
                <ArtifactRow
                  label="Route"
                  value={`${settings.inference.api_base} · ${settings.inference.credential_configured ? "key configured" : "key missing"}`}
                  tone={settings.inference.credential_configured ? "good" : "warn"}
                />
                <ArtifactRow
                  label="Upstreams"
                  value={settings.inference.allowed_upstreams.length > 0 ? settings.inference.allowed_upstreams.join(", ") : "allow-list required"}
                  tone={settings.inference.allowed_upstreams.length > 0 ? "good" : "warn"}
                />
                <ArtifactRow
                  label="Data policy"
                  value={`${settings.inference.data_collection} · ZDR ${settings.inference.zero_data_retention ? "on" : "off"} · fallbacks ${settings.inference.fallbacks_allowed ? "on" : "off"}`}
                  tone={settings.inference.data_collection === "deny" && !settings.inference.fallbacks_allowed ? "good" : "warn"}
                />
                <ArtifactRow
                  label="Consent"
                  value={`${settings.inference.chat_cloud_egress} · ${settings.inference.chat_cloud_consent ? "acknowledged" : "acknowledgement required"}`}
                  tone={settings.inference.chat_cloud_consent ? "good" : "warn"}
                />
              </div>
            )}

            <div className="border-t border-retro-text/10 pt-2 mt-1">
              <div className="flex items-center justify-between gap-2 mb-1">
                <div className="text-[10px] text-retro-text">Model fabric</div>
                <div className={`text-[9px] uppercase tracking-wider ${
                  modelFabricStale || modelFabric?.status === "degraded" || modelFabricRuntime?.status === "degraded"
                    ? "text-yellow-400"
                    : modelFabric
                      ? "text-green-400"
                      : "text-retro-text/40"
                }`}>
                  {modelFabricStale ? "stale" : modelFabricRuntime?.status ?? modelFabric?.status ?? "loading"}
                </div>
              </div>
              {modelFabricError && (
                <div className="border border-yellow-400/40 px-2 py-1 mb-1 text-[9px] text-yellow-400">
                  {modelFabricError}
                </div>
              )}
              <ArtifactRow
                label="Topology"
                value={
                  modelFabricRuntime
                    ? `text: ${modelFabricRuntime.topology.text.join(", ") || "none"} · VLM: ${modelFabricRuntime.topology.vlm.join(", ") || "none"}`
                    : "text and screenshot VLM route receipts unavailable"
                }
                tone={modelFabricRuntime ? "good" : "normal"}
              />
              <ArtifactRow
                label="Configured"
                value={
                  modelFabric?.profiles.length
                    ? modelFabric.profiles.map((profile) => (
                        `${profile.id}/${profile.model}:${profile.routable ? "routable" : `not routable (${profile.non_routable_reasons.join(", ") || profile.model_fabric_exclusion_reason || "unknown"})`}:cost ${profile.cost_source ?? "unknown"}`
                      )).join(" · ")
                    : "no candidates"
                }
                tone={modelFabric?.profiles.some((profile) => profile.routable) ? "good" : "warn"}
              />
              <ArtifactRow
                label="Excluded"
                value={
                  modelFabric?.excluded_profiles.length
                    ? modelFabric.excluded_profiles.map((profile) => (
                        `${profile.id}/${profile.model}:${profile.model_fabric_exclusion_reason ?? (profile.non_routable_reasons.join(", ") || "excluded")}`
                      )).join(" · ")
                    : "none"
                }
                tone={modelFabric?.excluded_profiles.length ? "warn" : "good"}
              />
              <ArtifactRow
                label="Text"
                value={modelFabricWorkloadLabel(modelFabricRuntime, "chat_agent")}
                tone={modelFabricWorkloadTone(modelFabricRuntime, "chat_agent")}
              />
              <ArtifactRow
                label="VLM"
                value={modelFabricWorkloadLabel(modelFabricRuntime, "screenshot_image_analysis")}
                tone={modelFabricWorkloadTone(modelFabricRuntime, "screenshot_image_analysis")}
              />
              <ArtifactRow
                label="Fallback"
                value={
                  `${(modelFabricRuntime?.runtime_paths.chat_agent ?? modelFabricRuntime?.workloads.interactive)?.fallback_used ? `last used:${(modelFabricRuntime?.runtime_paths.chat_agent ?? modelFabricRuntime?.workloads.interactive)?.fallback_reason_code ?? "reason unknown"}` : "last not used"} · ` +
                  (modelFabric?.workload_policies.length
                    ? modelFabric.workload_policies.map((policy) => `${policy.runtime_path}:${policy.fallback_allowed ? "allowed" : "blocked"}`).join(" · ")
                    : `default ${modelFabric?.defaults.fallback_allowed ? "allowed" : "blocked"}`)
                }
                tone={modelFabric?.workload_policies.some((policy) => policy.fallback_allowed) ? "normal" : "good"}
              />
              <ArtifactRow
                label="Proofs"
                value={modelFabricProofLabel(modelFabricRuntime)}
                tone={
                  modelFabricRuntime?.proofs.some((proof) => proof.status === "stale" || proof.status === "missing")
                    ? "warn"
                    : modelFabricRuntime?.proofs.length
                      ? "good"
                      : "normal"
                }
              />
            </div>

            {settings?.local_runtime && (
              <div className="border-t border-retro-text/10 pt-2 mt-1">
                <div className="flex items-center justify-between gap-2 mb-1">
                  <div className="text-[10px] text-retro-text">
                    {settings.local_runtime.active === false ? "Local runtime (inactive)" : "Local Gemma runtime"}
                  </div>
                  <div
                    className={`text-[9px] uppercase tracking-wider ${
                      localRuntimeProofTone(settings.local_runtime.profile_proof) === "good"
                        ? "text-green-400"
                        : localRuntimeProofTone(settings.local_runtime.profile_proof) === "warn"
                          ? "text-yellow-400"
                          : "text-retro-text/40"
                    }`}
                  >
                    {settings.local_runtime.active === false
                      ? "disabled · OpenRouter-only"
                      : settings.local_runtime.profile_proof.status.replace(/_/g, " ")}
                  </div>
                </div>
                <ArtifactRow
                  label="Gateway"
                  value={settings.local_runtime.gateway_configured ? "configured" : "not configured"}
                  tone={settings.local_runtime.gateway_configured ? "good" : "warn"}
                />
                <ArtifactRow
                  label="Model"
                  value={settings.local_runtime.model || "not configured"}
                  tone={settings.local_runtime.model ? "good" : "warn"}
                />
                <ArtifactRow
                  label="Reach"
                  value={vlmReachabilityLabel(settings.local_runtime.vlm_runtime)}
                  tone={vlmReachabilityTone(settings.local_runtime.vlm_runtime)}
                />
                <ArtifactRow
                  label="Proof"
                  value={settings.local_runtime.profile_proof.per_request_reasoning_control.replace(/_/g, " ")}
                  tone={localRuntimeProofTone(settings.local_runtime.profile_proof)}
                />
                <ArtifactRow
                  label="Routing"
                  value={
                    settings.local_runtime.profile_proof.safe_for_single_backend_profile_routing
                      ? "single backend profile routing safe"
                      : "single backend profile routing not safe"
                  }
                  tone={localRuntimeProofTone(settings.local_runtime.profile_proof)}
                />
                <ArtifactRow
                  label="Profiles"
                  value={
                    settings.local_runtime.profiles.length > 0
                      ? settings.local_runtime.profiles
                          .map((profile) => `${profile.id}:${profile.priority}/${profile.reasoning}`)
                          .join(" · ")
                      : "none"
                  }
                  tone={settings.local_runtime.profiles.length > 0 ? "good" : "normal"}
                />
                <ArtifactRow
                  label="Receipt"
                  value={
                    settings.local_runtime.profile_proof.last_receipt_sha256
                      ? `${settings.local_runtime.profile_proof.last_receipt_sha256.slice(0, 12)} · ${settings.local_runtime.profile_proof.last_receipt_at ?? "time unknown"}`
                      : `${settings.local_runtime.profile_proof.receipt_count} receipts`
                  }
                />
                {settings.local_runtime.profile_proof.notes.length > 0 && (
                  <ArtifactRow
                    label="Note"
                    value={settings.local_runtime.profile_proof.notes.slice(0, 2).join(" · ")}
                    tone="warn"
                  />
                )}
              </div>
            )}
    </div>
  );
}
