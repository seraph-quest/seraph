import { useCallback, useEffect, useRef, useState } from "react";

import {
  GITHUB_GRANT_SENTENCE,
  GITHUB_URL,
  listEndOfDayReports,
  readEndOfDayReport,
  type GitHubEvidence,
  type GitHubEvidenceItem,
  type GoalEvidence,
  type ReportDetail,
  type ReportListItem,
} from "../../lib/reports";

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

const KIND_LABELS: Record<string, string> = {
  commit: "commit",
  pr_opened: "PR opened",
  pr_merged: "PR merged",
  pr_closed: "PR closed",
  issue_opened: "issue opened",
  issue_closed: "issue closed",
};

// "[G3]" in the model's text becomes a link only when a stored item with a
// validated GitHub URL carries that ref; anything else stays plain text.
function LinkedBody({ body, items }: { body: string; items: GitHubEvidenceItem[] }) {
  const byRef = new Map(items.filter((item) => item.url && GITHUB_URL.test(item.url)).map((item) => [item.ref, item]));
  const parts = body.split(/(\[G\d{1,4}\])/);
  return (
    <>
      {parts.map((part, index) => {
        const item = byRef.get(part.slice(1, -1));
        return /^\[G\d{1,4}\]$/.test(part) && item?.url ? (
          <a key={index} href={item.url} target="_blank" rel="noreferrer noopener" title={`${item.repository} ${item.title}`}>
            {part}
          </a>
        ) : (
          <span key={index}>{part}</span>
        );
      })}
    </>
  );
}

function itemLabel(item: GitHubEvidenceItem) {
  const marker = item.number ? `#${item.number}` : (item.short_sha ?? "");
  const repeated = item.same_title_count > 1 ? ` (${item.same_title_count} commits with this title)` : "";
  return `${item.ref} · ${KIND_LABELS[item.kind] ?? item.kind} · ${item.repository} ${marker} · ${item.title}${repeated}`;
}

function GitHubEvidenceCard({ github }: { github: GitHubEvidence }) {
  return (
    <section aria-label="GitHub evidence" data-testid="report-github-evidence" className="cockpit-reports-github">
      <h4>GitHub evidence</h4>
      <p role="status" data-testid="report-github-status">
        {github.message ?? `GitHub source state: ${github.status}.`}
      </p>
      {github.status !== "not_connected" ? <p className="cockpit-home-muted">{GITHUB_GRANT_SENTENCE}</p> : null}
      {github.model_context === "not_allowed" ? (
        <p className="cockpit-home-muted">This activity was not shared with the model for analysis.</p>
      ) : null}
      {github.counts.length ? (
        <p>
          {github.counts.map(([kind, value]) => `${value} ${KIND_LABELS[kind] ?? kind}`).join(" · ")}
          {github.repositories.length ? ` · ${github.repositories.join(", ")}` : ""}
        </p>
      ) : null}
      {!github.items_available ? (
        <p className="cockpit-home-muted">
          {github.item_count} items recorded. The item list is unavailable: the report archive could not be verified.
        </p>
      ) : github.items.length ? (
        <ul aria-label="GitHub items">
          {github.items.map((item) => (
            <li key={item.ref}>
              {item.url ? (
                <a href={item.url} target="_blank" rel="noreferrer noopener">
                  {itemLabel(item)}
                </a>
              ) : (
                itemLabel(item)
              )}
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

function GoalEvidenceTable({ evidence }: { evidence: GoalEvidence }) {
  if (!evidence.goals.length || evidence.status === "not_requested") return null;
  const note =
    evidence.status === "blocked"
      ? "No model analysis ran, so goals were not compared with evidence."
      : evidence.status === "unparsed"
        ? "The analysis did not return a goal-to-evidence mapping."
        : null;
  return (
    <section aria-label="Goals and evidence" data-testid="report-goal-evidence">
      <h4>Goals and evidence</h4>
      {note ? <p className="cockpit-home-muted">{note}</p> : null}
      {evidence.status === "mapped" ? (
        <table>
          <thead>
            <tr>
              <th scope="col">Goal</th>
              <th scope="col">GitHub</th>
              <th scope="col">Screen</th>
            </tr>
          </thead>
          <tbody>
            {evidence.goals.map((goal) => (
              <tr key={goal.ref}>
                <td>
                  {goal.ref} · {goal.title}
                  {goal.level ? ` (${goal.level})` : ""}
                  {goal.parent_ref ? ` · under ${goal.parent_ref}` : ""}
                </td>
                <td>
                  {goal.github.length ? goal.github.join(", ") : "none"}
                  {goal.rolled_up_from.length ? ` · from ${goal.rolled_up_from.join(", ")}` : ""}
                </td>
                <td>{goal.screen ? "yes" : "no"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
      {evidence.status === "mapped" && evidence.goals_without_evidence.length ? (
        <p>Goals without evidence today: {evidence.goals_without_evidence.join(", ")}</p>
      ) : null}
      {evidence.status === "mapped" && evidence.github_without_goal.length ? (
        <p>GitHub records not tied to a goal: {evidence.github_without_goal.join(", ")}</p>
      ) : null}
    </section>
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
          <LinkedBody body={detail.body} items={detail.github_activity?.items ?? []} />
        </pre>
      )}
      {detail.github_activity ? <GitHubEvidenceCard github={detail.github_activity} /> : null}
      {detail.goal_evidence ? <GoalEvidenceTable evidence={detail.goal_evidence} /> : null}
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
