import { useEffect, useRef, useState } from 'react';
import { apiFetch } from '../../lib/api';

type BindingRequest = {
  expected_task_revision: number; expected_packet_revision: number; expected_packet_digest: string;
  operation: 'bind' | 'revoke'; preview_digest: string; idempotency_key: string;
  acknowledge_execution_use: true;
};
interface Preview {
  task_id: string; task_revision: number; packet_revision: number; packet_digest: string;
  preview_digest: string; executor_input_digest: string; operation: 'bind' | 'revoke';
  affected_slots: string[];
}
interface Inspection {
  task_id: string; task_revision: number; binding_state: 'unbound' | 'bound' | 'stale';
  binding_count: number; applied_result: { task_revision: number; binding_state: string } | null;
}
const hash = /^[a-f0-9]{64}$/;
const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
const requestKeys = ['expected_task_revision', 'expected_packet_revision', 'expected_packet_digest',
  'operation', 'preview_digest', 'idempotency_key', 'acknowledge_execution_use'];
function validRequest(value: unknown): value is BindingRequest {
  if (!value || typeof value !== 'object') return false;
  const v = value as BindingRequest;
  return Object.keys(v).length === requestKeys.length && Object.keys(v).every(key => requestKeys.includes(key))
    && Number.isSafeInteger(v.expected_task_revision) && v.expected_task_revision > 0
    && Number.isSafeInteger(v.expected_packet_revision) && v.expected_packet_revision > 0
    && hash.test(v.expected_packet_digest) && hash.test(v.preview_digest) && uuid.test(v.idempotency_key)
    && (v.operation === 'bind' || v.operation === 'revoke') && v.acknowledge_execution_use === true;
}

