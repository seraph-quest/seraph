import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import type { GoalInfo, WorkBoardTask } from "../../types";

const CAPABILITY = "work.document-compare.v1";
type Descriptor = { size_bytes: number; sha256: string };
type Pair = { artifact_id: string; revision: number; pair_state: string; uploaded: string[]; goal_id: string; goal_revision: number; typed_input_digest: string | null; ingest_deadline: string; reason_code?: string };
type Pending = { request: { schema_version: 1; operation: "compare-line-totals-by-sku"; goal_id: string; goal_revision: number; idempotency_key: string; pdf: Descriptor; csv: Descriptor; no_learning: true }; pair: string | null; taskKey: string };
interface Props { ownerPrincipalId?: string | null; ownerSessionId?: string | null; task?: WorkBoardTask; goals?: GoalInfo[]; onClose?: () => void; onCreated?: (task: WorkBoardTask) => void | Promise<void> }

async function request(path: string, body?: unknown, method = "POST") {
  const response = await apiFetch(`${API_URL}/api/work-board${path}`, { method, ...(body === undefined ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }) });
  const value = await response.json();
  if (!response.ok) throw Error(value.detail?.code ?? "Document request failed; read back before retry.");
  return value;
}
async function descriptor(file: File, maximum: number): Promise<Descriptor> {
  if (file.size < 1 || file.size > maximum) throw Error("Selected file exceeds the document size limit.");
  const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
  return { size_bytes: file.size, sha256: [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, "0")).join("") };
}

