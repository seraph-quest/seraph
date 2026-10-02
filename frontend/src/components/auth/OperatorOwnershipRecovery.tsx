import { useState } from "react";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import type { OperatorSession } from "./OperatorAuthGate";

type Selection = { kind: "goal" | "task" | "artifact" | "output_artifact" | "memory" | "routine"; record_id: string };
type InventoryRecord = Selection & { label: string; source_session_id: string; state: string };
type Journal = { journal_id: string; state: string; fresh_work: unknown; selections: Selection[] };
type Inventory = { records: InventoryRecord[]; journals: Journal[]; truncated: boolean };

async function ownershipRequest(path: string, body?: unknown): Promise<Record<string, unknown>> {
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      controller.abort();
      reject(new Error("Ownership recovery timed out. Keep the last confirmed selection; retry and review its journal."));
    }, 15_000);
  });
  let response: Response;
  let payload: Record<string, any>;
  try {
    const read = async () => {
      const result = await apiFetch(`${API_URL}/api/auth/ownership/${path}`, {
        signal: controller.signal,
        ...(body !== undefined ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {}),
      });
      return { result, payload: await result.json().catch(() => ({})) };
    };
    const received = await Promise.race([read(), deadline]);
    response = received.result;
    payload = received.payload;
  } finally { clearTimeout(timer); }
  if (!response.ok) {
    const code = payload?.detail?.code;
    const message = code === "ownership_proof_required" ? "This login has no private ownership proof. Earlier records remain blocked."
      : code === "historical_effect_reconciliation_required" ? "Prior effects or costs must be reconciled. Recovery cannot replay this work."
      : code === "legacy_ownership_unproved" ? "This legacy owner chain is ambiguous and cannot be claimed from a password."
      : code === "recovery_preview_stale" ? "The selected records changed. Preview them again before confirming."
      : code === "login_rate_limited" ? "Too many ownership attempts. Wait a minute and retry."
      : "Ownership recovery could not be completed. Keep the last confirmed selection and retry.";
    throw new Error(message);
  }
  return payload;
}

