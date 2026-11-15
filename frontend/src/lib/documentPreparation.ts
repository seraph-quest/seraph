export const MAX_DOCUMENT_CITATIONS = 16;
export const MAX_PRIVATE_PREPARATION_BYTES = 16_384;

export interface DocumentEvidenceCell {
  source_ref: string;
  text: string;
  formula: string | null;
  cached_value: string | null;
}

export interface DocumentEvidenceSection {
  source_ref: string;
  text: string;
  table_cells: DocumentEvidenceCell[];
}

export interface DocumentEvidenceForSelection {
  sections: DocumentEvidenceSection[];
}

export interface DocumentCitation {
  source_ref: string;
  text: string;
  formula: string | null;
  cached_value: string | null;
}

export interface DocumentPreparationTask {
  task_id: string;
  status: string;
  [key: string]: unknown;
}

export interface DocumentPreparationView {
  task_id: string;
  status: string;
  sections: DocumentCitation[];
  no_learning: true;
  provider_contacts: 0;
}

const record = (value: unknown): value is Record<string, unknown> =>
  Boolean(value && typeof value === "object" && !Array.isArray(value));

function citationRef(value: unknown): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= 512 && !/[\u0000-\u001f\u007f]/.test(value);
}

function citations(value: unknown, field: string): string[] {
  if (!Array.isArray(value) || value.length === 0 || value.length > MAX_DOCUMENT_CITATIONS
    || value.some((item) => !citationRef(item))) {
    throw new Error(`${field} must contain one to ${MAX_DOCUMENT_CITATIONS} exact citation references.`);
  }
  const refs = value as string[];
  if (new Set(refs).size !== refs.length) throw new Error(`${field} cannot contain duplicate citation references.`);
  return refs;
}

/**
 * Flatten only selectable leaves. A table-bearing section is a container, so
 * its section reference is intentionally excluded; each table cell must be
 * selected explicitly to avoid disclosing unselected cells.
 */
export function citationLeaves(evidence: DocumentEvidenceForSelection): DocumentCitation[] {
  if (!record(evidence) || !Array.isArray(evidence.sections)) throw new Error("Document evidence has no selectable sections.");
  const leaves: DocumentCitation[] = [];
  const refs = new Set<string>();
  for (const section of evidence.sections) {
    if (!record(section) || !citationRef(section.source_ref) || typeof section.text !== "string" || !Array.isArray(section.table_cells)) {
      throw new Error("Document evidence contains an invalid citation source.");
    }
    if (section.table_cells.length === 0) {
      if (refs.has(section.source_ref)) throw new Error("Document evidence contains duplicate citation references.");
      refs.add(section.source_ref);
      leaves.push({ source_ref: section.source_ref, text: section.text, formula: null, cached_value: null });
      continue;
    }
    for (const cell of section.table_cells) {
      if (!record(cell) || !citationRef(cell.source_ref) || typeof cell.text !== "string"
        || !(cell.formula === null || typeof cell.formula === "string")
        || !(cell.cached_value === null || typeof cell.cached_value === "string")) {
        throw new Error("Document evidence contains an invalid table citation.");
      }
      if (refs.has(cell.source_ref)) throw new Error("Document evidence contains duplicate citation references.");
      refs.add(cell.source_ref);
      leaves.push({ source_ref: cell.source_ref, text: cell.text, formula: cell.formula, cached_value: cell.cached_value });
    }
  }
  return leaves;
}

export function serializeDocumentPreparation(input: {
  artifact_ref: string;
  expected_source_revision: number;
  citation_refs: string[];
  acknowledge_local_use: boolean;
  idempotency_key: string;
}): string {
  if (!/^document-source:[0-9a-f-]{36}$/.test(input.artifact_ref)) throw new Error("Preparation requires the canonical private document source.");
  if (!Number.isSafeInteger(input.expected_source_revision) || input.expected_source_revision < 1) throw new Error("Preparation requires the current source revision.");
  citations(input.citation_refs, "citation_refs");
  if (input.acknowledge_local_use !== true) throw new Error("Preparation requires explicit local-use acknowledgement.");
  if (!/^[A-Za-z0-9_.:-]{1,128}$/.test(input.idempotency_key)) throw new Error("Preparation requires a stable idempotency key.");
  return JSON.stringify({
    artifact_ref: input.artifact_ref,
    expected_source_revision: input.expected_source_revision,
    citation_refs: input.citation_refs,
    acknowledge_local_use: true,
    idempotency_key: input.idempotency_key,
  });
}

export function parseDocumentPreparationTask(value: unknown): DocumentPreparationTask {
  if (!record(value) || !record(value.task) || typeof value.task.task_id !== "string" || !value.task.task_id.trim()
    || value.task.task_id.length > 128 || typeof value.task.status !== "string" || !value.task.status.trim()) {
    throw new Error("Preparation task receipt is incomplete. Inspect Work before retrying.");
  }
  return value.task as DocumentPreparationTask;
}

function parseCitation(value: unknown): DocumentCitation {
  if (!record(value) || !citationRef(value.source_ref) || typeof value.text !== "string"
    || !(value.formula === undefined || value.formula === null || typeof value.formula === "string")
    || !(value.cached_value === undefined || value.cached_value === null || typeof value.cached_value === "string")) {
    throw new Error("Preparation readback contains an invalid cited span.");
  }
  return {
    source_ref: value.source_ref,
    text: value.text,
    formula: value.formula == null ? null : value.formula,
    cached_value: value.cached_value == null ? null : value.cached_value,
  };
}

export function parseDocumentPreparationView(value: unknown, selectedRefs: string[]): DocumentPreparationView {
  if (!record(value) || typeof value.task_id !== "string" || !value.task_id.trim() || typeof value.status !== "string"
    || value.no_learning !== true || value.provider_contacts !== 0 || !Array.isArray(value.sections)
    || value.sections.length > MAX_DOCUMENT_CITATIONS) {
    throw new Error("Preparation readback is incomplete or provider contact was not disproved.");
  }
  const sections = value.sections.map(parseCitation);
  const normalized = { task_id: value.task_id, status: value.status, sections, no_learning: true as const, provider_contacts: 0 as const };
  if (value.status !== "succeeded") {
    if (sections.length !== 0) throw new Error("Incomplete preparation readback disclosed citations before success.");
    return normalized;
  }
  const expected = citations(selectedRefs, "selected citation_refs");
  const refs = sections.map((section) => section.source_ref);
  if (new Set(refs).size !== refs.length || refs.length !== expected.length || refs.some((ref) => !expected.includes(ref))) {
    throw new Error("Preparation readback does not match the exact selected citations.");
  }
  if (new TextEncoder().encode(JSON.stringify(normalized)).byteLength > MAX_PRIVATE_PREPARATION_BYTES) {
    throw new Error(`Preparation readback exceeds the ${MAX_PRIVATE_PREPARATION_BYTES}-byte private view limit.`);
  }
  return normalized;
}
