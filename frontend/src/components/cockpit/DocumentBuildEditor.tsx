import { useEffect, useRef, useState } from "react";
import type { GoalInfo, WorkBoardTask } from "../../types";
import { generalTaskRequest, validDocumentTaskBinding, type DocumentTaskBinding, type GeneralTaskPlanRead } from "../../lib/generalTask";
import { DocumentBuildError, documentRequest, parseBuildOutputs, parseBuildPreview, parseBuildProjection, readBuildDownload, validateBuildSpec, MEDIA } from "../../lib/documentBuild";
import type { BuildOutputs, BuildPreview, BuildProjection, CitationLeaf, DocumentBuildSpec, ReportTable, SpreadsheetSpec } from "../../lib/documentBuild";

interface Props {
  ownerPrincipalId: string; ownerSessionId: string; goals: GoalInfo[];
  task?: WorkBoardTask; read?: GeneralTaskPlanRead;
  onCreated?: (task: WorkBoardTask) => void | Promise<void>; onChanged?: () => void | Promise<void>;
}
interface Source { artifact_ref: string; revision: number; goal_id: string; goal_revision: number; state: string }
const initial = (): DocumentBuildSpec => ({ kind: "report", title: "", sections: [{ heading: "", paragraphs: [""], citation_refs: [] }], tables: [], citations: [], style_preset: "plain" });
const workbook = (): SpreadsheetSpec => ({ sheet_names: ["Sheet1"], cells: [], formulas: [], formats: [] });
export function DocumentBuildEditor(props: Props) {
  // Remount on owner/task changes. Private fields and outstanding request bodies never leave this component.
  return <OwnedEditor key={`${props.ownerPrincipalId}:${props.ownerSessionId}:${props.task?.task_id ?? "new"}`} {...props} />;
}
function OwnedEditor({ ownerPrincipalId, ownerSessionId, goals, task, read, onCreated, onChanged }: Props) {
  const [spec, setSpec] = useState<DocumentBuildSpec>(initial);
  const [goalId, setGoalId] = useState("");
  const [sources, setSources] = useState<Source[]>([]), [source, setSource] = useState<Source | null>(null);
  const [leaves, setLeaves] = useState<CitationLeaf[]>([]), [offset, setOffset] = useState<number | null>(null);
  const [binding, setBinding] = useState<DocumentTaskBinding | null>(null);
  const [projection, setProjection] = useState<BuildProjection | null>(null), [preview, setPreview] = useState<BuildPreview | null>(null);
  const [retained, setRetained] = useState<BuildProjection[]>([]), [buildOffset, setBuildOffset] = useState<number | null>(null);
  const [outputs, setOutputs] = useState<BuildOutputs | null>(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState<string | null>(null), [ack, setAck] = useState(false), [retireAck, setRetireAck] = useState(false);
  const alive = useRef(true), lock = useRef(false), abort = useRef(new AbortController());
  const pending = useRef<{ body: unknown; path: string; method: string } | null>(null);
  const ownedGoals = goals.filter(g => g.status === "active" && g.owner_session_id === ownerSessionId && g.ownership_access !== "recovered_read_only" && g.revision);
  const goal = ownedGoals.find(g => g.id === goalId);
  const owned = !task || task.owner_principal_id === ownerPrincipalId && task.owner_session_id === ownerSessionId && task.ownership_access !== "recovered_read_only";
  const terminalForRetirement = !!task?.latest_attempt?.ended_at && (["done", "cancelled"].includes(task.status) || task.status === "blocked" && task.block_reason === "general_task_native_cancel_fully_cancelled");
  const requestScope = `${task?.task_revision ?? 0}:${read?.task_revision ?? 0}:${owned}`;
  const currentScope = useRef(requestScope); currentScope.current = requestScope;
  const current = () => alive.current && currentScope.current === requestScope;
  const buildId = task ? read?.task_input.document_build?.build_ref.slice("document-build:".length) : projection?.build_id;
  useEffect(() => { alive.current = true; return () => { alive.current = false; abort.current.abort(); }; }, []);
  useEffect(() => { setPreview(null); setAck(false); setOutputs(null); setBusy(false); }, [task?.task_revision, read?.task_revision]);
  async function run(fn: () => Promise<void>) {
    if (lock.current || !owned || !current()) return;
    lock.current = true; setBusy(true); setError(null);
    try { await fn(); } catch (e) { if (current()) {
      if (e instanceof DocumentBuildError && [400, 401, 403, 409, 422].includes(e.status) && pending.current?.method !== "DELETE") {
        pending.current = null; setPreview(null);
      }
      setPreview(null); setAck(false); setError((e as Error).message);
    } }
    finally { lock.current = false; if (current()) setBusy(false); }
  }
  function edit(next: DocumentBuildSpec) { setSpec(next); setPreview(null); setBinding(null); setAck(false); setOutputs(null); }
  async function fetchPreview(id: string, expected?: BuildProjection) {
    if (current()) { setPreview(null); setAck(false); }
    const p = parseBuildPreview(await documentRequest(`/builds/${encodeURIComponent(id)}/preview`, undefined, "GET", abort.current.signal));
    if (p.build_id !== id || p.review.binding.owner_principal_id !== ownerPrincipalId || p.review.binding.owner_session_id !== ownerSessionId
      || expected && (p.goal_id !== expected.goal_id || p.goal_revision !== expected.goal_revision || p.spec_digest !== expected.spec_digest || p.selection_digest !== expected.selection_digest)
      || task && (p.task_id !== task.task_id || p.goal_id !== task.goal_id || p.goal_revision !== task.goal_revision || p.review.binding.task_revision !== read?.task_revision || p.review.binding.plan_revision !== read?.plan?.revision
        || p.spec_digest !== read?.task_input.document_build?.spec_digest || p.selection_digest !== read.task_input.document_build.selection_digest)) throw Error("Private preview does not match this owner and current task plan. Refresh Work and review again.");
    if (current()) { setPreview(p); setProjection(p); setAck(false); }
  }
  async function stage() {
    await run(async () => {
      if (pending.current && pending.current.path !== "/builds") throw Error("Reconcile the original outstanding action before starting another build.");
      if (!pending.current) {
        if (!goal?.revision) throw Error("Choose a current active owned Goal.");
        validateBuildSpec(spec);
        let selected: DocumentTaskBinding | null = null;
        if (spec.citations.length) {
          if (!source || source.goal_id !== goal.id || source.goal_revision !== goal.revision) throw Error("Choose an adopted source belonging to this current Goal revision.");
          const value = await documentRequest(`/sources/${encodeURIComponent(source.artifact_ref.slice(16))}/selection`, { expected_revision: source.revision, citation_refs: spec.citations.map(c => c.source_ref), acknowledge_local_use: true }, "POST", abort.current.signal);
          if (!value || typeof value !== "object" || !("source" in value) || !("goal_id" in value) || value.goal_id !== goal.id || !("goal_revision" in value) || value.goal_revision !== goal.revision) throw Error("Source selection receipt is unconfirmed.");
          selected = value.source as DocumentTaskBinding;
          if (!validDocumentTaskBinding(selected) || selected.artifact_ref !== source.artifact_ref || selected.source_revision !== source.revision || JSON.stringify(selected.citation_refs) !== JSON.stringify(spec.citations.map(c => c.source_ref))) throw Error("Exact source selection changed. Review citations again.");
          if (!current()) return;
          setBinding(selected);
        }
        pending.current = { path: "/builds", method: "POST", body: { goal_id: goal.id, goal_revision: goal.revision, spec, source: selected, idempotency_key: crypto.randomUUID() } };
      }
      const request = pending.current;
      const p = parseBuildProjection(await documentRequest(request.path, request.body, request.method, abort.current.signal));
      const requested = request.body as { goal_id: string; goal_revision: number };
      if (p.goal_id !== requested.goal_id || p.goal_revision !== requested.goal_revision || p.task_id !== null) throw Error("Private build receipt changed the selected Goal or inert state. Inspect retained builds before retrying.");
      if (!current()) return;
      pending.current = null; setProjection(p); await fetchPreview(p.build_id, p);
    });
  }
  async function prepare() {
    await run(async () => {
      if (pending.current && pending.current.path !== `/builds/${preview?.build_id}/prepare`) throw Error("Reconcile the original outstanding action first.");
      if (!preview || !ack || preview.task_id || Date.parse(preview.review.binding.expires_at) <= Date.now()) throw Error("Reload and acknowledge the current signed review.");
      pending.current ??= { path: `/builds/${preview.build_id}/prepare`, method: "POST", body: { expected_revision: preview.revision, review: preview.review, idempotency_key: crypto.randomUUID() } };
      const req = pending.current;
      const value = await documentRequest(req.path, req.body, req.method, abort.current.signal);
      if (!value || typeof value !== "object" || !("task" in value)) throw Error("Task receipt is unconfirmed. Inspect Work before retrying the same request.");
      const receipt = value.task as WorkBoardTask;
      if (receipt.owner_principal_id !== ownerPrincipalId || receipt.owner_session_id !== ownerSessionId || receipt.goal_id !== preview.goal_id || receipt.goal_revision !== preview.goal_revision || receipt.capability_id !== "agent.task.v1" || receipt.status !== "triage" || receipt.requires_review !== true || !receipt.task_id) throw Error("Prepared inert task does not match the original owner and Goal.");
      if (!current()) return;
      pending.current = null; setAck(false); await onCreated?.(receipt);
    });
  }
  async function accept() {
    await run(async () => {
      const b = preview?.review.binding;
      if (!task || !read?.plan || read.accepted || !ack || !b || b.task_id !== task.task_id || b.task_revision !== read.task_revision || b.plan_revision !== read.plan.revision || Date.parse(b.expires_at) <= Date.now()) throw Error("Refresh the current task and signed review before accepting.");
      await generalTaskRequest(`/tasks/${encodeURIComponent(task.task_id)}/actions`, { action: "promote", expected_revision: read.task_revision, document_build_review: preview!.review }, abort.current.signal);
      if (current()) { setPreview(null); setAck(false); await onChanged?.(); }
    });
  }
  async function loadSources() {
    await run(async () => {
      const v = await documentRequest("/sources", undefined, "GET", abort.current.signal);
      if (!v || typeof v !== "object" || !("sources" in v) || !Array.isArray(v.sources) || v.sources.length > 100) throw Error("Source list is unconfirmed.");
      const list = v.sources.filter((s): s is Source => !!s && typeof s === "object" && typeof s.artifact_ref === "string" && /^document-source:[0-9a-f-]{36}$/.test(s.artifact_ref) && Number.isSafeInteger(s.revision) && s.goal_id === goal?.id && s.goal_revision === goal?.revision && s.state === "sealed");
      if (current()) setSources(list);
    });
  }
  async function loadBuilds(next = 0) { await run(async () => {
    const v = await documentRequest(`/builds?limit=50&offset=${next}`, undefined, "GET", abort.current.signal);
    if (!v || typeof v !== "object" || !("builds" in v) || !Array.isArray(v.builds) || v.builds.length > 50 || !("next_offset" in v) || !("no_learning" in v) || v.no_learning !== true) throw Error("Retained build list is unconfirmed.");
    const list = v.builds.map(parseBuildProjection);
    if (v.next_offset !== null && (!Number.isSafeInteger(v.next_offset) || Number(v.next_offset) <= next || Number(v.next_offset) > 10000)) throw Error("Retained build page exceeded its bound.");
    if (current()) { setRetained(old => next ? [...old, ...list] : list); setBuildOffset(v.next_offset as number | null); }
  }); }
  async function loadCitations(s: Source, next = 0) {
    await run(async () => {
      const v = await documentRequest(`/sources/${encodeURIComponent(s.artifact_ref.slice(16))}/citations?limit=100&offset=${next}`, undefined, "GET", abort.current.signal);
      if (!v || typeof v !== "object" || !("artifact_ref" in v) || v.artifact_ref !== s.artifact_ref || !("source_revision" in v) || v.source_revision !== s.revision || !("citations" in v) || !Array.isArray(v.citations) || v.citations.length > 100 || !("next_offset" in v)) throw Error("Private citations changed. Reload the adopted source.");
      const refs = v.citations.filter((c): c is CitationLeaf => !!c && typeof c === "object" && typeof c.source_ref === "string" && c.source_ref.length <= 512 && typeof c.text === "string" && new TextEncoder().encode(c.text).length <= 16384);
      if (refs.length !== v.citations.length || (v.next_offset !== null && (!Number.isSafeInteger(v.next_offset) || Number(v.next_offset) <= next || Number(v.next_offset) > 10000))) throw Error("Citation page exceeded its finite bounds.");
      if (current()) { setSource(s); setLeaves(old => next ? [...old, ...refs] : refs); setOffset(typeof v.next_offset === "number" ? v.next_offset : null); }
    });
  }
  async function loadOutputs() { await run(async () => { if (!buildId) return; const v = parseBuildOutputs(await documentRequest(`/builds/${buildId}/outputs`, undefined, "GET", abort.current.signal)); if (v.task_id !== task?.task_id || v.build_id !== buildId || v.goal_id !== task?.goal_id || v.goal_revision !== task?.goal_revision || v.spec_digest !== read?.task_input.document_build?.spec_digest || v.selection_digest !== read.task_input.document_build.selection_digest) throw Error("Output lineage changed. Refresh Work."); if (current()) { setOutputs(v); setProjection(v); } }); }
  async function inspectBuild() { await run(async () => {
    if (!buildId) return;
    const p = parseBuildProjection(await documentRequest(`/builds/${buildId}`, undefined, "GET", abort.current.signal));
    if (p.build_id !== buildId || task && p.task_id !== task.task_id) throw Error("Original build lineage changed. Refresh Work.");
    if (current()) { setProjection(p); setPreview(null); setAck(false); }
  }); }
  async function openOriginalTask() { await run(async () => {
    if (!projection?.task_id) return;
    const value = await generalTaskRequest(`/tasks/${encodeURIComponent(projection.task_id)}`, undefined, abort.current.signal);
    if (!value || typeof value !== "object" || !("task" in value)) throw Error("Original Task readback is unavailable. Refresh Work.");
    const t = value.task as WorkBoardTask;
    if (t.task_id !== projection.task_id || t.goal_id !== projection.goal_id || t.owner_principal_id !== ownerPrincipalId || t.owner_session_id !== ownerSessionId) throw Error("Original task authority changed. Refresh Work.");
    if (current()) await onCreated?.(t);
  }); }
  async function download(slot: "editable" | "pdf") { await run(async () => {
    if (!outputs) return; const a = outputs.output[`${slot}_artifact`]; if (!a) throw Error("PDF is unavailable; editable output remains retained.");
    const blob = await readBuildDownload(outputs.build_id, slot, a, abort.current.signal); if (!current()) return;
    const url = URL.createObjectURL(blob), anchor = document.createElement("a");
    try { anchor.href = url; anchor.download = `document-${outputs.build_id}.${slot === "pdf" ? "pdf" : a.media_type === MEDIA.xlsx ? "xlsx" : "docx"}`; anchor.click(); } finally { URL.revokeObjectURL(url); }
  }); }
  async function retire() { await run(async () => {
    if (pending.current && (pending.current.method !== "DELETE" || pending.current.path !== `/builds/${projection?.build_id}`)) throw Error("Reconcile the original outstanding action first.");
    if (!projection || !retireAck || (task && (!task.latest_attempt?.attempt_id || !terminalForRetirement))) throw Error("Bound build retirement requires the original ended task and verified native cleanup.");
    pending.current ??= { path: `/builds/${projection.build_id}`, method: "DELETE", body: { expected_revision: projection.revision, ...(task ? { expected_task_revision: task.task_revision, attempt_id: task.latest_attempt!.attempt_id } : {}), idempotency_key: crypto.randomUUID() } };
    const req = pending.current; const p = parseBuildProjection(await documentRequest(req.path, req.body, req.method, abort.current.signal));
    if (current()) { pending.current = null; setProjection(p); setPreview(null); setOutputs(null); setRetireAck(false); await onChanged?.(); }
  }); }
  const disabled = busy || !owned || !!pending.current;
  const book = spec.kind === "table_workbook" ? spec.tables[0] as SpreadsheetSpec : null;
  return <section aria-label="Local document build" className="grid gap-3 mt-3">
    <h3>Build an editable document and PDF</h3>
    <p>Local CPU rendering · no provider contact · no learning. Review creates an inert task; explicit task acceptance authorizes execution.</p>
    {!owned && <p role="status">Current original task ownership is required.</p>}
    {error && <p role="alert">{error}</p>}
    {busy && <p role="status">Waiting for the authenticated document service…</p>}
    {!task && <><button type="button" disabled={busy || !owned || !!pending.current} onClick={() => void loadBuilds()}>Refresh retained private builds</button>{retained.map(p => <button type="button" key={p.build_id} disabled={busy || !!pending.current} onClick={() => { setProjection(p); setPreview(null); setOutputs(null); setAck(false); setRetireAck(false); }}>{p.state} · Goal {p.goal_id} · reserved {p.quota_reserved_bytes} bytes{p.task_id ? " · bound task; open its Work card" : " · unbound"}</button>)}{buildOffset !== null && <button type="button" disabled={busy} onClick={() => void loadBuilds(buildOffset)}>Load more retained builds</button>}</>}
    {!task && <fieldset disabled={disabled} className="grid gap-3">
      <label>Document Goal<select aria-label="Document Goal" value={goalId} onChange={e => { setGoalId(e.target.value); setSources([]); setSource(null); setLeaves([]); edit({ ...spec, citations: [], sections: spec.sections.map(s => ({ ...s, citation_refs: [] })) }); }}><option value="">Choose active Goal</option>{ownedGoals.map(g => <option key={g.id} value={g.id}>{g.title} · revision {g.revision}</option>)}</select></label>
      <label>Document kind<select aria-label="Document kind" value={spec.kind} onChange={e => edit({ ...spec, kind: e.target.value as DocumentBuildSpec["kind"], tables: e.target.value === "table_workbook" ? [workbook()] : [] })}><option value="report">Report · DOCX + PDF</option><option value="brief">Brief · DOCX + PDF</option><option value="table_workbook">Workbook · XLSX + PDF</option></select></label>
      <label>Document title<input aria-label="Document title" maxLength={200} value={spec.title} onChange={e => edit({ ...spec, title: e.target.value })} /></label>
      <label>Document style<select aria-label="Document style" value={spec.style_preset} onChange={e => edit({ ...spec, style_preset: e.target.value as "plain" | "compact" })}><option value="plain">Plain</option><option value="compact">Compact</option></select></label>
      {spec.sections.map((s, i) => <fieldset key={i} className="grid gap-2 border p-2"><legend>Section {i + 1}</legend>
        <label>Heading<input aria-label={`Section ${i + 1} heading`} maxLength={200} value={s.heading} onChange={e => edit({ ...spec, sections: spec.sections.map((row, n) => n === i ? { ...row, heading: e.target.value } : row) })} /></label>
        {s.paragraphs.map((p, j) => <label key={j}>Paragraph {j + 1}<textarea aria-label={`Section ${i + 1} paragraph ${j + 1}`} value={p} maxLength={4096} onChange={e => edit({ ...spec, sections: spec.sections.map((row, n) => n === i ? { ...row, paragraphs: row.paragraphs.map((text, k) => k === j ? e.target.value : text) } : row) })} /><button type="button" onClick={() => edit({ ...spec, sections: spec.sections.map((row, n) => n === i ? { ...row, paragraphs: row.paragraphs.filter((_, k) => k !== j) } : row) })}>Remove paragraph {j + 1}</button></label>)}
        <button type="button" disabled={spec.sections.reduce((n, r) => n + r.paragraphs.length, 0) >= 128} onClick={() => edit({ ...spec, sections: spec.sections.map((row, n) => n === i ? { ...row, paragraphs: [...row.paragraphs, ""] } : row) })}>Add paragraph to section {i + 1}</button>
        {spec.citations.map(c => <label key={c.source_ref}><input type="checkbox" checked={s.citation_refs.includes(c.source_ref)} onChange={e => edit({ ...spec, sections: spec.sections.map((row, n) => n === i ? { ...row, citation_refs: e.target.checked ? [...row.citation_refs, c.source_ref] : row.citation_refs.filter(r => r !== c.source_ref) } : row) })} />Cite {c.label} in section {i + 1}</label>)}
        <button type="button" onClick={() => edit({ ...spec, sections: spec.sections.filter((_, n) => n !== i) })}>Remove section {i + 1}</button>
      </fieldset>)}
      <button type="button" disabled={spec.sections.length >= 32} onClick={() => edit({ ...spec, sections: [...spec.sections, { heading: "", paragraphs: [""], citation_refs: [] }] })}>Add section</button>
      {!book && <><button type="button" disabled={spec.tables.length >= 16} onClick={() => edit({ ...spec, tables: [...spec.tables, { title: "", columns: ["Column 1"], rows: [[""]], citation_refs: [] }] })}>Add table</button>{(spec.tables as ReportTable[]).map((t, i) => <fieldset key={i}><legend>Table {i + 1}</legend>
        <label>Table title<input aria-label={`Table ${i + 1} title`} maxLength={200} value={t.title} onChange={e => edit({ ...spec, tables: spec.tables.map((row, n) => n === i ? { ...t, title: e.target.value } : row) })} /></label>
        {t.columns.map((c, j) => <label key={j}>Column {j + 1}<input aria-label={`Table ${i + 1} column ${j + 1}`} value={c} onChange={e => edit({ ...spec, tables: spec.tables.map((row, n) => n === i ? { ...t, columns: t.columns.map((v, k) => k === j ? e.target.value : v) } : row) })} /></label>)}
        {t.rows.map((r, j) => <div key={j}>{r.map((c, k) => <label key={k}>Row {j + 1}, column {k + 1}<input aria-label={`Table ${i + 1} row ${j + 1} column ${k + 1}`} value={String(c ?? "")} onChange={e => edit({ ...spec, tables: spec.tables.map((row, n) => n === i ? { ...t, rows: t.rows.map((a, m) => m === j ? a.map((v, p) => p === k ? e.target.value : v) : a) } : row) })} /></label>)}</div>)}
        <button type="button" disabled={t.columns.length >= 64} onClick={() => edit({ ...spec, tables: spec.tables.map((row, n) => n === i ? { ...t, columns: [...t.columns, ""], rows: t.rows.map(r => [...r, ""]) } : row) })}>Add column to table {i + 1}</button>
        <button type="button" disabled={t.rows.length >= 256} onClick={() => edit({ ...spec, tables: spec.tables.map((row, n) => n === i ? { ...t, rows: [...t.rows, t.columns.map(() => "")] } : row) })}>Add row to table {i + 1}</button>
        {spec.citations.map(c => <label key={c.source_ref}><input type="checkbox" checked={t.citation_refs.includes(c.source_ref)} onChange={e => edit({ ...spec, tables: spec.tables.map((row, n) => n === i ? { ...t, citation_refs: e.target.checked ? [...t.citation_refs, c.source_ref] : t.citation_refs.filter(r => r !== c.source_ref) } : row) })} />Cite {c.label} in table {i + 1}</label>)}
        <button type="button" onClick={() => edit({ ...spec, tables: spec.tables.filter((_, n) => n !== i) })}>Remove table {i + 1}</button>
      </fieldset>)}</>}
      {book && <WorkbookFields book={book} onChange={b => edit({ ...spec, tables: [b] })} />}
      <button type="button" disabled={!goal} onClick={() => void loadSources()}>Choose citations from an adopted source</button>
      {sources.map(s => <button key={s.artifact_ref} type="button" onClick={() => void loadCitations(s)}>Open source {s.artifact_ref} · revision {s.revision}</button>)}
      {leaves.map(c => <label key={c.source_ref}><input type="checkbox" disabled={!spec.citations.some(v => v.source_ref === c.source_ref) && spec.citations.length >= 16} checked={spec.citations.some(v => v.source_ref === c.source_ref)} onChange={e => edit({ ...spec, citations: e.target.checked ? [...spec.citations, { source_ref: c.source_ref, label: `Source ${spec.citations.length + 1}` }] : spec.citations.filter(v => v.source_ref !== c.source_ref), sections: spec.sections.map(s => ({ ...s, citation_refs: e.target.checked ? s.citation_refs : s.citation_refs.filter(r => r !== c.source_ref) })) })} />{c.source_ref}<span className="whitespace-pre-wrap">{c.text ?? ""}</span></label>)}
      {offset !== null && source && <button type="button" onClick={() => void loadCitations(source, offset)}>Load more source citations</button>}
      {spec.citations.map((c, i) => <label key={c.source_ref}>Citation label {i + 1}<input value={c.label} maxLength={200} onChange={e => edit({ ...spec, citations: spec.citations.map((r, n) => n === i ? { ...r, label: e.target.value } : r) })} /></label>)}
      <p>At most 32 sections, 128 paragraphs, 16 tables/citations and 65536 specification bytes. Formulas are validated deterministically; edits require a fresh private build and review.</p>
    </fieldset>}
    {binding && <p>Exact selected source acknowledged for local use: {binding.citation_refs.length} citations.</p>}
    {!task && <button type="button" disabled={busy || !owned || (pending.current ? pending.current.path !== "/builds" : !goal || !!projection && projection.state !== "deleted")} onClick={() => void stage()}>{pending.current?.path === "/builds" ? "Reconcile exact private build request" : "Stage private build and review"}</button>}
    {!task && projection && projection.state !== "deleted" && <><p>Editing fields requires retiring this charged private build, then staging a fresh specification and review.</p><label><input type="checkbox" checked={retireAck} disabled={busy} onChange={e => setRetireAck(e.target.checked)} />Discard this unbound private build</label><button type="button" disabled={busy || !retireAck || projection.task_id !== null} onClick={() => void retire()}>Retire unbound private build</button></>}
    {pending.current && <p role="status">An exact request is unconfirmed. Inspect Work and reconcile this request; changing fields or retrying another effect is blocked.</p>}
    {projection && <p role="status">Build {projection.state} · reserved storage {projection.quota_reserved_bytes} bytes · {projection.reason_code ?? "no current block"}. Unknown or partial cleanup remains charged until positive reconciliation.</p>}
    {!task && projection?.task_id && <button type="button" disabled={busy || !owned || !!pending.current} onClick={() => void openOriginalTask()}>Open original document Task</button>}
    {buildId && <button type="button" disabled={busy || !owned || !!pending.current} onClick={() => void run(() => fetchPreview(buildId))}>Open current signed document review</button>}
    {buildId && <button type="button" disabled={busy || !owned || !!pending.current} onClick={() => void inspectBuild()}>Inspect original charged build state</button>}
    {preview && <section aria-label="Signed private document review"><h4>{preview.spec.title}</h4><p>Goal {goals.find(g => g.id === preview.goal_id)?.title ?? preview.goal_id} · revision {preview.goal_revision}</p><p>{preview.formats.join(" + ").toUpperCase()} · style {preview.spec.style_preset} · review expires {preview.review.binding.expires_at} · original deadline {preview.original_deadline}</p>
      {preview.spec.sections.map((s, i) => <article key={i}><h5>{s.heading}</h5>{s.paragraphs.map((p, j) => <p key={j} className="whitespace-pre-wrap">{p}</p>)}<p>Citations: {s.citation_refs.join(", ") || "none"}</p></article>)}
      {preview.spec.tables.map((t, i) => "columns" in t ? <table key={i}><caption>{t.title}</caption><thead><tr>{t.columns.map((c, j) => <th key={j}>{c}</th>)}</tr></thead><tbody>{t.rows.map((r, j) => <tr key={j}>{r.map((c, k) => <td key={k}>{String(c ?? "")}</td>)}</tr>)}</tbody></table> : <div key={i}><p>Sheets: {t.sheet_names.join(", ")}</p>{t.cells.map(c => <p key={`${c.sheet}:${c.cell}`}>{c.sheet}!{c.cell}: {String(c.value ?? "blank")} (literal)</p>)}{t.formulas.map(c => <p key={`${c.sheet}:${c.cell}`}>{c.sheet}!{c.cell}: {c.expression} (validated formula; computed cached value written on execution)</p>)}</div>)}
      {preview.selection.map(c => <p key={c.source_ref} className="whitespace-pre-wrap">{c.source_ref}: {c.text ?? ""}</p>)}
      <p>Specification limit 64 KiB; editable and PDF output limits 4 MiB each. Permissions are private local artifact writing only; no external action or model composition.</p>
      {(task ? !read?.accepted : preview.task_id === null) && <><label><input type="checkbox" disabled={busy || Date.parse(preview.review.binding.expires_at) <= Date.now()} checked={ack} onChange={e => setAck(e.target.checked)} />I reviewed this exact private content, sources, formats and local rendering limits.</label><button type="button" disabled={busy || !owned || !ack || Date.parse(preview.review.binding.expires_at) <= Date.now() || !!pending.current && pending.current.path !== `/builds/${preview.build_id}/prepare`} onClick={() => void (task ? accept() : prepare())}>{task ? "Accept reviewed document task" : pending.current ? "Reconcile exact preparation" : "Prepare inert document task"}</button></>}
    </section>}
    {task && buildId && <><button type="button" disabled={busy || !owned} onClick={() => void loadOutputs()}>Read verified document outputs</button>{outputs && <section aria-label="Verified document outputs"><p role="status">{outputs.state} · original task {outputs.task_id} · private source references {outputs.output.source_refs.join(", ") || "none"}</p>{outputs.output.warnings.map((w, i) => <p role="status" key={i}>{w}</p>)}{!outputs.output.pdf_artifact && <p role="status">PDF unavailable. The editable document remains retained and downloadable; inspect the warning before creating a fresh reviewed build.</p>}<button type="button" disabled={busy} onClick={() => void download("editable")}>Download editable document</button><button type="button" disabled={busy || !outputs.output.pdf_artifact} onClick={() => void download("pdf")}>Download PDF</button></section>}
      <label><input type="checkbox" checked={retireAck} disabled={busy} onChange={e => setRetireAck(e.target.checked)} />Retire the original private build and outputs after verified terminal cleanup.</label><button type="button" disabled={busy || !owned || !retireAck || !projection || !terminalForRetirement} onClick={() => void retire()}>Retire private document build</button>
    </>}
  </section>;
}
function WorkbookFields({ book, onChange }: { book: SpreadsheetSpec; onChange: (book: SpreadsheetSpec) => void }) {
  const entries = [...book.cells.map(c => ({ ...c, expression: null as string | null })), ...book.formulas.map(c => ({ ...c, value: null, expression: c.expression }))];
  function update(i: number, patch: Partial<typeof entries[number]>) {
    const next = entries.map((c, n) => n === i ? { ...c, ...patch } : c);
    const before = entries[i], after = next[i];
    onChange({ ...book, cells: next.filter(c => c.expression === null).map(({ sheet, cell, value }) => ({ sheet, cell, value })), formulas: next.filter(c => c.expression !== null).map(({ sheet, cell, expression }) => ({ sheet, cell, expression: expression! })), formats: book.formats.map(f => f.sheet === before.sheet && f.cell === before.cell ? { ...f, sheet: after.sheet, cell: after.cell } : f) });
  }
  function addCell() {
    const sheet = book.sheet_names[0]; const used = new Set(entries.filter(c => c.sheet === sheet).map(c => c.cell.replace(/\$/g, "")));
    for (let n = 0; n < 16384; n++) { const column = Math.floor(n / 256) + 1; const name = column <= 26 ? String.fromCharCode(64 + column) : `${String.fromCharCode(64 + Math.floor((column - 1) / 26))}${String.fromCharCode(65 + (column - 1) % 26)}`; const cell = `${name}${n % 256 + 1}`; if (!used.has(cell)) { onChange({ ...book, cells: [...book.cells, { sheet, cell, value: "" }] }); return; } }
  }
  return <fieldset className="grid gap-2"><legend>Workbook cells</legend>
    {book.sheet_names.map((s, i) => <label key={i}>Sheet {i + 1}<input aria-label={`Sheet ${i + 1} name`} maxLength={31} value={s} onChange={e => onChange({ ...book, sheet_names: book.sheet_names.map((v, n) => n === i ? e.target.value : v), cells: book.cells.map(c => c.sheet === s ? { ...c, sheet: e.target.value } : c), formulas: book.formulas.map(c => c.sheet === s ? { ...c, sheet: e.target.value } : c), formats: book.formats.map(c => c.sheet === s ? { ...c, sheet: e.target.value } : c) })} /></label>)}
    <button type="button" disabled={book.sheet_names.length >= 8} onClick={() => onChange({ ...book, sheet_names: [...book.sheet_names, `Sheet${book.sheet_names.length + 1}`] })}>Add sheet</button>
    {entries.map((c, i) => <fieldset key={i}><legend>Cell {i + 1}</legend><label>Sheet<select aria-label={`Cell ${i + 1} sheet`} value={c.sheet} onChange={e => update(i, { sheet: e.target.value })}>{book.sheet_names.map(s => <option key={s}>{s}</option>)}</select></label><label>Address<input aria-label={`Cell ${i + 1} address`} maxLength={7} value={c.cell} onChange={e => update(i, { cell: e.target.value.toUpperCase() })} /></label>
      <label>Value type<select aria-label={`Cell ${i + 1} type`} value={c.expression !== null ? "formula" : c.value === null ? "blank" : typeof c.value} onChange={e => update(i, e.target.value === "formula" ? { expression: "=SUM(A1:A2)", value: null } : { expression: null, value: e.target.value === "number" ? 0 : e.target.value === "boolean" ? false : e.target.value === "blank" ? null : "" })}><option value="string">Literal text</option><option value="number">Number</option><option value="boolean">Boolean</option><option value="blank">Blank</option><option value="formula">Formula</option></select></label>
      {c.expression !== null ? <label>Formula<input aria-label={`Cell ${i + 1} formula`} maxLength={512} value={c.expression} onChange={e => update(i, { expression: e.target.value })} /></label> : typeof c.value === "boolean" ? <label>True<input aria-label={`Cell ${i + 1} boolean`} type="checkbox" checked={c.value} onChange={e => update(i, { value: e.target.checked })} /></label> : c.value !== null && <label>Value<input aria-label={`Cell ${i + 1} value`} value={String(c.value)} onChange={e => update(i, { value: typeof c.value === "number" ? Number(e.target.value) : e.target.value })} /></label>}
      <label>Format<select aria-label={`Cell ${i + 1} format`} value={book.formats.find(f => f.sheet === c.sheet && f.cell === c.cell)?.preset ?? "plain"} onChange={e => onChange({ ...book, formats: [...book.formats.filter(f => f.sheet !== c.sheet || f.cell !== c.cell), { sheet: c.sheet, cell: c.cell, preset: e.target.value as SpreadsheetSpec["formats"][number]["preset"] }] })}>{["plain", "integer", "decimal", "percent", "currency", "header"].map(f => <option key={f}>{f}</option>)}</select></label>
      <button type="button" onClick={() => onChange({ ...book, cells: book.cells.filter(v => v.sheet !== c.sheet || v.cell !== c.cell), formulas: book.formulas.filter(v => v.sheet !== c.sheet || v.cell !== c.cell), formats: book.formats.filter(v => v.sheet !== c.sheet || v.cell !== c.cell) })}>Remove cell {i + 1}</button>
    </fieldset>)}
    <button type="button" disabled={entries.length >= 16384} onClick={addCell}>Add cell</button>
    <p>Literal text beginning =, +, - or @ stays text. Only Formula cells execute the fixed arithmetic/SUM/AVERAGE/MIN/MAX/COUNT/IF grammar with same-workbook references. No macros, DDE or external references. Validation identifies exact failing cells.</p>
  </fieldset>;
}