export function DocumentComparisonPanel({ ownerPrincipalId, ownerSessionId, task, goals = [], onClose, onCreated }: Props) {
  const [pdf, setPdf] = useState<File | null>(null), [csv, setCsv] = useState<File | null>(null);
  const [goalId, setGoalId] = useState(""), [pending, setPending] = useState<Pending | null>(null), [pair, setPair] = useState<Pair | null>(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState<string | null>(null), [output, setOutput] = useState<string | null>(null);
  const generation = useRef(0);
  const key = ownerPrincipalId && ownerSessionId ? `seraph.document-pair.v1:${encodeURIComponent(ownerPrincipalId)}:${encodeURIComponent(ownerSessionId)}` : null;
  useEffect(() => {
    generation.current += 1; setPdf(null); setCsv(null); setOutput(null); setPair(null); setError(null); setPending(null);
    if (!key || task) return;
    try {
      const raw = sessionStorage.getItem(key);
      if (raw) {
        if (raw.length > 4096) throw Error();
        const value = JSON.parse(raw) as Pending;
        if (value.request?.operation !== "compare-line-totals-by-sku" || !/^[0-9a-f-]{36}$/.test(value.request.idempotency_key) || !value.request.no_learning || !/^[0-9a-f]{64}$/.test(value.request.pdf.sha256) || !/^[0-9a-f]{64}$/.test(value.request.csv.sha256)) throw Error();
        setPending(value); setGoalId(value.request.goal_id);
      }
    } catch { setError("Retained document request is unavailable. Keep its reservation for explicit cleanup."); }
  }, [key, task?.task_id]);
  function retain(value: Pending) {
    if (!key) throw Error("The current operator session is required.");
    const encoded = JSON.stringify(value); sessionStorage.setItem(key, encoded);
    if (sessionStorage.getItem(key) !== encoded) throw Error("Document request could not be retained.");
    setPending(value);
  }
  async function submit() {
    const version = generation.current; setBusy(true); setError(null);
    try {
      if (!pdf || !csv) throw Error("Select the PDF and CSV; retained requests require the exact original files.");
      const a = await descriptor(pdf, 2 * 1024 * 1024), b = await descriptor(csv, 1024 * 1024);
      const goal = goals.find(candidate => candidate.id === goalId);
      if (!goal || typeof goal.revision !== "number" || !Number.isSafeInteger(goal.revision) || goal.revision < 1) throw Error("Select a current bounded Goal with its revision.");
      let current: Pending = pending ?? { request: { schema_version: 1, operation: "compare-line-totals-by-sku", goal_id: goal.id, goal_revision: goal.revision, idempotency_key: crypto.randomUUID(), pdf: a, csv: b, no_learning: true }, pair: null, taskKey: crypto.randomUUID() };
      if (JSON.stringify(current.request.pdf) !== JSON.stringify(a) || JSON.stringify(current.request.csv) !== JSON.stringify(b)) throw Error("Reselect the exact original PDF and CSV; the reservation cannot change sources.");
      retain(current);
      let state = await request("/document-pairs", current.request) as Pair;
      if (version !== generation.current) return;
      current = { ...current, pair: state.artifact_id }; retain(current); setPair(state);
      for (const [slot, file] of [["pdf", pdf], ["csv", csv]] as const) {
        if (state.uploaded.includes(slot) || state.pair_state === "sealed") continue;
        const response = await apiFetch(`${API_URL}/api/work-board/document-pairs/${state.artifact_id}/sources/${slot}?expected_revision=${state.revision}`, { method: "PUT", headers: { "Content-Type": "application/octet-stream" }, body: file });
        const value = await response.json();
        if (!response.ok) throw Error(value.detail?.code ?? "Private source upload needs readback.");
        state = value as Pair; if (version !== generation.current) return; setPair(state);
      }
      if (state.pair_state !== "sealed") state = await request(`/document-pairs/${state.artifact_id}/complete`, { expected_revision: state.revision }) as Pair;
      if (!state.typed_input_digest || state.pair_state !== "sealed") throw Error("Both source readbacks are required before task creation.");
      const created = await request("/tasks", { title: "Compare selected private invoice", body: "Compare line totals by SKU in the selected immutable PDF and CSV.", capability_id: CAPABILITY, goal_id: current.request.goal_id, goal_revision: current.request.goal_revision, status: "todo", input_artifact_id: state.artifact_id, idempotency_key: current.taskKey, requires_review: false });
      if (created.task?.capability_id !== CAPABILITY || created.task.input_artifact_id !== state.artifact_id) throw Error("Exact task binding readback unavailable.");
      if (version !== generation.current) return;
      if (key) sessionStorage.removeItem(key); setPending(null); await onCreated?.(created.task as WorkBoardTask);
    } catch (failure) { if (version === generation.current) setError(failure instanceof Error ? failure.message : "Document comparison is blocked."); }
    finally { if (version === generation.current) setBusy(false); }
  }
  const contents = <section className="rounded border border-white/20 bg-slate-950 p-4 text-slate-100" aria-label="Private invoice comparison">
    <h3>Private invoice comparison</h3>
    <p className="text-xs">Compare USD line totals by SKU. PDF: 2 MiB and 10 pages; CSV: 1 MiB and 2,000 rows. Supported PDF text starts with INVOICE USD and SKU QTY UNIT_PRICE. CSV header: SKU,QTY,UNIT_PRICE. Sources remain private; no learning or model calls.</p>
    <p className="text-xs">Complex fonts, images, forms, actions and unsupported content are visibly blocked. Structural checks are not malware scanning.</p>
    {error && <p role="alert">{error}</p>}
    {!task && <>
      <label>Bounded Goal<select aria-label="Document comparison Goal" value={goalId} disabled={busy || Boolean(pending)} onChange={event => setGoalId(event.target.value)}><option value="">Select Goal</option>{goals.filter(goal => goal.status === "active").map(goal => <option key={goal.id} value={goal.id}>{goal.title}</option>)}</select></label>
      <label>Invoice PDF<input type="file" accept="application/pdf,.pdf" aria-label="Invoice PDF" disabled={busy} onChange={event => setPdf(event.target.files?.[0] ?? null)} /></label>
      <label>Comparison CSV<input type="file" accept="text/csv,.csv" aria-label="Comparison CSV" disabled={busy} onChange={event => setCsv(event.target.files?.[0] ?? null)} /></label>
      {pending && <p>Original request retained. Reselect the exact files to read back and resume; source bytes are not stored in browser storage.</p>}
      {pair && <p>{pair.pair_state} · upload window ends {pair.ingest_deadline} · sources {pair.uploaded.join(", ") || "pending"}</p>}
      <button type="button" disabled={busy || !pdf || !csv || !goalId || !key} onClick={() => void submit()}>{busy ? "Verifying private pair…" : pending ? "Read back and resume original pair" : "Reserve private pair and create comparison"}</button>
      <button type="button" disabled={busy} onClick={onClose}>Close</button>
    </>}
    {task && <>
      <p>Comparison {task.status} · recovery and cancellation use this original task and its bounded attempt.</p>
      <button type="button" disabled={busy || !["done", "review"].includes(task.status)} onClick={() => {
        setBusy(true); void request(`/tasks/${task.task_id}/document-output/report`, undefined, "GET").then(value => setOutput(value.text)).catch(failure => setError(String(failure))).finally(() => setBusy(false));
      }}>Read verified cited report</button>
      <button type="button" disabled={busy || !["done", "review"].includes(task.status)} onClick={() => {
        setBusy(true); void request(`/tasks/${task.task_id}/document-output/csv`, undefined, "GET").then(value => {
          const link = document.createElement("a"), url = URL.createObjectURL(new Blob([value.text], { type: "text/csv;charset=utf-8" })); link.href = url; link.download = "invoice-comparison.csv"; link.click(); URL.revokeObjectURL(url);
        }).catch(failure => setError(String(failure))).finally(() => setBusy(false));
      }}>Download verified derived CSV</button>
      {output !== null && <pre className="whitespace-pre-wrap break-words text-xs" aria-label="Verified cited document report">{output}</pre>}
    </>}
  </section>;
  return task ? contents : createPortal(<div className="fixed inset-0 overflow-auto bg-black/70 p-6" style={{ zIndex: 1100 }} role="dialog" aria-modal="true" aria-label="Create private invoice comparison"><div className="mx-auto max-w-2xl">{contents}</div></div>, document.body);
}
