import { useEffect, useRef, useState } from "react";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";

type Selection = { selection_ref: string; selection_digest: string; profile_hash: string; expires_at: string };
type Stage = { staged_ref: string; revision: number; bundle_digest: string; state: string; error_code?: string; coverage?: { missing: string[] } };

/** Documentary acquisition is separate from inference and empirical health. */
export function AudioDocumentationControls() {
  const [keyName, setKeyName] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("Audio admission blocked: exact codec, context/template, complete prices and ZDR sources are missing.");
  const [stage, setStage] = useState<Stage | null>(null);
  const generation = useRef(0);
  useEffect(() => () => { generation.current += 1; }, []);
  async function request(path: string, body?: unknown) {
    const response = await apiFetch(API_URL + path, body === undefined ? {} : {
      method: path.endsWith("/audio-documentation") ? "POST" : "PUT",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail?.code || "audio_documentation_unavailable");
    return result;
  }
  async function act(action: "select" | "acquire" | "reload" | "accept" | "reject") {
    if (busy) return;
    const current = ++generation.current;
    setBusy(true);
    try {
      if (action === "reload") {
        const result = await request("/api/settings/model-fabric/audio-documentation");
        if (current === generation.current) setStage(result.staged?.[0] ?? null);
      } else if (action === "accept" || action === "reject") {
        if (!stage) throw new Error("Original staging receipt is unavailable.");
        const result = await request("/api/settings/model-fabric/audio-documentation", {
          action: action === "accept" ? "accept_staged_documentation" : "reject_staged_documentation",
          staged_ref: stage.staged_ref, expected_staged_revision: stage.revision, expected_bundle_digest: stage.bundle_digest,
        });
        if (current === generation.current) setStage(result);
      } else {
        const settings = await request("/api/settings/model-fabric");
        const profile = settings.profiles?.find((p: { id: string }) => p.id === "openrouter.audio");
        if (!profile?.profile_contract_hash || !Number.isSafeInteger(settings.egress_revision)) throw new Error("Current enabled audio profile is unavailable.");
        if (action === "select") {
          if (!keyName.trim()) throw new Error("Enter the name of an existing owner-private management key.");
          await request("/api/settings/model-fabric", { expected_policy_revision: settings.egress_revision,
            audio_metadata_access: { action: "select_existing_management_credential", vault_key_name: keyName.trim(),
              expected_audio_profile_hash: profile.profile_contract_hash, operation_scope: "selected_audio_profile_metadata" } });
          if (current === generation.current) { setKeyName(""); setNotice("Metadata credential selected. Acquiring performs two explicit documentary reads, without inference."); }
        } else {
          const selection = settings.audio_metadata_access as Selection | null;
          if (!selection) throw new Error("Select an existing owner-private management credential first.");
          const result = await request("/api/settings/model-fabric/audio-documentation", {
            action: "acquire_selected_profile_metadata", expected_egress_revision: settings.egress_revision,
            expected_audio_profile_hash: profile.profile_contract_hash, metadata_selection_ref: selection.selection_ref,
            expected_metadata_selection_digest: selection.selection_digest, official_supplement_catalog_id: "openrouter-audio-guide.v1",
          });
          if (current === generation.current) { setStage(result); setNotice("Metadata retained privately. Availability unverified; incomplete source facts cannot authorize audio."); }
        }
      }
    } catch (error) {
      if (current === generation.current) setNotice(error instanceof Error && error.message === "audio_documentation_cleanup_unknown"
        ? "Acquisition closure is unproven. Private staging remains charged; it cannot be rejected or retried to renew its allowance."
        : error instanceof Error ? error.message : "audio_documentation_unavailable");
    } finally { if (current === generation.current) setBusy(false); }
  }
  return <section aria-label="Audio documentary sources" className="mt-2 text-xs">
    <p>OpenRouter management keys have account-level administrative access. This selection authorizes only two selected-profile metadata reads. It does not authorize inference.</p>
    <p>Successfully settled incomplete sources can be rejected. Failed, interrupted or older unbound acquisitions remain cleanup unknown and charged. Expiry alone does not release their quota.</p>
    <label>Existing private Vault key name<input aria-label="Audio metadata Vault key name" value={keyName} disabled={busy} onChange={e => setKeyName(e.target.value)} maxLength={128} autoComplete="off" /></label>
    <button disabled={busy} onClick={() => void act("select")}>Select metadata credential</button>
    <button disabled={busy} onClick={() => void act("acquire")}>Acquire documentary metadata</button>
    <button disabled={busy} onClick={() => void act("reload")}>Reload original staging</button>
    {stage && <div><p>{stage.state}: {stage.error_code || "availability unverified"}</p>
      <p>{stage.coverage?.missing.join(", ")}</p>
      <button disabled={busy || stage.state !== "staged"} onClick={() => void act("accept")}>Review and accept exact sources</button>
      <button disabled={busy || !["staged", "deleting"].includes(stage.state)} onClick={() => void act("reject")}>Reject and remove staged sources</button>
    </div>}
    <p role="status">{notice}</p>
  </section>;
}