export function EvidenceExecutionControls({ endpoint, taskId, taskRevision, ownerSessionId, canEdit,
  packetRevision, packetDigest }: { endpoint: string; taskId: string; taskRevision: number;
  ownerSessionId?: string | null; canEdit: boolean; packetRevision: number; packetDigest: string | null }) {
  const [inspection, setInspection] = useState<Inspection | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [ack, setAck] = useState(false);
  const [pending, setPending] = useState<BindingRequest | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const scope = `${ownerSessionId ?? ''}:${taskId}:${taskRevision}`;
  const currentScope = useRef(scope); currentScope.current = scope;
  const storageKey = `seraph:evidence-execution:${ownerSessionId ?? ''}:${taskId}`;
  const boundInspection = inspection?.task_id === taskId ? inspection : null;
  const boundPreview = preview?.task_id === taskId ? preview : null;
  const enabled = canEdit && Boolean(ownerSessionId);

  async function call(method: string, suffix = '', body?: unknown) {
    const response = await apiFetch(`${endpoint}/execution-binding${suffix}`, { method,
      ...(body === undefined ? {} : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }) });
    if (!response.ok) throw new Error('Execution evidence changed or is unavailable. Inspect the exact retained request before retrying.');
    return response.json();
  }
  async function inspect(request = pending) {
    const expectedScope = currentScope.current;
    const value = await call('GET', request ? `?pending_request=${encodeURIComponent(JSON.stringify(request))}` : '') as Inspection;
    if (value.task_id !== taskId || !Number.isSafeInteger(value.task_revision)
      || !['unbound', 'bound', 'stale'].includes(value.binding_state)
      || !Number.isSafeInteger(value.binding_count) || value.binding_count < 0 || value.binding_count > 16) {
      throw new Error('Execution evidence projection is invalid.');
    }
    if (currentScope.current === expectedScope) setInspection(value);
    return value;
  }
  useEffect(() => {
    let live = true;
    setPreview(null); setAck(false); setInspection(null); setPending(null); setError(null); setBusy(false);
    try {
      const raw = sessionStorage.getItem(storageKey);
      if (raw !== null) {
        if (raw.length > 2048) throw new Error('Retained execution request exceeds its bound.');
        const request: unknown = JSON.parse(raw);
        if (!validRequest(request)) throw new Error('Retained execution request is corrupt. Mutation remains blocked.');
        setPending(request);
      }
    } catch (err) { setError((err as Error).message); return; }
    // Reload only inspects; it never retries the retained mutation.
    void inspect(null).catch(err => { if (live) setError((err as Error).message); });
    return () => { live = false; };
    // Scope changes invalidate every old acknowledgment and preview.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope, endpoint, storageKey]);
  useEffect(() => { setPreview(null); setAck(false); }, [packetRevision, packetDigest]);

  async function action(run: () => Promise<void>) {
    const captured = scope; setBusy(true); setError(null);
    try { await run(); }
    catch (err) { if (currentScope.current === captured) setError((err as Error).message); }
    finally { if (currentScope.current === captured) setBusy(false); }
  }
  async function prepare(operation: 'bind' | 'revoke') {
    const captured = scope;
    const response = await apiFetch(`${endpoint}/execution-preview`, { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({
        expected_task_revision: boundInspection?.task_revision ?? taskRevision,
        expected_packet_revision: packetRevision, expected_packet_digest: packetDigest, operation }) });
    if (!response.ok) throw new Error('Review current eligible evidence before preparing execution binding.');
    const value = await response.json() as Preview;
    if (value.task_id !== taskId || value.operation !== operation || !hash.test(value.preview_digest)
      || !hash.test(value.executor_input_digest) || value.packet_digest !== packetDigest
      || value.packet_revision !== packetRevision || !Number.isSafeInteger(value.task_revision)
      || !Array.isArray(value.affected_slots) || value.affected_slots.length > 4
      || value.affected_slots.some(slot => typeof slot !== 'string' || slot.length > 256)) throw new Error('Execution preview binding is invalid.');
    if (currentScope.current === captured) { setPreview(value); setAck(false); }
  }
  async function submit(request: BindingRequest) {
    const captured = scope;
    await call('POST', '', request);
    await inspect(request);
    if (currentScope.current === captured) { setPreview(null); setAck(false); }
    // Keep the exact request until explicit inspection confirms its receipt.
  }
  async function accept() {
    if (!boundPreview || !ack || pending) return;
    const request: BindingRequest = { expected_task_revision: boundPreview.task_revision,
      expected_packet_revision: boundPreview.packet_revision, expected_packet_digest: boundPreview.packet_digest,
      operation: boundPreview.operation, preview_digest: boundPreview.preview_digest,
      idempotency_key: crypto.randomUUID(), acknowledge_execution_use: true };
    const bytes = JSON.stringify(request);
    if (bytes.length > 2048 || !validRequest(request)) throw new Error('Execution request is invalid.');
    sessionStorage.setItem(storageKey, bytes);
    if (sessionStorage.getItem(storageKey) !== bytes) throw new Error('Exact request storage failed; mutation remains blocked.');
    setPending(request); await submit(request);
  }
  return <div className="mt-3 border-t border-white/10 pt-2" aria-label="Execution evidence binding">
    <p>Execution evidence: {boundInspection?.binding_state ?? 'inspection pending'}. This is separate from model context consent.</p>
    <p>Rebinding preserves the executor input, permissions, deadlines and attempts. It never approves or dispatches work.</p>
    {error && <p role="alert">{error}</p>}
    <button disabled={busy} onClick={() => void action(async () => { await inspect(); })}>Inspect execution binding</button>
    {pending ? <>
      <p>Exact execution request retained. Reload does not resend it.</p>
      <button disabled={!enabled || busy || Boolean(error)} onClick={() => void action(() => submit(pending))}>Retry exact execution request</button>
      {boundInspection?.applied_result && <button disabled={busy} onClick={() => void action(async () => {
        const current = await inspect(pending);
        if (!current.applied_result) throw new Error('Exact applied receipt is unavailable. Keep the request.');
        sessionStorage.removeItem(storageKey); setPending(null); setAck(false); setPreview(null);
      })}>Dismiss applied execution request</button>}
    </> : <>
      <button disabled={!enabled || busy || Boolean(error) || !packetDigest || packetRevision < 1}
        onClick={() => void action(() => prepare('bind'))}>Preview execution evidence replacement</button>
      <button disabled={!enabled || busy || Boolean(error) || boundInspection?.binding_state !== 'bound'}
        onClick={() => void action(() => prepare('revoke'))}>Preview execution binding revocation</button>
    </>}
    {boundPreview && <>
      <p>Exact executor input SHA-256: <code>{boundPreview.executor_input_digest}</code></p>
      <p>Affected slots: {boundPreview.affected_slots.join(', ') || 'this task only'}.</p>
      <label><input type="checkbox" checked={ack} onChange={event => setAck(event.target.checked)} />
        I reviewed this exact packet for execution use; it grants no model or external permission.</label>
      <button disabled={!enabled || busy || !ack || Boolean(pending)} onClick={() => void action(accept)}>Accept exact execution binding</button>
    </>}
  </div>;
}
