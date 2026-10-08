import { useEffect, useRef, useState } from "react";
import { connectionSyncRequest, privateSyncItem, syncPath, syncProjection } from "../../lib/connectionSync";
import type { SourceItemRef, SyncProjection, SyncProvider } from "../../lib/connectionSync";

interface Props {
  inspectionOnly?: boolean;
  provider: SyncProvider; ownerPrincipalId?: string | null; ownerSessionId?: string | null;
  connectionId: string; connectionRevision: number; connectionState: string;
  consent: { id: string; revision: number; goalId: string; goalRevision: number; state: string; expiresAt: string; metadataLimit: number; privateLimit: number };
  labelIds?: string[]; initialWindow?: { start: string; end: string };
}
export function ConnectionSyncPanel({ provider, ownerPrincipalId, ownerSessionId, connectionId, connectionRevision, connectionState, consent, labelIds = [], initialWindow, inspectionOnly = false }: Props) {
  const scope = JSON.stringify([provider, ownerPrincipalId, ownerSessionId, connectionId, connectionRevision, consent.id, consent.revision, consent.goalId, consent.goalRevision, labelIds]);
  const generation = useRef(0);
  const [projection, setProjection] = useState<SyncProjection | null>(null), [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false), [stale, setStale] = useState(false), [uncertain, setUncertain] = useState(false);
  const [start, setStart] = useState(""), [end, setEnd] = useState(""), [maxItems, setMaxItems] = useState("50");
  const [ack, setAck] = useState(false), [reset, setReset] = useState(false), [readAck, setReadAck] = useState(false), [recoverAck, setRecoverAck] = useState(false);
  const [selected, setSelected] = useState<string[]>([]), [bodyAck, setBodyAck] = useState(false);
  const [privateItem, setPrivateItem] = useState<ReturnType<typeof privateSyncItem> | null>(null);
  const [reviewRecovered, setReviewRecovered] = useState(false);
  const recovered = reviewRecovered ? projection?.selection : null;
  const metadataLimit = recovered?.max_items ?? consent.metadataLimit;
  const authorized = Boolean(ownerPrincipalId && ownerSessionId && connectionState === "active" && (recovered
    ? recovered.connection_ref.revision === connectionRevision && Date.parse(projection?.freshness.expires_at ?? "") > Date.now()
    : consent.state === "active"
    && consent.metadataLimit > 0 && Date.parse(consent.expiresAt) > Date.now() && (provider !== "gmail" || labelIds.length > 0 && labelIds.length <= 3)));
  const path = syncPath(provider, connectionId);
  async function refresh() {
    const version = generation.current; setBusy(true); setPrivateItem(null); setReadAck(false); setError(null);
    try {
      const value = syncProjection(await connectionSyncRequest(path), provider, connectionId);
      if (version === generation.current) { setProjection(value); setStale(false); }
    } catch (e) { if (version === generation.current) { setStale(true); setError((e as Error).message); } }
    finally { if (version === generation.current) setBusy(false); }
  }
  useEffect(() => {
    ++generation.current; setProjection(null); setPrivateItem(null); setAck(false); setReset(false); setReadAck(false); setRecoverAck(false); setSelected([]); setBodyAck(false); setUncertain(false); setStale(false); setBusy(false); setReviewRecovered(false);
    const now = Date.now();
    setStart(initialWindow?.start ?? new Date(now - 86400000).toISOString()); setEnd(initialWindow?.end ?? new Date(now).toISOString());
    setMaxItems(String(Math.min(consent.metadataLimit || 50, 50)));
    if (ownerPrincipalId && ownerSessionId) void refresh();
    return () => { ++generation.current; };
  }, [scope]);
  async function sync() {
    if (!authorized || busy || !ack || stale || !projection || projection.active_job_id || uncertain) return;
    const count = Number(maxItems), from = Date.parse(start), to = Date.parse(end);
    if (!Number.isSafeInteger(count) || count < 1 || count > Math.min(50, metadataLimit) || !Number.isFinite(from) || !Number.isFinite(to)
      || to <= from || to - from > 7 * 86400000 || !/(Z|[+-]\d\d:\d\d)$/.test(start) || !/(Z|[+-]\d\d:\d\d)$/.test(end)) {
      setError("Choose timezone-qualified ISO timestamps within seven days and a finite item limit within the current grant."); return;
    }
    if (selected.length && (!bodyAck || selected.length > Math.min(10, consent.privateLimit))) { setError("Selected private reads require separate acknowledgement and the original body limit."); return; }
    const version = generation.current; setBusy(true); setError(null); setPrivateItem(null); setAck(false); setReadAck(false); setUncertain(true);
    try {
      const value = syncProjection(await connectionSyncRequest(path, { input: { goal_ref: recovered?.goal_ref ?? { id: consent.goalId, revision: consent.goalRevision },
        connection_ref: recovered?.connection_ref ?? { id: connectionId, revision: connectionRevision }, source_scope: { provider, consents: recovered?.source_scope.consents ?? [{ id: consent.id, revision: consent.revision }],
          label_ids: recovered?.source_scope.label_ids ?? (provider === "gmail" ? labelIds : []), thread_keys: recovered?.source_scope.thread_keys ?? [], selected_private_items: selected,
          acknowledge_private_read: selected.length > 0 && bodyAck, reset_cursor: reset }, window: { start, end }, max_items: count }, request_uuid: crypto.randomUUID() }), provider, connectionId);
      if (version === generation.current) { setProjection(value); setUncertain(false); setSelected([]); setBodyAck(false); setReset(false); await refresh(); }
    } catch (e) { if (version === generation.current) { setError(`${(e as Error).message} The contact has no confirmed receipt. Refresh sync state; no request was automatically replayed.`); setStale(true); } }
    finally { if (version === generation.current) setBusy(false); }
  }
  async function read(ref: SourceItemRef) {
    if (!authorized || busy || stale || !readAck || Date.parse(ref.expires_at) <= Date.now()) return;
    const version = generation.current; setBusy(true); setError(null); setPrivateItem(null); setReadAck(false);
    try {
      const result = privateSyncItem(await connectionSyncRequest(`${path}/items/${encodeURIComponent(ref.opaque_id)}`, { acknowledge_private_read: true }), ref);
      if (version === generation.current) setPrivateItem(result);
    } catch (e) { if (version === generation.current) { setError((e as Error).message); setStale(true); } }
    finally { if (version === generation.current) setBusy(false); }
  }
  async function releasePhysicalSlot() {
    if (!projection?.active_job_id || !projection.active_job_revision || projection.cursor_revision === undefined || projection.recovery_action !== "release_physical_slot" || !recoverAck || busy || stale) return;
    const version = generation.current; setBusy(true); setError(null); setPrivateItem(null); setRecoverAck(false); setAck(false); setReadAck(false); setBodyAck(false); setSelected([]); setReset(false);
    try {
      const value = await connectionSyncRequest(`${path}/${encodeURIComponent(projection.active_job_id)}/reconcile`, {
        expected_job_revision: projection.active_job_revision, expected_cursor_revision: projection.cursor_revision, acknowledge_physical_slot_release: true });
      if (!value || typeof value !== "object" || !("provider_contacts" in value) || value.provider_contacts !== 0
        || !("job_id" in value) || value.job_id !== projection.active_job_id || !("physical_slot_released" in value) || value.physical_slot_released !== true
        || !("memory_status" in value) || value.memory_status !== "no_learning" || !("status" in value) || typeof value.status !== "string"
        || !["running", "unknown_external_effect", "cost_liability", "failed", "succeeded"].includes(value.status)
        || projection.unresolved_jobs.some(job => job.job_id === projection.active_job_id && job.status !== value.status)) throw Error("Physical cleanup receipt is unconfirmed; refresh the original sync.");
      if (version === generation.current) { setUncertain(false); await refresh(); }
    } catch (e) { if (version === generation.current) { setError((e as Error).message); setStale(true); } }
    finally { if (version === generation.current) setBusy(false); }
  }
  return <section aria-label={`${provider === "gmail" ? "Mail" : "Calendar"} connected context sync`} className="mt-3 rounded border border-white/15 p-3 text-xs">
    <h3>Maintain selected {provider === "gmail" ? "mail" : "calendar"} context</h3>
    <p>Up to 50 metadata items, three pages and seven days per bounded run. Private fields stay behind explicit local readback; no model egress or learning.</p>
    {!authorized && <p role="status">{inspectionOnly ? "Read-only sync inspection. Use calendar preparation with the original finite grant for new provider work." : "Blocked: create a current finite consent with metadata sync acknowledged; restore the original active connection and Goal revision."}</p>}
    {error && <p role="alert">{error}</p>}
    {error?.includes("source_sync_operator_continuity_required") && <p>Open “Operator ownership and recovery” above the cockpit and explicitly enroll the current authenticated scope. Then refresh this connection and review its grant before any new sync.</p>}
    <button type="button" disabled={busy || !ownerPrincipalId || !ownerSessionId} onClick={() => void refresh()}>Refresh sync state (no provider contact)</button>
    {projection && <>
      <p role="status">{stale ? "Stale, unconfirmed state · " : ""}{projection.state ?? projection.status} · cursor revision {projection.cursor_revision ?? "unavailable"}{projection.recovery_action ? ` · recovery: ${projection.recovery_action}` : ""}</p>
      <p>Physical reservation: {projection.reservation_state ?? "unconfirmed"} · active external effect: {projection.external_effect_state ?? "unconfirmed"}</p>
      {projection.unresolved_jobs.map(job => <article key={job.job_id} aria-label="Unresolved sync contact"><p>Sync {job.job_id} · revision {job.revision} · stored status: {job.status} · external effect: {job.external_effect_state}{job.failure_reason ? ` · reason: ${job.failure_reason}` : ""}</p><p>This contact remains unresolved even when physical capacity is available. Cleanup does not settle its outcome, adopt output or retry it.</p></article>)}
      <p>Coverage: {projection.coverage.partial || projection.coverage.more_available ? "partial; more available" : projection.coverage.pages_read ? "bounded selected window complete" : "no verified page"} · pages {projection.coverage.pages_read ?? 0} · returned {projection.coverage.returned ?? 0}</p>
      <p>Last complete page: {projection.freshness.last_complete_at ?? "unavailable"} · source expires: {projection.freshness.expires_at ?? consent.expiresAt}</p>
      {projection.last_error_code && <p>Last source error: {projection.last_error_code}. Restore the original token, scope or grant as indicated before creating new work.</p>}
      {inspectionOnly && !reviewRecovered && projection.selection && <button type="button" disabled={busy || stale || Boolean(projection.active_job_id)} onClick={() => {
        setReviewRecovered(true); setStart(projection.selection!.window.start); setEnd(projection.selection!.window.end); setMaxItems(String(projection.selection!.max_items));
        setAck(false); setBodyAck(false); setReadAck(false); setReset(false);
      }}>Review original synchronized scope</button>}
      {recovered && <p>Original Goal {recovered.goal_ref.id} revision {recovered.goal_ref.revision} · exact grants {recovered.source_scope.consents.map(ref => `${ref.id} revision ${ref.revision}`).join(", ")}. No previous acknowledgement was restored.</p>}
      {projection.cooldown && <p>Rate limit: bounded retry {projection.cooldown.retry_at ?? "not admitted"} · original deadline {projection.cooldown.original_deadline ?? "unavailable"}. No manual repeat while this run is active.</p>}
      {projection.active_job_id && <p>Existing sync {projection.active_job_id} · revision {projection.active_job_revision ?? "unavailable"}. Resolve this run before changing scope.</p>}
      {projection.recovery_action === "release_physical_slot" && <><p>Release physical capacity only after the original callback has closed. This does not settle OAuth or read outcomes, advance the cursor, adopt output, retry work or renew consent.</p><label><input type="checkbox" checked={recoverAck} onChange={e => setRecoverAck(e.target.checked)} />I acknowledge physical slot release only; the recorded contact outcome stays unchanged.</label><button type="button" disabled={busy || stale || !recoverAck || !projection.active_job_revision || projection.cursor_revision === undefined} onClick={() => void releasePhysicalSlot()}>Release physical sync slot</button></>}
      {uncertain && !stale && !projection.active_job_id && <button type="button" disabled={busy} onClick={() => { setUncertain(false); setAck(false); }}>Use confirmed state for a new bounded request</button>}
      {(!inspectionOnly || reviewRecovered) && <fieldset disabled={busy || !authorized || stale || Boolean(projection.active_job_id) || uncertain} className="mt-2 grid gap-2">
        <label>Sync window start (ISO with timezone)<input aria-label="Sync window start" value={start} onChange={e => { setStart(e.target.value); setAck(false); }} /></label>
        <label>Sync window end (ISO with timezone)<input aria-label="Sync window end" value={end} onChange={e => { setEnd(e.target.value); setAck(false); }} /></label>
        {projection.coverage.window && <button type="button" onClick={() => { setStart(projection.coverage.window!.start); setEnd(projection.coverage.window!.end); setAck(false); }}>Use original synchronized window</button>}
        <label>Sync metadata item limit<input aria-label="Sync metadata item limit" type="number" min={1} max={Math.min(50, metadataLimit || 50)} value={maxItems} onChange={e => { setMaxItems(e.target.value); setAck(false); }} /></label>
        <label><input type="checkbox" checked={reset} onChange={e => { setReset(e.target.checked); setAck(false); }} />Start a new cursor generation for this exact selected scope after physical capacity is released. Unresolved contact history remains recorded.</label>
        {projection.items.length > 0 && <><p>Optional private fields selection: at most {Math.min(10, consent.privateLimit)} existing opaque items.</p>{projection.items.map(ref => <label key={ref.opaque_id}><input type="checkbox" checked={selected.includes(ref.opaque_id)} disabled={!selected.includes(ref.opaque_id) && selected.length >= Math.min(10, consent.privateLimit)} onChange={e => { setSelected(e.target.checked ? [...selected, ref.opaque_id] : selected.filter(id => id !== ref.opaque_id)); setAck(false); setBodyAck(false); }} />Select private item {ref.opaque_id}</label>)}<label><input type="checkbox" checked={bodyAck} onChange={e => setBodyAck(e.target.checked)} />I acknowledge provider private-field reads only for these selected items within the original consent.</label></>}
        <label><input type="checkbox" checked={ack} onChange={e => setAck(e.target.checked)} />I acknowledge this exact window, selected scope and bounded provider metadata contact.</label>
        <button type="button" disabled={!ack || selected.length > 0 && !bodyAck} onClick={() => void sync()}>Sync selected context</button>
      </fieldset>}
      {(!inspectionOnly || reviewRecovered) && projection.items.length > 0 && <div className="mt-2"><label><input type="checkbox" checked={readAck} onChange={e => setReadAck(e.target.checked)} />I acknowledge reading one exact synchronized private item locally.</label>{projection.items.map(ref => <article key={ref.opaque_id} className="mt-2 border p-2"><p>Source {ref.opaque_id} · revision {ref.revision} · digest {ref.content_digest} · expires {ref.expires_at}</p><button type="button" disabled={busy || !authorized || stale || !readAck || Date.parse(ref.expires_at) <= Date.now()} onClick={() => void read(ref)}>Read private item {ref.opaque_id}</button></article>)}</div>}
      {privateItem && <article aria-label="Private synchronized source" className="mt-2 whitespace-pre-wrap break-words"><p>Citation: {privateItem.ref.provider}:{privateItem.ref.opaque_id} · {privateItem.ref.revision}</p><pre>{JSON.stringify(privateItem.content, null, 2)}</pre><p>Literal private readback · no_learning. A body appears only if separately selected and consented in the sync.</p></article>}
    </>}
  </section>;
}
