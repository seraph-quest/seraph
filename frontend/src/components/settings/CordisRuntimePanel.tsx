import { useEffect, useRef, useState } from "react";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import { blockedRuntimeDomains, chatRuntimeLabel, guardianRuntimeLabel, normalizeChatRuntime, normalizeCordisRuntime,
  normalizeGuardianRuntime, normalizeRuntimeDomains } from "../../lib/cordisRuntime";
import type { ChatRuntimeStatus, CordisRuntimeSnapshot, GuardianRuntimeStatus, RuntimeDomainStatus } from "../../lib/cordisRuntime";

const CHAT_DOMAIN = "seraph.agent-loop.v1";
const DELEGATION_DOMAIN = "seraph.delegation.v1";

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
  async function moveDomain(domain: string, target: "legacy" | "cordis", epoch: number): Promise<string | null> {
    const response = await apiFetch(`${API_URL}/api/runtime/composition/domains/${domain}/owner`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ target, expected_epoch: epoch, confirm_domain: domain }) });
    if (response.ok) return null;
    const detail: unknown = await response.json().catch(() => null);
    return detail && typeof detail === "object" && "detail" in detail && detail.detail && typeof detail.detail === "object"
      && "code" in detail.detail ? String(detail.detail.code) : `HTTP ${response.status}`;
  }
  async function moveChat(target: "legacy" | "cordis") {
    if (!chat || chat.owner_epoch === null) return;
    // Delegation depends on the chat loop (#1020): it rolls back before chat and cuts over after it,
    // so its owner row never reads Cordis while specialists run on the legacy orchestrator.
    const delegationRow = domains.find(item => item.domain === DELEGATION_DOMAIN);
    if (target === "legacy" && delegationRow !== undefined && delegationRow.epoch === null
        && (delegationRow.state === "unavailable" || (delegationRow.state === "blocked" && delegationRow.owner_kind === null))) {
      // An unreadable inventory: the rollback would be refused if delegation is Cordis-owned. A missing
      // or unseeded row is no dependent, so chat rolls back alone.
      setMoveError(`Delegation (${DELEGATION_DOMAIN}) state is unknown, so chat cannot roll back yet; refresh once the runtime inventory reads again.`);
      return;
    }
    const delegation = delegationRow?.epoch !== null ? delegationRow : undefined;
    const moveDelegation = delegation !== undefined && delegation.owner_kind !== target;
    const label = target === "legacy" ? "roll chat back to the legacy agent loop" : "cut chat over to the Cordis agent loop";
    const domainsText = moveDelegation ? (target === "legacy" ? `${DELEGATION_DOMAIN}, then ${CHAT_DOMAIN}` : `${CHAT_DOMAIN}, then ${DELEGATION_DOMAIN}`) : CHAT_DOMAIN;
    if (!window.confirm(`Confirm: ${label} (${domainsText}). In-flight turns must finish first.`)) return;
    setMoving(true); setMoveError(null);
    try {
      if (target === "legacy" && moveDelegation) {
        const refused = await moveDomain(DELEGATION_DOMAIN, "legacy", delegation.epoch as number);
        if (refused) { setMoveError(`Chat loop change refused: ${refused} (${DELEGATION_DOMAIN}); chat was not changed.`); return; }
      }
      const refused = await moveDomain(CHAT_DOMAIN, target, chat.owner_epoch);
      if (refused) {
        setMoveError(target === "legacy" && moveDelegation
          ? `Chat loop change refused: ${refused}. Delegation is already on legacy; refresh and roll chat back again.`
          : `Chat loop change refused: ${refused}`);
        return;
      }
      if (target === "cordis" && moveDelegation) {
        const delegationRefused = await moveDomain(DELEGATION_DOMAIN, "cordis", delegation.epoch as number);
        if (delegationRefused) setMoveError(`Chat is on Cordis, but delegation was refused: ${delegationRefused} (${DELEGATION_DOMAIN}); refresh and use "Cut delegation over to Cordis".`);
      }
    } catch { setMoveError("Chat loop change failed; refresh and try again."); }
    finally { setMoving(false); void refresh(); }
  }
  const pendingDelegation = chat?.owner_kind === "cordis"
    ? domains.find(item => item.domain === DELEGATION_DOMAIN && item.owner_kind === "legacy" && item.epoch !== null)
    : undefined;
  async function cutDelegationOver() {
    if (!pendingDelegation) return;
    if (!window.confirm(`Confirm: cut delegation over to Cordis (${DELEGATION_DOMAIN}). In-flight turns must finish first.`)) return;
    setMoving(true); setMoveError(null);
    try {
      const refused = await moveDomain(DELEGATION_DOMAIN, "cordis", pendingDelegation.epoch as number);
      if (refused) setMoveError(`Delegation change refused: ${refused} (${DELEGATION_DOMAIN})`);
    } catch { setMoveError("Delegation change failed; refresh and try again."); }
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
    {pendingDelegation && <button type="button" disabled={moving} onClick={() => void cutDelegationOver()}>Cut delegation over to Cordis</button>}
    {moveError && <p role="status" className="text-yellow-400">{moveError}</p>}
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
