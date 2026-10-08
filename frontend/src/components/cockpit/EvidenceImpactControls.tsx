import { useEffect, useRef, useState } from 'react';
import { apiFetch } from '../../lib/api';

interface ImpactRequest {
  source_id: string; cursor: string | null; expected_snapshot_digest: string;
  idempotency_key: string; acknowledge_safety_pause: true;
}
interface ImpactPage {
  source_id: string; snapshot_digest: string | null; cursor: string | null; next_cursor: string | null;
  tasks: { task_id: string; task_revision: number; status: string; stale: boolean; reason_code: string | null }[];
  applied_result: { paused_task_ids: string[]; retained_task_ids: string[] } | null;
}
const hash = /^[a-f0-9]{64}$/;
const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
function validRequest(value: unknown): value is ImpactRequest {
  if (!value || typeof value !== 'object') return false;
  const v = value as ImpactRequest;
  const keys = ['source_id', 'cursor', 'expected_snapshot_digest', 'idempotency_key', 'acknowledge_safety_pause'];
  return Object.keys(v).length === keys.length && Object.keys(v).every(k => keys.includes(k))
    && hash.test(v.source_id) && hash.test(v.expected_snapshot_digest) && uuid.test(v.idempotency_key)
    && (v.cursor === null || typeof v.cursor === 'string' && v.cursor.length <= 2048)
    && v.acknowledge_safety_pause === true;
}
function validPage(value: unknown): value is ImpactPage {
  if (!value || typeof value !== 'object') return false;
  const p = value as ImpactPage;
  return hash.test(p.source_id) && (p.snapshot_digest === null || hash.test(p.snapshot_digest))
    && [p.cursor, p.next_cursor].every(c => c === null || typeof c === 'string' && c.length <= 2048)
    && Array.isArray(p.tasks) && p.tasks.length <= 50 && p.tasks.every(t => typeof t.task_id === 'string'
      && t.task_id.length <= 128 && Number.isSafeInteger(t.task_revision) && t.task_revision > 0
      && typeof t.status === 'string' && t.status.length <= 64 && typeof t.stale === 'boolean'
      && (t.reason_code === null || typeof t.reason_code === 'string' && t.reason_code.length <= 128))
    && (p.applied_result === null || p.applied_result && [p.applied_result.paused_task_ids,
      p.applied_result.retained_task_ids].every(ids => Array.isArray(ids) && ids.length <= 50
        && ids.every(id => typeof id === 'string' && id.length <= 128)));
}

