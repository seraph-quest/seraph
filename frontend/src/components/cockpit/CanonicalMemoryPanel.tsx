import { useCallback, useEffect, useRef, useState } from "react";

import {
  fetchMemoryRecord,
  fetchMemoryRecords,
  MEMORY_KINDS,
  MEMORY_STATUSES,
  MemoryRecordsApiError,
  postMemoryControl,
  postMemoryCorrection,
  postMemoryDeleteExport,
  safeMemoryHref,
} from "../../lib/memoryRecords";
import type { CanonicalMemoryKind, CanonicalMemoryLink, CanonicalMemoryRecord, CanonicalMemoryStatus } from "../../types";

export interface CanonicalMemoryPanelProps {
  active?: boolean;
  onOpenTask?: (taskId: string) => void;
  onInspectLink?: (link: CanonicalMemoryLink) => void;
  onOpenMemoryControls?: () => void;
  onOpenCapabilities?: () => void;
}

function formatTime(value: string | null | undefined): string {
  if (!value) return "unknown";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export function CanonicalMemoryPanel({ active = true, onOpenTask, onInspectLink, onOpenMemoryControls, onOpenCapabilities }: CanonicalMemoryPanelProps) {
  const [records, setRecords] = useState<CanonicalMemoryRecord[]>([]);
  const [selected, setSelected] = useState<CanonicalMemoryRecord | null>(null);
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState<CanonicalMemoryKind | "">("");
  const [status, setStatus] = useState<CanonicalMemoryStatus | "">("");
  const [cursor, setCursor] = useState<string | null>(null);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [lastConfirmedAt, setLastConfirmedAt] = useState<string | null>(null);
  const [loading, setLoading] = useState(active);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [controlMessage, setControlMessage] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [correctionText, setCorrectionText] = useState("");
  const [forgetMode, setForgetMode] = useState<"archive" | "redact">("archive");
  const [deleteExportAcknowledged, setDeleteExportAcknowledged] = useState(false);
  const controllerRef = useRef<AbortController | null>(null);
  const detailControllerRef = useRef<AbortController | null>(null);
  const detailRequestRef = useRef(0);

  const load = useCallback(async (nextCursorValue: string | null = null, append = false) => {
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setLoading(true);
    try {
      const result = await fetchMemoryRecords({ limit: 20, cursor: nextCursorValue, query, kind, status, signal: controller.signal });
      if (controller.signal.aborted || controllerRef.current !== controller) return;
      setRecords((current) => append ? [...current, ...result.records] : result.records);
      setCursor(nextCursorValue);
      setNextCursor(result.next_cursor);
      const recordConfirmation = result.records
        .map((record) => record.last_confirmed_at)
        .filter((value): value is string => Boolean(value))
        .sort();
      const lastRecordConfirmation = recordConfirmation.length > 0
        ? recordConfirmation[recordConfirmation.length - 1]
        : null;
      setLastConfirmedAt(result.last_confirmed_at ?? lastRecordConfirmation);
      setError(null);
    } catch (cause) {
      if (
        (cause instanceof DOMException && cause.name === "AbortError")
        || controller.signal.aborted
        || controllerRef.current !== controller
      ) return;
      if (controller.signal.aborted || controllerRef.current !== controller) return;
      const apiError = cause instanceof MemoryRecordsApiError ? cause : null;
      setError(apiError?.status === 403 || apiError?.status === 404
        ? "This operator session cannot access that memory record."
        : `Memory records unavailable; showing last confirmed data. ${apiError?.message ?? "retry the read"}`);
    } finally {
      if (!controller.signal.aborted && controllerRef.current === controller) setLoading(false);
    }
  }, [kind, query, status]);

  useEffect(() => {
    if (!active) {
      controllerRef.current?.abort();
      detailControllerRef.current?.abort();
      setLoading(false);
      return;
    }
    void load();
    return () => {
      controllerRef.current?.abort();
      detailControllerRef.current?.abort();
    };
  }, [active, load]);

  const openRecord = async (record: CanonicalMemoryRecord) => {
    detailControllerRef.current?.abort();
    const controller = new AbortController();
    detailControllerRef.current = controller;
    const requestId = ++detailRequestRef.current;
    setSelected(record);
    setDetailLoading(true);
    setControlMessage(null);
    try {
      const detail = await fetchMemoryRecord(record.id, controller.signal);
      if (!controller.signal.aborted && requestId === detailRequestRef.current) setSelected(detail);
    } catch (cause) {
      if (
        (cause instanceof DOMException && cause.name === "AbortError")
        || controller.signal.aborted
        || requestId !== detailRequestRef.current
        || detailControllerRef.current !== controller
      ) return;
      const apiError = cause instanceof MemoryRecordsApiError ? cause : null;
      setError(apiError?.status === 404 ? "Memory detail is unavailable for this operator session." : apiError?.message ?? "Memory detail unavailable.");
    } finally {
      if (!controller.signal.aborted && requestId === detailRequestRef.current) setDetailLoading(false);
    }
  };

  const runControl = async (action: "pin" | "forget" | "audit") => {
    if (!selected || !reason.trim()) {
      setControlMessage("A reason is required before changing canonical memory.");
      return;
    }
    setControlMessage(`Applying ${action}…`);
    try {
      await fetchMemoryRecord(selected.id);
      await postMemoryControl(selected.id, action, {
        reason: reason.trim().slice(0, 500),
        privacy_boundary: selected.privacy_boundary ?? undefined,
        ...(action === "forget" ? { mode: forgetMode } : {}),
      });
      setControlMessage(`${action} receipt recorded.`);
      await load(cursor);
      const refreshed = await fetchMemoryRecord(selected.id).catch(() => null);
      setSelected(refreshed);
    } catch (cause) {
      setControlMessage(cause instanceof Error ? cause.message : `${action} failed.`);
    }
  };

  const correct = async () => {
    if (!selected || !reason.trim() || !correctionText.trim()) {
      setControlMessage("A correction and reason are required.");
      return;
    }
    setControlMessage("Applying correction…");
    try {
      const current = await fetchMemoryRecord(selected.id);
      await postMemoryCorrection({
        content: correctionText.trim().slice(0, 2_000),
        kind: current.kind,
        summary: correctionText.trim().slice(0, 240),
        corrects_memory_id: current.id,
        reason: reason.trim().slice(0, 500),
        confidence: current.confidence ?? undefined,
        privacy_boundary: current.privacy_boundary ?? undefined,
      });
      setControlMessage("Correction receipt recorded; refresh to inspect the superseding record.");
      setCorrectionText("");
      await load(cursor);
    } catch (cause) {
      setControlMessage(cause instanceof Error ? cause.message : "Correction failed.");
    }
  };

  const deleteExport = async () => {
    if (!selected || !reason.trim()) {
      setControlMessage("A reason is required before deleting canonical memory.");
      return;
    }
    if (!deleteExportAcknowledged) {
      setControlMessage("Confirm the delete/export boundary before continuing.");
      return;
    }
    const targetId = selected.id;
    setControlMessage("Applying acknowledged delete/export…");
    try {
      const current = await fetchMemoryRecord(targetId);
      await postMemoryDeleteExport({
        memory_id: current.id,
        reason: reason.trim().slice(0, 500),
        privacy_boundary: current.privacy_boundary ?? undefined,
      });
      // A successful terminal delete must be read back through the same
      // owner-bound detail route. The canonical API intentionally returns
      // 404 after tombstoning, which is the confirmation that this surface
      // may clear its selected record.
      const readback = await fetchMemoryRecord(targetId).catch((cause) => {
        if (cause instanceof MemoryRecordsApiError && cause.status === 404) return null;
        throw cause;
      });
      setDeleteExportAcknowledged(false);
      setSelected(readback);
      setControlMessage(readback ? "Delete/export receipt recorded; the record remains visible with its terminal state." : "Delete/export receipt recorded; the record is no longer available.");
      await load(cursor);
    } catch (cause) {
      if (cause instanceof MemoryRecordsApiError && cause.status === 404) {
        setDeleteExportAcknowledged(false);
        setSelected(null);
        setControlMessage("Delete/export receipt recorded; the record is no longer available.");
        await load(cursor);
        return;
      }
      setControlMessage(cause instanceof Error ? cause.message : "Delete/export failed.");
    }
  };

  const provenanceReferences = selected
    ? Object.entries(selected.safe_provenance).filter(([key, value]) => (
      ["proposal_id", "source_task_id", "source_attempt_id", "goal_id", "artifact_ref", "readback_ref", "evidence_digest", "artifact_digest", "readback_digest", "typed_input_digest", "source_context_digest"].includes(key)
      && typeof value === "string"
    ))
    : [];

  return (
    <section className="cockpit-section-surface canonical-memory-panel" data-testid="canonical-memory-panel" aria-busy={loading}>
      <div className="cockpit-section-header">
        <div>
          <div className="cockpit-eyebrow">LIBRARY · MEMORY</div>
          <h2>Canonical memory</h2>
          <p>Owner-bound records and provenance. Existing task learning remains in Work Board review.</p>
        </div>
        {onOpenCapabilities ? <button type="button" onClick={onOpenCapabilities}>Capabilities and procedures</button> : null}
        <button type="button" onClick={() => void load()} disabled={loading}>{loading ? "Refreshing…" : "Refresh records"}</button>
      </div>
      <div className="canonical-memory-filters">
        <input aria-label="Search canonical memory" value={query} maxLength={200} onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void load(); }} placeholder="Search records" />
        <select aria-label="Memory kind" value={kind} onChange={(event) => { setKind(event.target.value as CanonicalMemoryKind | ""); void load(); }}>
          <option value="">All kinds</option>{MEMORY_KINDS.map((value) => <option key={value} value={value}>{value}</option>)}
        </select>
        <select aria-label="Memory status" value={status} onChange={(event) => { setStatus(event.target.value as CanonicalMemoryStatus | ""); void load(); }}>
          <option value="">Active (default)</option>{MEMORY_STATUSES.map((value) => <option key={value} value={value}>{value}</option>)}
        </select>
      </div>
      {error ? <div className="cockpit-section-notice" role="status">{error}</div> : null}
      {controlMessage ? <div className="cockpit-section-notice" role="status">{controlMessage}</div> : null}
      <div className="canonical-memory-layout">
        <div className="canonical-memory-list" aria-label="Canonical memory records">
          {loading && records.length === 0 ? (
            <div className="guardian-inbox-skeleton" data-testid="canonical-memory-loading" aria-label="Loading canonical memory">
              <div className="guardian-inbox-skeleton-row" aria-hidden="true" />
              <div className="guardian-inbox-skeleton-row" aria-hidden="true" />
            </div>
          ) : records.map((record) => (
            <button key={record.id} type="button" className={selected?.id === record.id ? "active" : ""} data-testid={`memory-record-${record.id}`} onClick={() => void openRecord(record)}>
              <strong>{record.summary ?? record.id}</strong>
              <span>{record.kind} · {record.status}</span>
              <small>updated {formatTime(record.updated_at)}</small>
            </button>
          ))}
          {!loading && records.length === 0 ? <p>No canonical records are confirmed for this session.</p> : null}
          {nextCursor ? <button type="button" onClick={() => void load(nextCursor, true)} disabled={loading}>Load more</button> : null}
        </div>
        <aside className="canonical-memory-detail" aria-label="Canonical memory detail" data-testid="memory-record-detail">
          {!selected ? <p>Select a memory record to inspect provenance and controls.</p> : (
            <>
              <h3>{selected.summary ?? selected.id}</h3>
              <div className="cockpit-chip-row"><span className="cockpit-chip">{selected.kind}</span><span className="cockpit-chip">{selected.status}</span></div>
              {detailLoading ? <p role="status">Loading verified memory detail…</p> : null}
              <p>{selected.content ?? "Content unavailable in metadata projection."}</p>
              <dl className="guardian-candidate-details">
                <div><dt>record</dt><dd>{selected.id}</dd></div>
                <div><dt>confidence</dt><dd>{selected.confidence ?? "unknown"}</dd></div>
                <div><dt>last confirmed</dt><dd>{formatTime(selected.last_confirmed_at)}</dd></div>
                <div><dt>provenance</dt><dd>{selected.safe_provenance.source_type ?? (Array.isArray(selected.safe_provenance.source_types) ? selected.safe_provenance.source_types.join(", ") : "unknown")}</dd></div>
                {selected.redaction_state ? <div><dt>redaction</dt><dd>{selected.redaction_state}</dd></div> : null}
                {selected.source_state ? <div><dt>source state</dt><dd>{typeof selected.source_state.count === "number" ? `${selected.source_state.count} source(s)` : "available"}{selected.source_state.verified === true ? " · verified" : ""}{selected.safe_provenance.sources_truncated === true ? " · bounded/truncated" : ""}</dd></div> : null}
                {selected.conflict_state ? <div><dt>conflict state</dt><dd>{selected.conflict_state.has_conflicts === true ? "conflict present" : "no conflict reported"}</dd></div> : null}
                {selected.tombstone_state ? <div><dt>tombstone</dt><dd>{selected.tombstone_state}</dd></div> : null}
                <div><dt>privacy boundary</dt><dd>{selected.privacy_boundary ?? "operator session"}</dd></div>
              </dl>
              {provenanceReferences.length > 0 ? (
                <dl className="guardian-candidate-details">
                  {provenanceReferences.map(([key, value]) => <div key={key}><dt>{key.replace(/_/g, " ")}</dt><dd>{String(value)}</dd></div>)}
                </dl>
              ) : null}
              {selected.links.map((link) => {
                const key = `${link.kind}:${link.id ?? link.href ?? "reference"}`;
                const label = link.label ?? link.kind;
                const href = safeMemoryHref(link.href);
                const owningTaskId = typeof selected.safe_provenance.source_task_id === "string"
                  ? selected.safe_provenance.source_task_id
                  : null;
                if (link.id && link.kind.includes("task") && onOpenTask) {
                  return <button key={key} type="button" onClick={() => onOpenTask(link.id as string)}>Open Work Board memory review · {label} {link.id}</button>;
                }
                if (link.id && link.kind.includes("readback") && owningTaskId && onOpenTask) {
                  return <button key={key} type="button" onClick={() => onOpenTask(owningTaskId)}>Open owning Work Board · {label} {link.id}</button>;
                }
                if (link.id && link.kind.includes("artifact") && owningTaskId && onOpenTask) {
                  return <button key={key} type="button" onClick={() => onOpenTask(owningTaskId)}>Open owning Work Board · {label} {link.id}</button>;
                }
                if (href) return <a key={key} href={href}>{label}</a>;
                if (link.id && (link.kind.includes("artifact") || link.kind.includes("readback")) && onInspectLink) {
                  return <button key={key} type="button" onClick={() => onInspectLink(link)}>{link.kind.includes("readback") ? "Open owning Work Board" : "Inspect"} {label} {link.id}</button>;
                }
                return <span key={key} className="cockpit-home-muted">{label}: {link.id ?? "reference unavailable in this surface"}</span>;
              })}
              {typeof selected.safe_provenance.source_task_id === "string" && onOpenTask ? (
                <button type="button" onClick={() => onOpenTask(selected.safe_provenance.source_task_id as string)}>Open Work Board memory review</button>
              ) : null}
              {selected.tombstone ? <p className="cockpit-section-notice">This record is tombstoned and cannot be revived from this panel.</p> : null}
              {selected.ownership_access === "recovered_read_only" && <p className="cockpit-section-notice">Recovered original · read only. Previous permissions stay blocked; create and review fresh current-scope intent separately.</p>}
              <fieldset className="canonical-memory-controls" disabled={selected.ownership_access === "recovered_read_only"}>
                <label>Reason<input aria-label="Memory control reason" value={reason} maxLength={500} onChange={(event) => setReason(event.target.value)} /></label>
                <div className="source-watch-actions">
                  <button type="button" onClick={() => void runControl("pin")}>Pin</button>
                  <button type="button" onClick={() => void runControl("audit")}>Audit</button>
                  <select aria-label="Forget mode" value={forgetMode} onChange={(event) => setForgetMode(event.target.value as "archive" | "redact")}><option value="archive">Archive</option><option value="redact">Redact</option></select>
                  <button type="button" onClick={() => void runControl("forget")}>Forget</button>
                </div>
                <label>Correction text<textarea aria-label="Correction text" value={correctionText} maxLength={2_000} onChange={(event) => setCorrectionText(event.target.value)} /></label>
                <p className="cockpit-home-muted">This is an ordinary canonical correction. Signed task comparison stays in the existing Work Board memory review.</p>
                <button type="button" onClick={() => void correct()}>Save correction</button>
                <label className="canonical-memory-danger-ack">
                  <input
                    type="checkbox"
                    checked={deleteExportAcknowledged}
                    onChange={(event) => setDeleteExportAcknowledged(event.target.checked)}
                  />
                  I understand this permanently tombstones the owner-bound record and records a delete/export receipt.
                </label>
                <button type="button" onClick={() => void deleteExport()}>Delete/export record</button>
                {onOpenMemoryControls ? <button type="button" onClick={onOpenMemoryControls}>Open acknowledged memory controls</button> : null}
              </fieldset>
            </>
          )}
        </aside>
      </div>
      <div className="cockpit-home-footer">last confirmed · {lastConfirmedAt ?? "pending"}</div>
    </section>
  );
}
