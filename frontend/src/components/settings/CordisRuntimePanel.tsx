import { useEffect, useRef, useState } from "react";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import { normalizeCordisRuntime } from "../../lib/cordisRuntime";
import type { CordisRuntimeSnapshot } from "../../lib/cordisRuntime";

export function CordisRuntimePanel({ metadata, managed = false }: { metadata?: unknown; managed?: boolean }) {
  const [snapshot, setSnapshot] = useState<CordisRuntimeSnapshot | null>(null);
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
      if (version === generation.current) { setSnapshot(result); setError(null); }
    } catch { if (version === generation.current) setError("Cordis runtime metadata is unavailable. Last confirmed state remains visible; refresh after restoring the host. Artifact settings remain usable."); }
    finally { clearTimeout(timeout); if (version === generation.current) setBusy(false); }
  }
  useEffect(() => { if (!managed) void refresh(); return () => { ++generation.current; controller.current?.abort(); }; }, [managed]);
  useEffect(() => {
    if (!managed) return;
    const result = normalizeCordisRuntime(metadata);
    if (result) { setSnapshot(result); setError(null); }
    else setError("Cordis runtime metadata is unavailable. Last confirmed state remains visible; refresh after restoring the host. Artifact settings remain usable.");
  }, [managed, metadata]);
  return <section aria-label="Cordis runtime host" className="mb-3 rounded border border-retro-text/10 p-2 text-[10px]">
    <div className="flex items-center justify-between gap-2"><h3>Cordis runtime host</h3><button type="button" disabled={busy} onClick={() => void refresh()}>{busy ? "Checking host…" : "Refresh Cordis host"}</button></div>
    <p>This lifecycle host reports composition and cleanup. Agent-loop migration remains planned.</p>
    {error && <p role="status" className="text-yellow-400">{error}</p>}
    {!snapshot && <p role="status">{busy ? "Loading host state…" : "Host state unavailable"}</p>}
    {snapshot && <>
      <p role="status">{error ? "Host readiness unknown · stale metadata" : `${snapshot.state}${snapshot.reason ? ` · ${snapshot.reason}` : ""}`}</p>
      <p>Readiness: {error ? "unknown" : snapshot.readiness.state} · last verified: {snapshot.readiness.checked_at === null ? "unavailable" : new Date(snapshot.readiness.checked_at).toISOString()}</p>
      <p>Profile: {snapshot.profile_id ?? "none"} · Cordis {snapshot.cordis_version} · Node {snapshot.node_version ?? "unavailable"}</p>
      <p>Composition: {snapshot.composition_digest ?? "unavailable"}</p><p>Package: {snapshot.package_digest ?? "unavailable"}</p>
      <ul>{snapshot.plugins.map(p => <li key={p.id}>{p.id}: {error ? "unknown · stale metadata" : `${p.state}${p.reason ? ` · ${p.reason}` : ""}`}</li>)}</ul>
      <p>Cleanup: {snapshot.cleanup.state} · process {snapshot.cleanup.process_reaped ? "reaped" : "unconfirmed"} · resources {snapshot.cleanup.resources_remaining ?? "unknown"} · Cordis disposal {snapshot.cleanup.cordis_disposal}</p>
      {(error || snapshot.state !== "ready") && <p>Dependent work stays blocked until mandatory services are ready. Restore the reviewed host build and supported Node profile, then refresh. Unknown cleanup requires reconciliation before restart.</p>}
    </>}
  </section>;
}