export function EvidenceImpactControls({ endpoint, taskId, taskRevision, ownerSessionId, sourceId, canEdit }: {
  endpoint: string; taskId: string; taskRevision: number; ownerSessionId?: string | null;
  sourceId: string; canEdit: boolean;
}) {
  const scope = `${ownerSessionId ?? ''}:${taskId}:${taskRevision}:${sourceId}`;
  const currentScope = useRef(scope); currentScope.current = scope;
  const storageKey = `seraph:evidence-impact:${ownerSessionId ?? ''}:${taskId}:${sourceId}`;
  const [page, setPage] = useState<{ scope: string; value: ImpactPage } | null>(null);
  const [pending, setPending] = useState<ImpactRequest | null>(null);
  const [ack, setAck] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const current = page?.scope === scope ? page.value : null;
  const enabled = canEdit && Boolean(ownerSessionId);

  async function inspect(cursor: string | null = current?.cursor ?? null, request = pending) {
    const captured = scope;
    const params = new URLSearchParams({ source_id: sourceId });
    if (cursor !== null) params.set('cursor', cursor);
    if (request) params.set('pending_request', JSON.stringify(request));
    const r = await apiFetch(`${endpoint}/affected?${params}`);
    if (!r.ok) throw new Error('Impact snapshot changed. Inspect current evidence; keep any retained exact request.');
    const value: unknown = await r.json();
    if (!validPage(value) || value.source_id !== sourceId || value.cursor !== cursor) throw new Error('Impact projection is invalid.');
    if (currentScope.current === captured) { setPage({ scope: captured, value }); setAck(null); }
    return value;
  }
  useEffect(() => {
    setPage(null); setPending(null); setAck(null); setError(null); setBusy(false);
    let request: ImpactRequest | null = null;
    try {
      const raw = sessionStorage.getItem(storageKey);
      if (raw !== null) {
        if (raw.length > 4096) throw new Error('Retained impact request exceeds its finite bound.');
        const parsed: unknown = JSON.parse(raw);
        if (!validRequest(parsed) || parsed.source_id !== sourceId) throw new Error('Retained impact request is corrupt.');
        request = parsed; setPending(parsed);
      }
    } catch (err) { setError((err as Error).message); return; }
    void inspect(request?.cursor ?? null, request).catch(err => {
      if (currentScope.current === scope) setError((err as Error).message);
    });
    // Reload reads only; it never retries a safety pause.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope, storageKey, endpoint]);
  async function action(run: () => Promise<void>) {
    const captured = scope; setBusy(true); setError(null);
    try { await run(); }
    catch (err) { if (currentScope.current === captured) setError((err as Error).message); }
    finally { if (currentScope.current === captured) setBusy(false); }
  }
  async function submit(request: ImpactRequest) {
    const r = await apiFetch(`${endpoint}/impact-evaluation`, { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(request) });
    if (!r.ok) throw new Error('Impact evaluation was rejected or its outcome is ambiguous. Keep the exact request for inspection.');
    await inspect(request.cursor, request);
  }
  async function accept() {
    if (!current?.snapshot_digest || ack !== `${scope}:${current.snapshot_digest}` || pending) return;
    const request: ImpactRequest = { source_id: sourceId, cursor: current.cursor,
      expected_snapshot_digest: current.snapshot_digest, idempotency_key: crypto.randomUUID(), acknowledge_safety_pause: true };
    const bytes = JSON.stringify(request);
    if (!validRequest(request) || bytes.length > 4096) throw new Error('Impact request exceeds its bound.');
    sessionStorage.setItem(storageKey, bytes);
    if (sessionStorage.getItem(storageKey) !== bytes) throw new Error('Exact impact request retention failed.');
    setPending(request); await submit(request);
  }
  return <section aria-label="Bounded evidence impact" className="mt-2 border-t border-white/10 pt-2">
    <p>Affected tasks: at most 50 per page. Inspection never changes or dispatches work.</p>
    <p>Evaluation only pauses stale pending work; running, completed, Review and other blockers retain their state.</p>
    {error && <p role="alert">{error}</p>}
    <button disabled={busy} onClick={() => void action(async () => { await inspect(pending?.cursor ?? current?.cursor ?? null); })}>Inspect affected tasks</button>
    {current?.tasks.map(t => <p key={t.task_id}><code>{t.task_id}</code> · {t.status} · {t.stale ? 'execution evidence stale' : 'selected source current'}</p>)}
    {pending ? <>
      <p>Exact safety-pause request retained; reload does not resend it.</p>
      <button disabled={!enabled || busy || Boolean(error)} onClick={() => void action(() => submit(pending))}>Retry exact safety pause</button>
      {current?.applied_result && <button disabled={busy} onClick={() => void action(async () => {
        const value = await inspect(pending.cursor, pending);
        if (!value.applied_result) throw new Error('Exact applied impact receipt is unavailable.');
        sessionStorage.removeItem(storageKey); setPending(null); setAck(null);
      })}>Dismiss applied safety-pause request</button>}
    </> : current && <>
      <label><input type="checkbox" checked={ack === `${scope}:${current.snapshot_digest}`}
        disabled={!enabled || !current.snapshot_digest} onChange={event => setAck(event.target.checked ? `${scope}:${current.snapshot_digest}` : null)} />
        I reviewed this exact page and authorize only bounded safety pauses, with no approval or dispatch.</label>
      <button disabled={!enabled || busy || Boolean(error) || !current.snapshot_digest || ack !== `${scope}:${current.snapshot_digest}`}
        onClick={() => void action(accept)}>Evaluate reviewed impact page</button>
      {current.next_cursor && <button disabled={busy} onClick={() => void action(async () => { await inspect(current.next_cursor, null); })}>Inspect next affected page</button>}
    </>}
  </section>;
}
