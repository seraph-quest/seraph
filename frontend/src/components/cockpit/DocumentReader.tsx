import { useEffect, useRef, useState } from "react";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import {
  citationLeaves,
  parseDocumentPreparationTask,
  parseDocumentPreparationView,
  serializeDocumentPreparation,
  type DocumentPreparationTask,
  type DocumentPreparationView,
} from "../../lib/documentPreparation";
import type { GoalInfo } from "../../types";

type Format = "pdf" | "docx" | "xlsx" | "csv";
interface Source { artifact_id: string; artifact_ref: string; revision: number; state: string; format: Format;
  source_digest: string; goal_id: string; goal_revision: number; reason_code: string | null; cleanup: string; writer_kind: "parser" | "upload" | null; no_learning: true; provider_contacts: 0 }
interface Evidence { sections: { source_ref: string; text: string; table_cells: { source_ref: string; text: string; formula: string | null; cached_value: string | null }[] }[];
  warnings: string[]; source_digest: string; no_learning: true }
const record = (v: unknown): v is Record<string, unknown> => Boolean(v && typeof v === "object" && !Array.isArray(v));
async function request(path: string, method = "GET", body?: BodyInit, headers?: HeadersInit): Promise<unknown> {
  const response = await apiFetch(`${API_URL}/api/documents${path}`, { method, body, headers });
  if (!response.ok) {
    let code = "";
    try { const result = await response.json(); if (record(result) && record(result.detail) && typeof result.detail.code === "string" && /^[a-z_]{1,100}$/.test(result.detail.code)) code = ` ${result.detail.code}.`; } catch { /* The HTTP status remains useful through a partial error payload. */ }
    throw Error(`Document operation blocked (${response.status}).${code} Inspect the retained source and cleanup state before retrying.`);
  }
  return response.json();
}
function sourceRead(value: unknown): Source {
  if (!record(value) || typeof value.artifact_id !== "string" || typeof value.artifact_ref !== "string" || !/^document-source:[0-9a-f-]{36}$/.test(value.artifact_ref)
    || !Number.isSafeInteger(value.revision) || typeof value.state !== "string" || !["pdf", "docx", "xlsx", "csv"].includes(String(value.format))
    || typeof value.source_digest !== "string" || !/^[a-f0-9]{64}$/.test(value.source_digest) || typeof value.goal_id !== "string"
    || !Number.isSafeInteger(value.goal_revision) || value.no_learning !== true || value.provider_contacts !== 0 || typeof value.cleanup !== "string"
    || ![null, "parser", "upload"].includes(value.writer_kind as null | string)) throw Error("Private source readback is invalid. Inspect the original receipt before continuing.");
  return value as unknown as Source;
}
export function DocumentReader({ goals, ownerPrincipalId, ownerSessionId, onOpenTask }: { goals: GoalInfo[]; ownerPrincipalId?: string | null; ownerSessionId?: string | null; onOpenTask?: (taskId: string) => void }) {
  const [file, setFile] = useState<File | null>(null), [goalId, setGoalId] = useState("");
  const [source, setSource] = useState<Source | null>(null), [evidence, setEvidence] = useState<Evidence | null>(null);
  const [error, setError] = useState<string | null>(null), [busy, setBusy] = useState(false), [ack, setAck] = useState(false);
  const [selectedRefs, setSelectedRefs] = useState<string[]>([]), [prepareAck, setPrepareAck] = useState(false);
  const [preparationTask, setPreparationTask] = useState<DocumentPreparationTask | null>(null);
  const [preparationRefs, setPreparationRefs] = useState<string[]>([]), [preparationView, setPreparationView] = useState<DocumentPreparationView | null>(null);
  const [preparationPending, setPreparationPending] = useState(false);
  const [pages, setPages] = useState(""), [sheets, setSheets] = useState("");
  const [retained, setRetained] = useState<Source[]>([]), [nextOffset, setNextOffset] = useState<number | null>(null);
  const [uploadReadiness, setUploadReadiness] = useState<"ready" | "blocked" | "unknown">("unknown");
  const pending = useRef<{ request: string; file: File; digest: string; goal: GoalInfo } | null>(null);
  const preparationRequest = useRef<{ body: string; idempotency_key: string } | null>(null), generation = useRef(0);
  const eligible = goals.filter(g => g.status === "active" && g.revision && g.owner_session_id === ownerSessionId && g.ownership_access !== "recovered_read_only");
  const goal = eligible.find(g => g.id === goalId), owned = Boolean(ownerPrincipalId && ownerSessionId);
  useEffect(() => { ++generation.current; setFile(null); setGoalId(""); setSource(null); setEvidence(null); setSelectedRefs([]); setPrepareAck(false); setPreparationTask(null); setPreparationRefs([]); setPreparationView(null); setPreparationPending(false); setRetained([]); setNextOffset(null); setUploadReadiness("unknown"); setError(null); setBusy(false); setAck(false); pending.current = null; preparationRequest.current = null; return () => { ++generation.current; }; }, [ownerPrincipalId, ownerSessionId]);
  const body = (value: unknown) => JSON.stringify(value);
  function resetPreparationTask() {
    preparationRequest.current = null; setPreparationPending(false); setPreparationTask(null); setPreparationRefs([]); setPreparationView(null);
  }
  function clearPreparation() {
    setSelectedRefs([]); setPrepareAck(false); resetPreparationTask();
  }
  async function selectAndUpload() {
    if (!owned || busy || (!pending.current && (!file || !goal?.revision || !ack))) return;
    const version = generation.current; setBusy(true); setError(null); setEvidence(null); clearPreparation();
    try {
      if (!pending.current) {
        const selected = file!, selectedGoal = goal!;
        const format = selected.name.split(".").pop()?.toLowerCase() as Format;
        const limit = format === "docx" ? 10 * 1024 * 1024 : 16 * 1024 * 1024;
        if (!["pdf", "docx", "xlsx", "csv"].includes(format) || !selected.size || selected.size > limit) throw Error("Choose PDF, DOCX, XLSX or CSV within the finite source size limit (DOCX 10 MiB; other formats 16 MiB).");
        const buffer = await selected.arrayBuffer();
        const digest = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", buffer)), b => b.toString(16).padStart(2, "0")).join("");
        if (version !== generation.current) return;
        pending.current = { file: selected, digest, goal: selectedGoal, request: body({ format, source: { size_bytes: selected.size, sha256: digest }, goal_id: selectedGoal.id, goal_revision: selectedGoal.revision, idempotency_key: crypto.randomUUID(), no_learning: true }) };
      }
      const exact = pending.current;
      const receipt = sourceRead(await request("/sources", "POST", exact.request, { "Content-Type": "application/json" }));
      if (receipt.goal_id !== exact.goal.id || receipt.goal_revision !== exact.goal.revision || receipt.source_digest !== exact.digest) throw Error("Reserved source does not match this exact file and Goal.");
      if (version !== generation.current) return; setSource(receipt);
      // Upload never auto retries. A lost receipt requires GET inspection.
      const uploaded = sourceRead(await request(`/sources/${encodeURIComponent(receipt.artifact_id)}/content?expected_revision=${receipt.revision}`, "PUT", exact.file, { "Content-Type": "application/octet-stream" }));
      if (version !== generation.current) return; setSource(uploaded);
      const sealed = sourceRead(await request(`/sources/${encodeURIComponent(uploaded.artifact_id)}/seal?expected_revision=${uploaded.revision}`, "POST"));
      if (version !== generation.current) return; setSource(sealed); pending.current = null; setFile(null);
    } catch (e) { if (version === generation.current) setError((e as Error).message); }
    finally { if (version === generation.current) setBusy(false); }
  }
  async function inspect() {
    if (!source || busy) return;
    const version = generation.current; setBusy(true); setError(null); setEvidence(null); clearPreparation();
    try { const result = sourceRead(await request(`/sources/${encodeURIComponent(source.artifact_id)}`)); if (result.artifact_id !== source.artifact_id || result.source_digest !== source.source_digest) throw Error("Source identity changed."); if (version === generation.current) setSource(result); }
    catch (e) { if (version === generation.current) setError((e as Error).message); }
    finally { if (version === generation.current) setBusy(false); }
  }
  async function discover(offset = 0) {
    if (!owned || busy) return;
    const version = generation.current; setBusy(true); setError(null);
    try {
      const result = await request(`/sources?limit=50&offset=${offset}`);
      if (!record(result) || result.no_learning !== true || !Array.isArray(result.sources) || result.sources.length > 50
        || !(result.next_offset === null || Number.isSafeInteger(result.next_offset))) throw Error("Retained source discovery is unavailable.");
      const values = result.sources.map(sourceRead);
      if (version === generation.current) { setRetained(offset === 0 ? values : previous => [...previous, ...values]); setNextOffset(result.next_offset as number | null); setUploadReadiness(result.upload_readiness === "ready" || result.upload_readiness === "blocked" ? result.upload_readiness : "unknown"); }
    } catch (e) { if (version === generation.current) setError((e as Error).message); }
    finally { if (version === generation.current) setBusy(false); }
  }
  async function reconcile() {
    if (!source || busy) return;
    const version = generation.current; setBusy(true); setError(null); setEvidence(null); clearPreparation();
    try { const action = source.writer_kind === "upload" ? "reconcile-upload" : "reconcile"; const result = sourceRead(await request(`/sources/${encodeURIComponent(source.artifact_id)}/${action}?expected_revision=${source.revision}`, "POST")); if (version === generation.current) setSource(result); }
    catch (e) { if (version === generation.current) setError((e as Error).message); }
    finally { if (version === generation.current) setBusy(false); }
  }
  async function read() {
    if (!source || busy || source.state !== "sealed" || source.cleanup === "unknown_writer_retained") return;
    const version = generation.current; setBusy(true); setError(null); setEvidence(null); clearPreparation();
    try {
      const selectedPages = pages.trim() ? pages.split(",").map(p => Number(p.trim())) : [];
      const selectedSheets = sheets.trim() ? sheets.split("\n").map(s => s.trim()).filter(Boolean) : [];
      if (selectedPages.some(p => !Number.isInteger(p) || p < 1 || p > 100) || selectedPages.length > 100 || selectedSheets.length > 16) throw Error("Select at most 100 physical PDF pages (1–100) or 16 literal XLSX sheet names.");
      const result = await request("/read", "POST", body({ artifact_ref: source.artifact_ref, format: source.format,
        selection: { pages: source.format === "pdf" ? selectedPages : [], sheets: source.format === "xlsx" ? selectedSheets : [] }, page_sheet_limits: { max_pages: 100, max_sheets: 16, max_cells: 100000 } }), { "Content-Type": "application/json" });
      if (!record(result) || result.provider_contacts !== 0) throw Error("Extraction readback is unconfirmed.");
      if (result.status === "blocked" && result.no_learning === true) throw Error(`Document extraction blocked: ${String(result.reason)}. Inspect the source; unsupported, encrypted or malformed files need a supported replacement. Cleanup: ${String(result.cleanup)}.`);
      if (result.status !== "succeeded" || result.cleanup !== "wait_reaped" || !record(result.evidence) || result.evidence.source_digest !== source.source_digest || result.evidence.no_learning !== true
        || !Array.isArray(result.evidence.sections) || !Array.isArray(result.evidence.warnings)
        || !result.evidence.warnings.every(w => typeof w === "string") || result.evidence.sections.some(s => !record(s) || typeof s.source_ref !== "string" || typeof s.text !== "string" || !Array.isArray(s.table_cells)
          || s.table_cells.some(c => !record(c) || typeof c.source_ref !== "string" || typeof c.text !== "string" || !(c.formula === null || typeof c.formula === "string") || !(c.cached_value === null || typeof c.cached_value === "string")))) throw Error("Cited document evidence does not match the sealed source.");
      const parsedEvidence = result.evidence as unknown as Evidence;
      citationLeaves(parsedEvidence);
      if (version === generation.current) setEvidence(parsedEvidence);
    } catch (e) { if (version === generation.current) setError((e as Error).message); }
    finally { if (version === generation.current) setBusy(false); }
  }
  async function prepare() {
    if (!source || !evidence || busy || source.state !== "sealed" || source.cleanup === "unknown_writer_retained" || !prepareAck || !selectedRefs.length) return;
    const version = generation.current; setBusy(true); setError(null); setPreparationView(null);
    try {
      const current = preparationRequest.current ?? (() => {
        const idempotency_key = crypto.randomUUID();
        const payload = serializeDocumentPreparation({ artifact_ref: source.artifact_ref, expected_source_revision: source.revision, citation_refs: selectedRefs, acknowledge_local_use: true, idempotency_key });
        const requestBody = { body: payload, idempotency_key };
        preparationRequest.current = requestBody; setPreparationPending(true);
        return requestBody;
      })();
      const task = parseDocumentPreparationTask(await request("/preparations", "POST", current.body, { "Content-Type": "application/json" }));
      if (version === generation.current) { preparationRequest.current = null; setPreparationPending(false); setPreparationTask(task); setPreparationRefs([...selectedRefs]); }
    } catch (e) { if (version === generation.current) setError((e as Error).message); }
    finally { if (version === generation.current) setBusy(false); }
  }
  async function refreshPreparation() {
    if (!preparationTask || busy) return;
    const version = generation.current; setBusy(true); setError(null);
    try {
      const result = await request(`/preparations/${encodeURIComponent(preparationTask.task_id)}`);
      const view = parseDocumentPreparationView(result, preparationRefs);
      if (version === generation.current) { setPreparationTask(previous => previous ? { ...previous, status: view.status } : previous); setPreparationView(view.status === "succeeded" ? view : null); }
    } catch (e) { if (version === generation.current) setError((e as Error).message); }
    finally { if (version === generation.current) setBusy(false); }
  }
  async function remove() {
    if (!source || busy) return;
    const version = generation.current; setBusy(true); setError(null); setEvidence(null); clearPreparation();
    try { const result = sourceRead(await request(`/sources/${encodeURIComponent(source.artifact_id)}?expected_revision=${source.revision}`, "DELETE")); if (version === generation.current) { setSource(result); if (result.state === "deleted") pending.current = null; } }
    catch (e) { if (version === generation.current) setError((e as Error).message); }
    finally { if (version === generation.current) setBusy(false); }
  }
  return <details className="mb-3 rounded border border-white/10 p-2 text-xs"><summary>Read a local document</summary><section aria-label="Local document reader">
    <p>Select a private file for bounded CPU extraction into cited evidence. File content stays local; no provider contact and no learning.</p>
    <button type="button" disabled={busy || !owned} onClick={() => void discover()}>Refresh retained document sources</button>
    {uploadReadiness === "blocked" && <p role="status">New uploads are blocked until this host proves private writer exclusion. Restart the managed document service and refresh. Retained sources remain inspectable.</p>}
    {retained.length > 0 && <ul aria-label="Retained private document sources">{retained.map(item => <li key={item.artifact_id}><button type="button" disabled={busy} onClick={() => { setSource(item); setEvidence(null); clearPreparation(); setError(null); setFile(null); setAck(false); setPages(""); setSheets(""); pending.current = null; }}>{item.format} · {item.state} · {item.artifact_ref} · Goal {item.goal_id}</button></li>)}</ul>}
    {nextOffset !== null && <button type="button" disabled={busy} onClick={() => void discover(nextOffset)}>Load more retained document sources</button>}
    {error && <p role="alert" className="text-amber-200">{error}</p>}
    <fieldset disabled={busy || !owned || Boolean(source && source.state !== "deleted") || Boolean(pending.current)}>
      <label>Document Goal<select aria-label="Document Goal" value={goalId} onChange={e => { setGoalId(e.target.value); setAck(false); }}><option value="">Choose current Goal</option>{eligible.map(g => <option key={g.id} value={g.id}>{g.title} · revision {g.revision}</option>)}</select></label>
      <label>Selected document<input aria-label="Selected document" type="file" accept=".pdf,.docx,.xlsx,.csv" onChange={e => { setFile(e.target.files?.[0] ?? null); setAck(false); }} /></label>
      <label><input type="checkbox" checked={ack} onChange={e => setAck(e.target.checked)} />Store this exact selected file privately under this Goal for local document readback.</label>
    </fieldset>
    {(!source || source.state === "deleted") && <button type="button" disabled={busy || !owned || (!pending.current && (!file || !goal || !ack))} onClick={() => void selectAndUpload()}>{pending.current ? "Reconcile exact source reservation" : "Store selected document"}</button>}
    {source && <>
      <p role="status">Source {source.state} · {source.reason_code ?? "no active block"} · cleanup {source.cleanup}</p>
      <p className="font-mono break-all">{source.artifact_ref} · revision {source.revision} · SHA-256 {source.source_digest}</p>
      <button type="button" disabled={busy} onClick={() => void inspect()}>Inspect document source</button>
      {source.cleanup === "unknown_writer_retained" && <><p>Cleanup is unknown. The original {source.writer_kind === "upload" ? "upload lease must prove its writer is closed" : "parser must prove it was reaped"}. Capacity remains held until exact positive closure.</p><button type="button" disabled={busy} onClick={() => void reconcile()}>{source.writer_kind === "upload" ? "Reconcile original document upload cleanup" : "Reconcile original document reader cleanup"}</button></>}
      {source.format === "pdf" && <label>Physical PDF pages (comma separated)<input aria-label="Physical PDF pages" disabled={busy} value={pages} onChange={e => { setPages(e.target.value); setEvidence(null); }} /></label>}
      {source.format === "xlsx" && <label>XLSX sheets (one per line)<textarea aria-label="XLSX sheets" disabled={busy} value={sheets} onChange={e => { setSheets(e.target.value); setEvidence(null); }} /></label>}
      <button type="button" disabled={busy || source.state !== "sealed" || source.cleanup === "unknown_writer_retained"} onClick={() => void read()}>Read cited document evidence</button>
      <button type="button" disabled={busy || source.state === "deleted"} onClick={() => void remove()}>Delete private source and verify cleanup</button>
      {source.state !== "sealed" && source.state !== "deleted" && <p>Inspect an interrupted upload before taking another action. Reconcile its exact writer closure first; then delete this retained source, verify cleanup, and explicitly select the file again. Recovery never resumes or seals a partial upload.</p>}
    </>}
    {evidence && <div aria-label="Cited document evidence" role="region">
      {evidence.warnings.map((warning, i) => <p key={i} role="status">{warning}</p>)}
      {evidence.sections.map((section, i) => <section key={i}><h4 className="font-mono break-all">{section.source_ref}</h4><pre className="whitespace-pre-wrap break-all">{section.text}</pre>
        {section.table_cells.length > 0 && <table><thead><tr><th>Source</th><th>Text</th><th>Formula (inert)</th><th>Cached value</th></tr></thead><tbody>{section.table_cells.map((cell, j) => <tr key={j}><td>{cell.source_ref}</td><td>{cell.text}</td><td>{cell.formula ?? "none"}</td><td>{cell.cached_value ?? "unavailable"}</td></tr>)}</tbody></table>}
      </section>)}
      <section aria-label="Select cited document spans" className="mt-3 rounded border border-white/10 p-2">
        <p>Select exact existing citations for a local preparation task. Table sections are containers; select individual cells so unselected cells stay private. {selectedRefs.length}/16 selected.</p>
        <ul aria-label="Available document citations">
          {citationLeaves(evidence).map((leaf) => <li key={leaf.source_ref}>
            <label className="flex gap-2">
              <input type="checkbox" aria-label={`Select citation ${leaf.source_ref}`} checked={selectedRefs.includes(leaf.source_ref)} disabled={busy || (!selectedRefs.includes(leaf.source_ref) && selectedRefs.length >= 16)} onChange={(event) => {
                const checked = event.currentTarget.checked;
                resetPreparationTask();
                setSelectedRefs((previous) => checked
                  ? (previous.includes(leaf.source_ref) || previous.length >= 16 ? previous : [...previous, leaf.source_ref])
                  : previous.filter((ref) => ref !== leaf.source_ref));
              }} />
              <span className="break-all">{leaf.source_ref} · {leaf.text.slice(0, 160)}{leaf.text.length > 160 ? "…" : ""}</span>
            </label>
          </li>)}
        </ul>
        <label className="mt-2 flex gap-2"><input type="checkbox" aria-label="Acknowledge local document use" checked={prepareAck} disabled={busy} onChange={(event) => { resetPreparationTask(); setPrepareAck(event.currentTarget.checked); }} />I acknowledge using these exact citations for this Goal's local preparation task. Document content stays local; model egress is false, inference calls and cost are zero.</label>
        {preparationPending && <p role="status">The exact preparation request has no confirmed receipt. Retry it with the same idempotency key or inspect Work; no new task request is generated.</p>}
        <button type="button" className="mt-2" disabled={busy || source?.state !== "sealed" || source?.cleanup === "unknown_writer_retained" || !selectedRefs.length || !prepareAck} onClick={() => void prepare()}>{preparationPending ? "Retry exact preparation request" : "Prepare cited local task"}</button>
      </section>
      {preparationTask && <section aria-label="Document preparation task" className="mt-3 rounded border border-white/10 p-2">
        <p role="status">Persisted preparation task {preparationTask.task_id} · {preparationTask.status}. Review and accept this exact one-step task in Work; this reader never accepts or executes it.</p>
        <p className="text-xs">Selected citations are bound to the sealed source revision and Goal. Local preparation has no model or provider contact and no learning.</p>
        {onOpenTask && <button type="button" disabled={busy} onClick={() => onOpenTask(preparationTask.task_id)}>Review task in Work</button>}
        <button type="button" disabled={busy} onClick={() => void refreshPreparation()}>Refresh cited preparation</button>
        {preparationView && <section aria-label="Authenticated private cited preparation" className="mt-2 rounded border border-emerald-500/30 p-2">
          <p role="status">Authenticated private cited preparation · {preparationView.task_id} · no provider contact · no learning.</p>
          {preparationView.sections.map((section) => <article key={section.source_ref} className="mt-2"><h4 className="font-mono break-all">{section.source_ref}</h4><pre className="whitespace-pre-wrap break-all">{section.text}</pre>{(section.formula !== null || section.cached_value !== null) && <dl><dt>Formula (inert)</dt><dd className="whitespace-pre-wrap break-all">{section.formula ?? "none"}</dd><dt>Cached value (freshness unknown)</dt><dd className="whitespace-pre-wrap break-all">{section.cached_value ?? "unavailable"}</dd></dl>}{section.cached_value === null && <p role="status">Cached value unavailable; freshness unknown. Formula remains inert.</p>}</article>)}
        </section>}
      </section>}
    </div>}
  </section></details>;
}
