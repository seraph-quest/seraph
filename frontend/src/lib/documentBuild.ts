import { API_URL } from "../config/constants";
import { apiFetch } from "./api";
import type { DocumentTaskBinding } from "./generalTask";

export type CellValue = string | number | boolean | null;
export interface Section { heading: string; paragraphs: string[]; citation_refs: string[] }
export interface ReportTable { title: string; columns: string[]; rows: CellValue[][]; citation_refs: string[] }
export interface SpreadsheetSpec {
  sheet_names: string[]; cells: { sheet: string; cell: string; value: CellValue }[];
  formulas: { sheet: string; cell: string; expression: string }[];
  formats: { sheet: string; cell: string; preset: "plain" | "integer" | "decimal" | "percent" | "currency" | "header" }[];
}
export interface DocumentBuildSpec {
  kind: "report" | "brief" | "table_workbook"; title: string; sections: Section[];
  tables: (ReportTable | SpreadsheetSpec)[]; citations: { source_ref: string; label: string }[];
  style_preset: "plain" | "compact";
}
export interface DocumentBuildTaskBinding {
  build_ref: string; build_revision: number; spec_digest: string; selection_digest: string;
  source_binding: DocumentTaskBinding | null; original_deadline: string;
}
export interface BuildProjection {
  build_id: string; build_ref: string; revision: number;
  state: "reserved" | "staged" | "bound" | "rendering" | "reaping" | "unknown" | "completed" | "degraded" | "cleanup_tombstone" | "deleted";
  goal_id: string; goal_revision: number; spec_digest: string; selection_digest: string;
  task_id: string | null; original_deadline: string; reason_code: string | null;
  quota_reserved_bytes: number; no_learning: true; provider_contacts: 0;
}
export interface BuildReviewBinding {
  schema: "document-build-review.v1"; owner_principal_id: string; owner_session_id: string;
  root_authority: string; root_token_digest: string; goal_id: string; goal_revision: number;
  build_id: string; build_revision: number; generation: 1; spec_digest: string; selection_digest: string;
  source_binding_digest: string; descriptor_digest: string; policy_digest: string;
  renderer_profile: "document-build-renderer.v1"; renderer_profile_digest: string; limits_digest: string;
  formats: ("docx" | "xlsx" | "pdf")[]; original_deadline: string;
  task_id: string | null; task_revision: number | null; plan_revision: number | null; expires_at: string;
}
export interface BuildReview { binding: BuildReviewBinding; mac: string }
export interface BuildPreview extends BuildProjection {
  spec: DocumentBuildSpec; selection: CitationLeaf[]; review: BuildReview;
  limits: { spec_bytes: 65536; editable_bytes: 4194304; pdf_bytes: 4194304 }; formats: ("docx" | "xlsx" | "pdf")[];
}
export interface CitationLeaf { source_ref: string; text?: string; formula?: string | null; cached_value?: CellValue }
export interface BuildArtifact { artifact_ref: string; sha256: string; size_bytes: number; media_type: string }
export interface BuildOutputs extends BuildProjection {
  output: { editable_artifact: BuildArtifact; pdf_artifact: BuildArtifact | null; source_refs: string[]; warnings: string[] };
}
export class DocumentBuildError extends Error {
  constructor(message: string, public status: number) { super(message); }
}
export const MEDIA = { docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document", xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", pdf: "application/pdf" };
const obj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
const digest = (v: unknown): v is string => typeof v === "string" && /^[a-f0-9]{64}$/.test(v);
const integer = (v: unknown): v is number => typeof v === "number" && Number.isSafeInteger(v) && v >= 1;
const date = (v: unknown): v is string => typeof v === "string" && v.length <= 40 && Number.isFinite(Date.parse(v));
export function validBuildBinding(v: unknown): v is DocumentBuildTaskBinding {
  const source = obj(v) ? v.source_binding : undefined;
  const validSource = source === null || obj(source) && Object.keys(source).length === 6 && typeof source.artifact_ref === "string" && /^document-source:[0-9a-f-]{36}$/.test(source.artifact_ref)
    && integer(source.source_revision) && digest(source.metadata_digest) && digest(source.selection_digest) && source.acknowledge_local_use === true
    && Array.isArray(source.citation_refs) && source.citation_refs.length >= 1 && source.citation_refs.length <= 16 && new Set(source.citation_refs).size === source.citation_refs.length && source.citation_refs.every(r => typeof r === "string" && r.length > 0 && r.length <= 512);
  return obj(v) && Object.keys(v).length === 6 && typeof v.build_ref === "string" && /^document-build:[0-9a-f-]{36}$/.test(v.build_ref)
    && integer(v.build_revision) && digest(v.spec_digest) && digest(v.selection_digest) && date(v.original_deadline)
    && validSource;
}
export function parseBuildProjection(v: unknown): BuildProjection {
  if (!obj(v) || typeof v.build_id !== "string" || !/^[0-9a-f-]{36}$/.test(v.build_id) || v.build_ref !== `document-build:${v.build_id}`
    || !integer(v.revision) || !integer(v.goal_revision) || typeof v.goal_id !== "string" || !v.goal_id || v.goal_id.length > 128
    || !["reserved", "staged", "bound", "rendering", "reaping", "unknown", "completed", "degraded", "cleanup_tombstone", "deleted"].includes(String(v.state))
    || !digest(v.spec_digest) || !digest(v.selection_digest) || !date(v.original_deadline)
    || !(v.task_id === null || typeof v.task_id === "string" && v.task_id.length <= 128)
    || !(v.reason_code === null || typeof v.reason_code === "string" && /^[a-z0-9_]{1,128}$/.test(v.reason_code))
    || typeof v.quota_reserved_bytes !== "number" || !Number.isSafeInteger(v.quota_reserved_bytes) || v.quota_reserved_bytes < 0 || v.quota_reserved_bytes > 24 * 1024 * 1024
    || v.no_learning !== true || v.provider_contacts !== 0) throw Error("Document build readback is unconfirmed. Refresh under the original owner.");
  return v as unknown as BuildProjection;
}
export function validateBuildSpec(spec: DocumentBuildSpec): void {
  const bytes = (s: string) => new TextEncoder().encode(s).length;
  const text = (s: string, max: number) => { if (typeof s !== "string" || bytes(s) > max || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\ud800-\udfff\ufffe\uffff]/u.test(s)) throw Error("Document text contains unsupported characters or exceeds its bound."); };
  const literal = (c: CellValue) => { if (typeof c === "string") text(c, 4096); else if (typeof c === "number") { if (!Number.isFinite(c) || Math.abs(c) > 1e100) throw Error("Document numbers must be finite."); } else if (c !== null && typeof c !== "boolean") throw Error("Document cells require literal text, a number, a boolean or blank."); };
  if (Object.keys(spec).sort().join(",") !== "citations,kind,sections,style_preset,tables,title" || !["report", "brief", "table_workbook"].includes(spec.kind) || !["plain", "compact"].includes(spec.style_preset) || !spec.title.trim() || spec.title.length > 200 || spec.sections.length > 32 || spec.tables.length > 16 || spec.citations.length > 16) throw Error("Choose a title and keep sections, tables and citations within their finite bounds.");
  text(spec.title, 800);
  spec.citations.forEach(c => { if (Object.keys(c).sort().join(",") !== "label,source_ref" || !c.source_ref || c.source_ref.length > 512 || !c.label.trim() || c.label.length > 200) throw Error("Each citation requires a bounded source and label."); text(c.source_ref, 2048); text(c.label, 800); });
  const refs = new Set(spec.citations.map(c => c.source_ref));
  if (refs.size !== spec.citations.length) throw Error("Each citation must be unique.");
  let paragraphs = 0;
  for (const s of spec.sections) { if (Object.keys(s).sort().join(",") !== "citation_refs,heading,paragraphs" || s.heading.length > 200 || s.citation_refs.length > 16) throw Error("Section heading or citation bound exceeded."); text(s.heading, 800); paragraphs += s.paragraphs.length; s.paragraphs.forEach(p => text(p, 4096)); if (s.citation_refs.some(r => !refs.has(r))) throw Error("Select the section's exact source citations."); }
  if (paragraphs > 128) throw Error("A document may contain at most 128 paragraphs.");
  for (const t of spec.tables) {
    if ("columns" in t) {
      if (Object.keys(t).sort().join(",") !== "citation_refs,columns,rows,title" || t.title.length > 200 || t.citation_refs.length > 16 || t.citation_refs.some(r => !refs.has(r))) throw Error("Table title or citations are invalid.");
      text(t.title, 800);
      if (!t.columns.length || t.columns.length > 64 || t.rows.length > 256 || t.rows.some(r => r.length !== t.columns.length)) throw Error("Tables require 1–64 columns and at most 256 rectangular rows.");
      t.columns.forEach(c => text(c, 4096)); t.rows.flat().forEach(literal);
    } else {
      if (Object.keys(t).sort().join(",") !== "cells,formats,formulas,sheet_names" || t.formats.length > 16384) throw Error("Workbook format bound exceeded.");
      if (!t.sheet_names.length || t.sheet_names.length > 8 || new Set(t.sheet_names.map(s => s.toLowerCase())).size !== t.sheet_names.length || t.sheet_names.some(s => !/^[A-Za-z_][A-Za-z0-9_]{0,30}$/.test(s) || s.toLowerCase() === "overview")) throw Error("Use 1–8 unique sheet names containing letters, numbers and underscores; Overview is reserved.");
      if (t.cells.length + t.formulas.length > 16384 || t.formulas.length > 2048) throw Error("Workbook cell or formula allowance exceeded.");
      const seen = new Set<string>();
      for (const c of [...t.cells, ...t.formulas]) {
        if (Object.keys(c).sort().join(",") !== ("expression" in c ? "cell,expression,sheet" : "cell,sheet,value")) throw Error("Workbook cells and formulas must use their separate closed fields.");
        const m = /^\$?([A-Z]{1,2})\$?([1-9][0-9]{0,2})$/.exec(c.cell);
        const col = m ? [...m[1]].reduce((n, ch) => n * 26 + ch.charCodeAt(0) - 64, 0) : 0;
        const key = `${c.sheet}:${c.cell.replace(/\$/g, "")}`;
        if (!m || col > 64 || +m[2] > 256 || !t.sheet_names.includes(c.sheet) || seen.has(key)) throw Error(`Invalid or duplicate cell ${c.sheet}!${c.cell}. Use A1–BL256 on a declared sheet.`);
        seen.add(key);
        if ("expression" in c) { if (!c.expression || bytes(c.expression) > 512) throw Error(`Formula ${key} requires 1–512 UTF-8 bytes.`); }
        else literal(c.value);
      }
      const formatSeen = new Set<string>();
      for (const f of t.formats) { const key = `${f.sheet}:${f.cell.replace(/\$/g, "")}`; if (!seen.has(key) || formatSeen.has(key) || !["plain", "integer", "decimal", "percent", "currency", "header"].includes(f.preset)) throw Error(`Invalid format for ${key}.`); formatSeen.add(key); }
    }
  }
  if (spec.kind === "table_workbook" && (spec.tables.length !== 1 || !("sheet_names" in spec.tables[0]))) throw Error("A workbook requires one typed workbook specification.");
  if (bytes(JSON.stringify(spec)) > 65536) throw Error("Document specification exceeds 65536 UTF-8 bytes.");
}
export function parseBuildPreview(v: unknown): BuildPreview {
  const p = parseBuildProjection(v);
  if (!obj(v) || !obj(v.spec) || !obj(v.review) || !obj(v.review.binding) || !digest(v.review.mac) || !obj(v.limits)
    || v.limits.spec_bytes !== 65536 || v.limits.editable_bytes !== 4194304 || v.limits.pdf_bytes !== 4194304 || !Array.isArray(v.selection) || v.selection.length > 16 || !Array.isArray(v.formats)) throw Error("Private document preview is unconfirmed.");
  const b = v.review.binding;
  if (Object.keys(b).length !== 24 || b.schema !== "document-build-review.v1" || b.renderer_profile !== "document-build-renderer.v1" || b.generation !== 1
    || b.build_id !== p.build_id || b.build_revision !== p.revision || b.goal_id !== p.goal_id || b.goal_revision !== p.goal_revision || b.spec_digest !== p.spec_digest || b.selection_digest !== p.selection_digest || b.task_id !== p.task_id || b.original_deadline !== p.original_deadline
    || !date(b.expires_at) || !["root_authority", "root_token_digest", "source_binding_digest", "descriptor_digest", "policy_digest", "renderer_profile_digest", "limits_digest"].every(k => digest(b[k]))
    || typeof b.owner_principal_id !== "string" || typeof b.owner_session_id !== "string"
    || !(b.task_revision === null || integer(b.task_revision)) || !(b.plan_revision === null || integer(b.plan_revision))
    || !Array.isArray(b.formats) || b.formats.length !== 2 || !["docx", "xlsx"].includes(String(b.formats[0])) || b.formats[1] !== "pdf" || JSON.stringify(v.formats) !== JSON.stringify(b.formats)) throw Error("Signed document preview does not match the current build.");
  try { validateBuildSpec(v.spec as unknown as DocumentBuildSpec); } catch { throw Error("Private specification readback is invalid."); }
  if (!v.selection.every(c => obj(c) && typeof c.source_ref === "string" && c.source_ref.length <= 512 && typeof c.text === "string" && new TextEncoder().encode(c.text).length <= 16384)) throw Error("Private citation readback exceeded its bound.");
  const spec = v.spec as unknown as DocumentBuildSpec;
  if (b.formats[0] !== (spec.kind === "table_workbook" ? "xlsx" : "docx") || spec.citations.some(c => !(v.selection as CitationLeaf[]).some(s => s.source_ref === c.source_ref))
    || (b.task_id === null ? b.task_revision !== null || b.plan_revision !== null : !integer(b.task_revision) || !integer(b.plan_revision))) throw Error("Private preview formats, selected citations or Task binding changed.");
  return v as unknown as BuildPreview;
}
export function parseBuildOutputs(v: unknown): BuildOutputs {
  const p = parseBuildProjection(v);
  if (!obj(v) || !obj(v.output)) throw Error("Verified outputs are unavailable.");
  const artifact = (a: unknown, slot: string) => obj(a) && a.artifact_ref === `${p.build_ref}:${slot}` && digest(a.sha256) && typeof a.size_bytes === "number" && Number.isSafeInteger(a.size_bytes) && a.size_bytes > 0 && a.size_bytes <= 4194304 && Object.values(MEDIA).includes(a.media_type as string);
  if (!artifact(v.output.editable_artifact, "editable") || (v.output.editable_artifact as Record<string, unknown>).media_type === MEDIA.pdf || !(v.output.pdf_artifact === null || artifact(v.output.pdf_artifact, "pdf") && (v.output.pdf_artifact as Record<string, unknown>).media_type === MEDIA.pdf) || !Array.isArray(v.output.source_refs) || v.output.source_refs.length > 16 || !v.output.source_refs.every(r => typeof r === "string" && r.length <= 512) || !Array.isArray(v.output.warnings) || v.output.warnings.length > 16 || !v.output.warnings.every(r => typeof r === "string" && [...r].length <= 200)) throw Error("Output receipt is unconfirmed. Inspect the original task.");
  return v as unknown as BuildOutputs;
}
export async function documentRequest(path: string, body?: unknown, method = "POST", signal?: AbortSignal): Promise<unknown> {
  const r = await apiFetch(`${API_URL}/api/documents${path}`, { signal, ...(body === undefined ? {} : { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }) });
  if (!r.ok) {
    let message = `Document service blocked (${r.status}). Refresh and review under the original owner.`;
    try { const v = await r.json(); const d = v?.detail; if (obj(d) && typeof d.code === "string") message = `${d.code}: ${typeof d.recovery === "string" ? d.recovery : "Refresh the current private review."}${Array.isArray(d.errors) ? " " + d.errors.slice(0, 8).map(e => `${e.field?.join(".")}${typeof e.sheet === "string" && /^[A-Za-z_][A-Za-z0-9_]{0,30}$/.test(e.sheet) && typeof e.cell === "string" && /^[A-Z]{1,2}[1-9][0-9]{0,2}$/.test(e.cell) ? ` (${e.sheet}!${e.cell})` : ""}: ${e.code}`).join("; ") : ""}`; else if (Array.isArray(d)) message = d.slice(0, 8).map(e => `${e.loc?.join(".")}: ${e.msg}`).join("; "); } catch { /* Finite status recovery remains visible. */ }
    throw new DocumentBuildError(message, r.status);
  }
  return r.json();
}
export async function readBuildDownload(buildId: string, slot: "editable" | "pdf", expected: BuildArtifact, signal?: AbortSignal): Promise<Blob> {
  const r = await apiFetch(`${API_URL}/api/documents/builds/${encodeURIComponent(buildId)}/outputs/${slot}`, { signal });
  if (!r.ok || r.headers.get("content-type")?.split(";")[0] !== expected.media_type) throw Error("Authenticated download is unavailable or changed. Refresh verified outputs.");
  if (!r.body) throw Error("Output byte stream is unavailable.");
  const reader = r.body.getReader(); const chunks: Uint8Array[] = []; let count = 0;
  try {
    while (true) { const next = await reader.read(); if (next.done) break; count += next.value.byteLength; if (count > expected.size_bytes || count > 4194304) { await reader.cancel(); throw Error("Output size changed. Download withheld."); } chunks.push(next.value); }
  } finally { reader.releaseLock(); }
  const bytes = new Uint8Array(count); let offset = 0; for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  if (bytes.byteLength !== expected.size_bytes || bytes.byteLength > 4194304) throw Error("Output size changed. Download withheld.");
  const sha = [...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))].map(n => n.toString(16).padStart(2, "0")).join("");
  if (sha !== expected.sha256) throw Error("Output hash changed. Download withheld.");
  return new Blob([bytes.buffer], { type: expected.media_type });
}
