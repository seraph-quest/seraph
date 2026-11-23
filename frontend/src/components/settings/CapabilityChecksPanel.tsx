import { useEffect, useRef, useState } from "react";
import {
  DEFAULT_PROOF_TTL_SECONDS,
  isGreenModelFabricCanary,
  normalizeModelFabricCanary,
  NEAR_TEXT_PROFILE,
  type ModelFabricCanaryResult,
  type ModelFabricSettingsStatus,
} from "../../lib/modelFabric";
import { MODEL_FABRIC_EMPIRICAL_CANARY_CAPABILITIES, postModelFabricCanary } from "./settingsData";

const PROOF_TTL_CHOICES = [
  { seconds: 3600, label: "1 hour" },
  { seconds: 8 * 3600, label: "8 hours" },
  { seconds: 24 * 3600, label: "24 hours" },
] as const;

interface Props {
  modelFabric: ModelFabricSettingsStatus | null;
  modelFabricStale: boolean;
  onRefresh: () => Promise<void>;
}

/**
 * Manual exact-route canaries (#1280 PR 3): moved unchanged from the old first
 * tab into the Diagnostics "Capability checks" fold. Nothing runs until the
 * operator presses a button; "Prove route" stops at the first failure.
 */
export function CapabilityChecksPanel({ modelFabric, modelFabricStale, onRefresh }: Props) {
  const mountedRef = useRef(true);
  useEffect(() => { mountedRef.current = true; return () => { mountedRef.current = false; }; }, []);
  const [canaryProfile, setCanaryProfile] = useState("");
  const [canaryCapability, setCanaryCapability] = useState("text");
  const [canaryRunning, setCanaryRunning] = useState(false);
  const [canaryResult, setCanaryResult] = useState<ModelFabricCanaryResult | null>(null);
  const [canaryError, setCanaryError] = useState<string | null>(null);
  const [proofTtlSeconds, setProofTtlSeconds] = useState<number>(DEFAULT_PROOF_TTL_SECONDS);
  const [proveProgress, setProveProgress] = useState<{ done: number; total: number; current: string } | null>(null);
  const [proveResults, setProveResults] = useState<ModelFabricCanaryResult[]>([]);

  // Each settings read picks the first enabled non-NEAR profile when none is
  // chosen, and caps the proof lifetime, as the old first tab did.
  useEffect(() => {
    if (!modelFabric) return;
    setProofTtlSeconds((current) => Math.min(current, modelFabric.canary_proof_ttl.max_seconds));
    setCanaryProfile((current) => current && current !== NEAR_TEXT_PROFILE ? current : modelFabric.profiles.find((profile) => profile.enabled && profile.id !== NEAR_TEXT_PROFILE)?.id || "");
  }, [modelFabric]);

  const canaryProfileStatus = modelFabric?.profiles.find((profile) => profile.id === canaryProfile);
  const availableCanaryCapabilities = Array.from(new Set([
    ...(canaryProfileStatus?.capabilities.filter((capability) => MODEL_FABRIC_EMPIRICAL_CANARY_CAPABILITIES.has(capability)) ?? []),
    "health",
    "latency_ms",
  ]));
  const selectedCanaryCapability = availableCanaryCapabilities.includes(canaryCapability)
    ? canaryCapability
    : availableCanaryCapabilities[0] ?? "health";

  async function postCanary(settingsStatus: ModelFabricSettingsStatus, capability: string): Promise<ModelFabricCanaryResult> {
    const selectedProfile = settingsStatus.profiles.find((profile) => profile.id === canaryProfile);
    const payload = await postModelFabricCanary(settingsStatus.canary_endpoint, {
      profile_id: canaryProfile,
      capability,
      timeout_seconds: selectedProfile?.canary_timeout_seconds ?? 10,
      proof_ttl_seconds: Math.min(proofTtlSeconds, settingsStatus.canary_proof_ttl.max_seconds),
    });
    const result = normalizeModelFabricCanary(payload);
    if (!result) throw new Error("Canary response is invalid.");
    return result;
  }

  async function runModelFabricCanary() {
    if (!modelFabric || !canaryProfile || canaryProfile === NEAR_TEXT_PROFILE || canaryRunning) return;
    setCanaryRunning(true);
    setCanaryResult(null);
    setCanaryError(null);
    try {
      const result = await postCanary(modelFabric, selectedCanaryCapability);
      if (!mountedRef.current) return;
      setCanaryResult(result);
      await onRefresh();
    } catch (error) {
      if (mountedRef.current) {
        setCanaryError(error instanceof Error ? error.message : "Model-fabric canary failed.");
      }
    } finally {
      if (mountedRef.current) setCanaryRunning(false);
    }
  }

  // Runs every capability the selected route needs, one billed canary at a
  // time on the shared single-flight lane, and stops at the first failure so
  // a broken route does not keep spending.
  async function proveRoute() {
    if (!modelFabric || !canaryProfile || canaryProfile === NEAR_TEXT_PROFILE || canaryRunning) return;
    const capabilities = [...availableCanaryCapabilities];
    setCanaryRunning(true);
    setCanaryResult(null);
    setCanaryError(null);
    setProveResults([]);
    try {
      for (const [index, capability] of capabilities.entries()) {
        setProveProgress({ done: index, total: capabilities.length, current: capability });
        const result = await postCanary(modelFabric, capability);
        if (!mountedRef.current) return;
        setProveResults((current) => [...current, result]);
        if (!isGreenModelFabricCanary(result)) {
          setCanaryError(`Prove route stopped at ${capability.replace(/_/g, " ")}: ${result.error_code ?? result.outcome}. Remaining canaries were not run.`);
          break;
        }
      }
      await onRefresh();
    } catch (error) {
      if (mountedRef.current) {
        setCanaryError(error instanceof Error ? error.message : "Prove route failed.");
      }
    } finally {
      if (mountedRef.current) {
        setCanaryRunning(false);
        setProveProgress(null);
      }
    }
  }

  return (
              <div className="border border-retro-text/10 px-2 py-2">
                <div className="text-[9px] text-retro-text/50 mb-1">
                  Manual exact-route canaries. Each canary is one real, billed OpenRouter call and runs only when you press a button; status refreshes never probe.
                  Prove route runs every capability the selected route needs ({availableCanaryCapabilities.length} calls).
                </div>
                <div className="flex flex-wrap items-center gap-1">
                  <label className="text-[9px] text-retro-text/40" htmlFor="model-fabric-canary-profile">Profile</label>
                  <select
                    id="model-fabric-canary-profile"
                    aria-label="Canary profile"
                    value={canaryProfile}
                    disabled={canaryRunning || !modelFabric}
                    onChange={(event) => setCanaryProfile(event.target.value)}
                    className="min-w-0 border border-retro-text/20 bg-retro-bg px-1 py-0.5 text-[9px] text-retro-text disabled:opacity-40"
                  >
                    <option value="">choose profile</option>
                    {modelFabric?.profiles.filter((profile) => profile.id !== NEAR_TEXT_PROFILE).map((profile) => (
                      <option key={profile.id} value={profile.id}>
                        {profile.id} · {profile.transport_adapter}
                      </option>
                    ))}
                  </select>
                  <label className="text-[9px] text-retro-text/40" htmlFor="model-fabric-canary-capability">Capability</label>
                  <select
                    id="model-fabric-canary-capability"
                    aria-label="Canary capability"
                    value={selectedCanaryCapability}
                    disabled={canaryRunning || !modelFabric}
                    onChange={(event) => setCanaryCapability(event.target.value)}
                    className="min-w-0 border border-retro-text/20 bg-retro-bg px-1 py-0.5 text-[9px] text-retro-text disabled:opacity-40"
                  >
                    {availableCanaryCapabilities.map((capability) => (
                      <option key={capability} value={capability}>{capability.replace(/_/g, " ")}</option>
                    ))}
                  </select>
                  <label className="text-[9px] text-retro-text/40" htmlFor="model-fabric-proof-ttl">Proof lifetime</label>
                  <select
                    id="model-fabric-proof-ttl"
                    aria-label="Proof lifetime"
                    value={proofTtlSeconds}
                    disabled={canaryRunning}
                    onChange={(event) => setProofTtlSeconds(Number(event.target.value))}
                    className="min-w-0 border border-retro-text/20 bg-retro-bg px-1 py-0.5 text-[9px] text-retro-text disabled:opacity-40"
                  >
                    {PROOF_TTL_CHOICES.filter((choice) => choice.seconds <= (modelFabric?.canary_proof_ttl.max_seconds ?? DEFAULT_PROOF_TTL_SECONDS)).map((choice) => (
                      <option key={choice.seconds} value={choice.seconds}>{choice.label}</option>
                    ))}
                  </select>
                  <button
                    type="button"
                    disabled={canaryRunning || !canaryProfile || canaryProfile === NEAR_TEXT_PROFILE || modelFabricStale}
                    onClick={() => void runModelFabricCanary()}
                    className="border border-retro-text/20 px-2 py-1 text-[9px] uppercase tracking-wider text-retro-text/70 hover:text-retro-text disabled:opacity-40"
                  >
                    {canaryRunning && !proveProgress ? "Running" : "Run canary"}
                  </button>
                  <button
                    type="button"
                    disabled={canaryRunning || !canaryProfile || canaryProfile === NEAR_TEXT_PROFILE || modelFabricStale}
                    onClick={() => void proveRoute()}
                    className="border border-retro-text/20 px-2 py-1 text-[9px] uppercase tracking-wider text-retro-text/70 hover:text-retro-text disabled:opacity-40"
                  >
                    {proveProgress ? `Proving ${proveProgress.done + 1}/${proveProgress.total}` : "Prove route"}
                  </button>
                </div>
                {proveProgress && (
                  <div role="status" className="mt-1 text-[9px] text-retro-text/60">
                    Running billed canary {proveProgress.done + 1} of {proveProgress.total}: {proveProgress.current.replace(/_/g, " ")}
                  </div>
                )}
                {proveResults.length > 0 && (
                  <div className="mt-1 text-[9px]" data-testid="prove-route-results">
                    {proveResults.map((result) => (
                      <div key={result.capability} className={isGreenModelFabricCanary(result) ? "text-green-400" : "text-yellow-400"}>
                        {result.capability.replace(/_/g, " ")} · {result.outcome}
                        {result.proof?.expires_at ? ` · expires ${result.proof.expires_at}` : ""}
                        {result.error_code ? ` · ${result.error_code}` : ""}
                      </div>
                    ))}
                  </div>
                )}
                {canaryError && <div className="mt-1 text-[9px] text-red-400">{canaryError}</div>}
                {canaryResult && (
                  <div className={`mt-1 text-[9px] ${isGreenModelFabricCanary(canaryResult) ? "text-green-400" : "text-yellow-400"}`}>
                    {canaryResult.profile_id}/{canaryResult.capability} · {canaryResult.outcome}
                    {canaryResult.error_code ? ` · ${canaryResult.error_code}` : ""}
                    {canaryResult.proof ? ` · proof ${canaryResult.proof.proof_hash.slice(0, 12)}` : " · no proof"}
                    {` · receipt ${canaryResult.receipt_persistence}`}
                    {` · proof persistence ${canaryResult.proof_persistence}`}
                    {!isGreenModelFabricCanary(canaryResult) ? " · not authorizing" : ""}
                  </div>
                )}
              </div>
  );
}
