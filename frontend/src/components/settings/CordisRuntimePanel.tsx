import { useEffect, useRef, useState } from "react";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import { blockedRuntimeDomains, chatRuntimeLabel, guardianRuntimeLabel, normalizeChatRuntime, normalizeCordisRuntime,
  normalizeGuardianRuntime, normalizeRuntimeDomains } from "../../lib/cordisRuntime";
import type { ChatRuntimeStatus, CordisRuntimeSnapshot, GuardianRuntimeStatus, RuntimeDomainStatus } from "../../lib/cordisRuntime";

const CHAT_DOMAIN = "seraph.agent-loop.v1";

export function CordisRuntimePanel({ metadata, managed = false }: { metadata?: unknown; managed?: boolean }) {
  const [snapshot, setSnapshot] = useState<CordisRuntimeSnapshot | null>(null);
  const [chat, setChat] = useState<ChatRuntimeStatus | null>(null);
  const [guardian, setGuardian] = useState<GuardianRuntimeStatus | null>(null);
  const [domains, setDomains] = useState<RuntimeDomainStatus[]>([]);
  const [busy, setBusy] = useState(false), [error, setError] = useState<string | null>(null);
  const generation = useRef(0), controller = useRef<AbortController | null>(null);
  async function refresh() {
    controller.current?.abort(); const abort = new AbortController(); controller.current = abort;
    const version = ++generation.current; setBusy(true);
    const timeout = setTimeout(() => abort.abort(), 5000);
    try {
      const response = await apiFetch(`${API_URL}/api/runtime/status`, { signal: abort.signal });
      if (!response.ok) throw Error("Runtime metadata unavailable");
      const value: unknown = await response.json();
      const result = normalizeCordisRuntime(value && typeof value === "object" && "cordis_runtime" in value ? value.cordis_runtime : null);
      if (!result) throw Error("Cordis runtime metadata missing or invalid");
      const chatStatus = normalizeChatRuntime(value && typeof value === "object" && "chat_runtime" in value ? value.chat_runtime : null);
      const guardianStatus = normalizeGuardianRuntime(value && typeof value === "object" && "guardian_runtime" in value ? value.guardian_runtime : null);
      const domainRows = normalizeRuntimeDomains(value && typeof value === "object" && "runtime_domains" in value ? value.runtime_domains : null);
      if (version === generation.current) { setSnapshot(result); setChat(chatStatus); setGuardian(guardianStatus); setDomains(domainRows); setError(null); }
    } catch { if (version === generation.current) setError("Cordis runtime metadata is unavailable. Last confirmed state remains visible; refresh after restoring the host. Artifact settings remain usable."); }
    finally { clearTimeout(timeout); if (version === generation.current) setBusy(false); }
  }
  useEffect(() => { if (!managed) void refresh(); return () => { ++generation.current; controller.current?.abort(); }; }, [managed]);
  const [moving, setMoving] = useState(false), [moveError, setMoveError] = useState<string | null>(null);
  const [moveNotice, setMoveNotice] = useState<string | null>(null);
  async function moveChat(target: "legacy" | "cordis") {
    if (!chat || chat.owner_epoch === null) return;
    const label = target === "legacy" ? "roll chat back to the legacy agent loop" : "cut chat over to the Cordis agent loop";
    if (!window.confirm(`Confirm: ${label} (${CHAT_DOMAIN}). The change applies when Seraph restarts.`)) return;
    setMoving(true); setMoveError(null); setMoveNotice(null);
    try {
      const response = await apiFetch(`${API_URL}/api/runtime/composition/domains/${CHAT_DOMAIN}/owner`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ target, expected_epoch: chat.owner_epoch, confirm_domain: CHAT_DOMAIN }) });
      if (!response.ok) {
        const detail: unknown = await response.json().catch(() => null);
        const code = detail && typeof detail === "object" && "detail" in detail && detail.detail && typeof detail.detail === "object"
          && "code" in detail.detail ? String(detail.detail.code) : `HTTP ${response.status}`;
        setMoveError(`Chat loop change refused: ${code}`);
      } else setMoveNotice("Requested. Restart Seraph (./manage.sh -e dev local up) to apply the chat loop change.");
    } catch { setMoveError("Chat loop change failed; refresh and try again."); }
    finally { setMoving(false); void refresh(); }
  }
  useEffect(() => {
    if (!managed) return;
    const result = normalizeCordisRuntime(metadata);
    if (result) { setSnapshot(result); setError(null); }
    else setError("Cordis runtime metadata is unavailable. Last confirmed state remains visible; refresh after restoring the host. Artifact settings remain usable.");
  }, [managed, metadata]);
  return <section aria-label="Cordis runtime host" className="mb-3 rounded border border-retro-text/10 p-2 text-[10px]">
    <div className="flex items-center justify-between gap-2"><h3>Cordis runtime host</h3><button type="button" disabled={busy} onClick={() => void refresh()}>{busy ? "Checking host…" : "Refresh Cordis host"}</button></div>
    <p>This lifecycle host reports composition and cleanup. Chat runs on the agent loop its owner row names.</p>
    {chat && <p role="status" aria-label="Chat agent loop">{chatRuntimeLabel(chat)}</p>}
    {chat?.loop === "blocked" && chat.recovery && <p role="status">Chat fix: {chat.recovery}</p>}
    {chat && chat.owner_epoch !== null && (chat.owner_kind === "cordis"
      ? <button type="button" disabled={moving} onClick={() => void moveChat("legacy")}>Roll chat back to legacy</button>
      : chat.owner_kind === "legacy" && <button type="button" disabled={moving} onClick={() => void moveChat("cordis")}>Cut chat over to Cordis</button>)}
    {moveError && <p role="status" className="text-yellow-400">{moveError}</p>}
    {moveNotice && <p role="status">{moveNotice}</p>}
    {guardian && <p role="status" aria-label="Guardian loop">{guardianRuntimeLabel(guardian)}</p>}
    {guardian?.loop === "blocked" && guardian.recovery && <p role="status">Guardian fix: {guardian.recovery}</p>}
    {blockedRuntimeDomains(domains).length > 0 && <ul aria-label="Runtime domains needing action">
      {blockedRuntimeDomains(domains).map(item => <li key={item.domain}>{item.domain}: {item.state}{item.reason ? ` · ${item.reason}` : ""}{item.recovery ? ` · fix: ${item.recovery}` : ""}</li>)}
    </ul>}
    {error && <p role="status" className="text-yellow-400">{error}</p>}
    {!snapshot && <p role="status">{busy ? "Loading host state…" : "Host state unavailable"}</p>}
    {snapshot && <>
      <p role="status">{error ? "Host readiness unknown · stale metadata" : `${snapshot.state}${snapshot.reason ? ` · ${snapshot.reason}` : ""}`}</p>
      <p>Readiness: {error ? "unknown" : snapshot.readiness.state} · last verified: {snapshot.readiness.checked_at === null ? "unavailable" : new Date(snapshot.readiness.checked_at).toISOString()}</p>
      <p>Profile: {snapshot.profile_id ?? "none"} · Cordis {snapshot.cordis_version} · Node {snapshot.node_version ?? "unavailable"}</p>
      <p>Composition: {snapshot.composition_digest ?? "unavailable"}</p><p>Package: {snapshot.package_digest ?? "unavailable"}</p>
      <ul>{snapshot.plugins.map(p => <li key={p.id}>{p.id}: {error ? "unknown · stale metadata" : `${p.state}${p.reason ? ` · ${p.reason}` : ""}`}</li>)}</ul>
      <p>Cleanup: {snapshot.cleanup.state} · process {snapshot.cleanup.process_reaped ? "reaped" : "unconfirmed"} · resources {snapshot.cleanup.resources_remaining ?? "unknown"} · Cordis disposal {snapshot.cleanup.cordis_disposal}</p>
      {!error && snapshot.state !== "ready" && snapshot.recovery && <p role="status">Fix: {snapshot.recovery}</p>}
      {(error || snapshot.state !== "ready") && <p>Dependent work stays blocked until mandatory services are ready. Restore the reviewed host build and supported Node profile, then refresh. Unknown cleanup requires reconciliation before restart.</p>}
    </>}
  </section>;
}
