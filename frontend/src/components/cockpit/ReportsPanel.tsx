import { useCallback, useEffect, useRef, useState } from "react";

import { listEndOfDayReports, readEndOfDayReport, type ReportDetail, type ReportListItem } from "../../lib/reports";

// Read-only view of stored end-of-day reports. It only reads reports the
// scheduled end-of-day job already stored; it cannot generate a report or
// start any model inference.

export interface ReportsPanelProps {
  active?: boolean;
  authenticated?: boolean;
  pageSize?: number;
}

const formatMinutes = (minutes: number) => (minutes >= 60 ? `${Math.floor(minutes / 60)}h ${minutes % 60}m` : `${minutes}m`);
const formatSeconds = (seconds: number) => formatMinutes(Math.round(seconds / 60));
const statusLabel = (item: ReportListItem) => (item.status === "blocked" ? `Blocked · ${item.blocked_reason ?? "unspecified"}` : "Ready");

function MixList({ label, entries }: { label: string; entries: Array<[string, number]> }) {
  return (
    <div className="cockpit-reports-mix">
      <h4>{label}</h4>
      {entries.length ? (
        <ul aria-label={label}>
          {entries.map(([key, seconds]) => (
            <li key={key}>
              {key} · {formatSeconds(seconds)}
            </li>
          ))}
        </ul>
      ) : (
        <p className="cockpit-home-muted">None recorded</p>
      )}
    </div>
  );
}

function ReportBody({ detail }: { detail: ReportDetail }) {
  const { summary } = detail;
  return (
    <article aria-label={`Report for ${detail.date ?? "unknown date"}`} data-testid="report-detail">
      <h3>
        {detail.date ?? "Unknown date"} · {detail.timezone ?? "timezone unknown"}
      </h3>
      <p>
        Analysis · {detail.analysis_provider ?? "unrecorded"} · Stored {detail.created_at ?? "unknown"}
      </p>
      {detail.status === "blocked" ? (
        <div role="status" className="cockpit-reports-blocked" data-testid="report-blocked-notice">
          <strong>Report analysis was blocked ({detail.blocked_reason ?? "unspecified"}).</strong>
          <p>This stored entry is a blocked notice, not a report. No analysis output was used.</p>
          <pre className="whitespace-pre-wrap break-words">{detail.body}</pre>
        </div>
      ) : (
        <pre className="whitespace-pre-wrap break-words" data-testid="report-body">
          {detail.body}
        </pre>
      )}
      <p>
        Goals · {detail.active_goal_count} active · {detail.completed_goal_count} completed that day
      </p>
      <p>
        Tracked {formatMinutes(summary.total_tracked_minutes)} · {summary.total_observations} observations ·{" "}
        {summary.switch_count === null ? "switch count unavailable" : `${summary.switch_count} switches`}
      </p>
      {summary.source === "episode_metadata" ? (
        <p className="cockpit-home-muted">Activity breakdown unavailable: the report archive could not be verified.</p>
      ) : (
        <div className="cockpit-reports-mixes">
          <MixList label="Activity mix" entries={summary.by_activity} />
          <MixList label="Project mix" entries={summary.by_project} />
          <MixList label="Source mix" entries={summary.by_source} />
        </div>
      )}
    </article>
  );
}

export function ReportsPanel({ active = true, authenticated = true, pageSize = 30 }: ReportsPanelProps) {
  const [items, setItems] = useState<ReportListItem[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ReportDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const listController = useRef<AbortController | null>(null);
  const detailController = useRef<AbortController | null>(null);
  const initialized = useRef(false);

  const load = useCallback(async () => {
    listController.current?.abort();
    const controller = new AbortController();
    listController.current = controller;
    setLoading(true);
    try {
      const next = await listEndOfDayReports(pageSize, controller.signal);
      if (controller.signal.aborted || listController.current !== controller) return;
      setItems(next);
      setError(null);
    } catch (cause) {
      if (controller.signal.aborted || listController.current !== controller) return;
      setError(cause instanceof Error ? cause.message : "Stored reports could not be read.");
    } finally {
      if (!controller.signal.aborted && listController.current === controller) setLoading(false);
    }
  }, [pageSize]);

  const select = useCallback(async (id: string) => {
    detailController.current?.abort();
    const controller = new AbortController();
    detailController.current = controller;
    setSelectedId(id);
    setDetail(null);
    setDetailError(null);
    setDetailLoading(true);
    try {
      const next = await readEndOfDayReport(id, controller.signal);
      if (controller.signal.aborted || detailController.current !== controller) return;
      setDetail(next);
    } catch (cause) {
      if (controller.signal.aborted || detailController.current !== controller) return;
      setDetailError(cause instanceof Error ? cause.message : "The stored report could not be read.");
    } finally {
      if (!controller.signal.aborted && detailController.current === controller) setDetailLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!active || !authenticated) return;
    if (!initialized.current) {
      initialized.current = true;
      void load();
    }
  }, [active, authenticated, load]);

  useEffect(
    () => () => {
      listController.current?.abort();
      detailController.current?.abort();
    },
    [],
  );

  if (!authenticated) {
    return (
      <section className="cockpit-section-surface" data-testid="cockpit-reports-section">
        <div className="cockpit-section-header">
          <div>
            <div className="cockpit-eyebrow">REPORTS</div>
            <h2>End-of-day reports</h2>
          </div>
        </div>
        <p role="status">Current operator ownership is unavailable. Sign in or restore the existing session.</p>
      </section>
    );
  }

  return (
    <section className="cockpit-section-surface cockpit-reports" data-testid="cockpit-reports-section" aria-busy={loading}>
      <div className="cockpit-section-header">
        <div>
          <div className="cockpit-eyebrow">REPORTS</div>
          <h2>End-of-day reports</h2>
          <p>Read stored end-of-day activity and goal reports. This view only reads stored reports.</p>
        </div>
        <button type="button" disabled={loading} onClick={() => void load()}>
          {loading ? "Refreshing…" : "Refresh reports"}
        </button>
      </div>
      {error ? (
        <p role="alert">
          {items ? "Showing the last loaded list · " : ""}
          {error}
        </p>
      ) : null}
      {items === null && loading ? <p role="status">Loading stored reports…</p> : null}
      {items && items.length === 0 ? (
        <p role="status" data-testid="reports-empty">
          No stored reports yet. Reports are produced by the scheduled end-of-day job and appear here once stored.
        </p>
      ) : null}
      {items && items.length ? (
        <div className="cockpit-reports-layout">
          <ul aria-label="Stored reports" className="cockpit-reports-list">
            {items.map((item) => (
              <li key={item.episode_id}>
                <button
                  type="button"
                  aria-current={selectedId === item.episode_id ? "true" : undefined}
                  data-testid={`report-row-${item.episode_id}`}
                  onClick={() => void select(item.episode_id)}
                >
                  <strong>{item.date ?? "Unknown date"}</strong> · {statusLabel(item)} · {item.analysis_provider ?? "unrecorded"}
                </button>
              </li>
            ))}
          </ul>
          <div className="cockpit-reports-detail">
            {detailLoading ? <p role="status">Loading report…</p> : null}
            {detailError ? <p role="alert">{detailError}</p> : null}
            {detail ? <ReportBody detail={detail} /> : null}
            {!selectedId ? <p className="cockpit-home-muted">Select a report to read it.</p> : null}
          </div>
        </div>
      ) : null}
    </section>
  );
}