function downloadCode(code: string) {
  const blob = new Blob([`Seraph one-time ownership recovery code\n${code}\n\nUse with your operator password. Keep private.\n`], { type: "text/plain" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "seraph-ownership-recovery.txt";
  anchor.click();
  URL.revokeObjectURL(url);
}

export function OperatorOwnershipRecovery({ session, onSessionChanged }: {
  session: OperatorSession;
  onSessionChanged: () => Promise<boolean>;
}) {
  const [open, setOpen] = useState(false);
  const [inventory, setInventory] = useState<Inventory | null>(null);
  const [selected, setSelected] = useState<Selection[]>([]);
  const [previewDigest, setPreviewDigest] = useState<string | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [requestKey, setRequestKey] = useState(() => crypto.randomUUID());
  const [code, setCode] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [revokeConfirmed, setRevokeConfirmed] = useState(false);
  const [outputArtifactId, setOutputArtifactId] = useState("");

  async function act(action: () => Promise<void>) {
    if (busy) return;
    setBusy(true);
    setMessage(null);
    try { await action(); } catch (cause) { setMessage(cause instanceof Error ? cause.message : "Recovery unavailable."); }
    finally { setBusy(false); }
  }

  async function loadInventory() {
    const payload = await ownershipRequest("recovery");
    setInventory(payload as unknown as Inventory);
  }

  return <div className="border-b border-white/10 px-4 py-2 text-xs">
    <button type="button" onClick={() => setOpen(!open)} aria-expanded={open}>Operator ownership and recovery</button>
    {open && <section aria-label="Operator ownership and recovery" className="mt-3 max-w-3xl space-y-3">
      <p>Private ownership proof keeps enrolled records discoverable after a new login. Recovery restores selected reads; previous approvals, source consents and queued work stay blocked.</p>
      {!session.operator_identity_id ? <>
        <p>Earlier unproved scopes remain private. Enroll this authenticated scope before logging out to preserve its future recovery.</p>
        <button type="button" disabled={busy} onClick={() => void act(async () => {
          const payload = await ownershipRequest("enroll", {});
          setCode(String(payload.recovery_code));
          await onSessionChanged();
          setMessage("This scope is enrolled. Save the one-time code for recovery on another device.");
        })}>Enroll this authenticated scope</button>
      </> : <>
        <button type="button" disabled={busy} onClick={() => void act(loadInventory)}>Review privately recoverable records</button>
        <button type="button" disabled={busy} className="ml-3" onClick={() => void act(async () => {
          const payload = await ownershipRequest("recovery-code", {});
          setCode(String(payload.recovery_code));
          setMessage("Previous unused recovery codes were revoked. Save this replacement once.");
        })}>Replace one-time recovery code</button>
        {inventory && <>
          {inventory.records.length === 0 && <p>No proved earlier records are available. Missing legacy ownership proof remains blocked.</p>}
          {inventory.truncated && <p>Inventory is bounded. Narrow selections to at most 50 records.</p>}
          <ul>{inventory.records.map((record) => <li key={`${record.kind}:${record.record_id}`}>
            <label><input type="checkbox" checked={selected.some(item => item.kind === record.kind && item.record_id === record.record_id)} disabled={busy} onChange={(event) => {
              setSelected(current => event.target.checked ? [...current, { kind: record.kind, record_id: record.record_id }] : current.filter(item => item.kind !== record.kind || item.record_id !== record.record_id));
              setPreviewDigest(null); setConfirmed(false); setRequestKey(crypto.randomUUID());
            }} /> {record.kind}: {record.label} · {record.state} · original {record.record_id}</label>
          </li>)}</ul>
          <label>Exact verified output artifact ID from a task receipt<input autoComplete="off" value={outputArtifactId} onChange={(event) => setOutputArtifactId(event.target.value)} /></label>
          <button type="button" disabled={busy || !/^art_[A-Za-z0-9_.:-]+$/.test(outputArtifactId)} onClick={() => {
            setSelected(current => [...current.filter(item => item.kind !== "output_artifact" || item.record_id !== outputArtifactId), { kind: "output_artifact", record_id: outputArtifactId }]);
            setPreviewDigest(null); setConfirmed(false); setRequestKey(crypto.randomUUID()); setOutputArtifactId("");
          }}>Select exact output artifact for private proof review</button>
          {selected.filter(item => item.kind === "output_artifact").map(item => <p key={item.record_id}>Output selection: {item.record_id}</p>)}
          <button type="button" disabled={busy || selected.length === 0 || selected.length > 50} onClick={() => void act(async () => {
            const payload = await ownershipRequest("recovery/preview", { selections: selected });
            setPreviewDigest(String(payload.preview_digest));
            setMessage(`Reviewed ${selected.length} selected records. Confirm read-only recovery below.`);
          })}>Preview selected recovery</button>
          {previewDigest && <div>
            <label><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} /> Confirm selected reads only; execution needs fresh current-scope review.</label>
            <button type="button" disabled={busy || !confirmed} className="ml-3" onClick={() => void act(async () => {
              await ownershipRequest("recovery/confirm", { selections: selected, preview_digest: previewDigest, idempotency_key: requestKey, acknowledge_read_only: true });
              await loadInventory();
              setPreviewDigest(null); setConfirmed(false);
              setMessage("Recovery is confirmed. Reload the cockpit to discover the original records in Home, Work and Memory.");
            })}>Confirm selected recovery</button>
          </div>}
          {inventory.journals.map(journal => <div key={journal.journal_id} className="border-t border-white/10 pt-2">
            <p>Recovery {journal.journal_id}: {journal.state} · {journal.selections.length} selected records</p>
            {journal.state === "confirmed" && !journal.fresh_work && <>
              <button type="button" disabled={busy} onClick={() => void act(async () => {
                await ownershipRequest(`recovery/${journal.journal_id}/rollback`, {}); await loadInventory();
                setMessage("Recovery reads revoked. Historical records and receipts remain stored.");
              })}>Roll back selected reads</button>
              <button type="button" disabled={busy} className="ml-3" onClick={() => void act(async () => {
                await ownershipRequest(`recovery/${journal.journal_id}/fresh-work`, {}); await loadInventory();
                setMessage("Fresh triage intent created with historical citations. Set current inputs, budget and permissions before execution.");
              })}>Create fresh reviewed intent from selected goals/tasks</button>
            </>}
            {Boolean(journal.fresh_work) && <p>Fresh intent is separate from this historical recovery. Review it in Work.</p>}
          </div>)}
        </>}
        <div><button type="button" disabled={busy} onClick={() => void act(async () => {
          await ownershipRequest("forget-device", {});
          setMessage("This browser continuity proof was revoked. Current login remains finite; use a saved code with your password for future recovery.");
        })}>Forget this device proof</button></div>
        <div><label><input type="checkbox" checked={revokeConfirmed} onChange={(event) => setRevokeConfirmed(event.target.checked)} /> Revoke this identity’s credentials and all its current sessions</label>
          <button type="button" disabled={busy || !revokeConfirmed} className="ml-3" onClick={() => void act(async () => {
            await ownershipRequest("revoke", {}); window.location.reload();
          })}>Revoke identity and sign out</button></div>
      </>}
      {code && <div role="status"><p>One-time recovery code. Save privately; it is not retained in browser storage.</p><button type="button" onClick={() => { downloadCode(code); setCode(null); }}>Download recovery code once</button><button type="button" className="ml-3" onClick={() => setCode(null)}>Clear pending recovery code</button></div>}
      <button type="button" onClick={() => window.location.reload()}>Reload cockpit with confirmed reads</button>
      {message && <p role="status" aria-live="polite">{message}</p>}
    </section>}
  </div>;
}
