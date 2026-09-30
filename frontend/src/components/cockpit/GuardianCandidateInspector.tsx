import { useEffect, useRef } from "react";

import type {
  GuardianInboxAction,
  GuardianInboxEvidencePreview,
  GuardianInboxEvidenceRef,
  GuardianInboxItem,
} from "../../types";

export interface GuardianCandidateInspectorProps {
  item: GuardianInboxItem | null;
  onClose?: () => void;
  onOpenTask?: (taskId: string) => void;
  onInspectArtifact?: (reference: GuardianInboxEvidenceRef, preview?: GuardianInboxEvidencePreview) => void;
  onAction?: (action: GuardianInboxAction) => void;
  actionsEnabled?: boolean;
  actionDisabledReason?: string | null;
}

function formatTime(value: string | null | undefined): string {
  if (!value) return "unknown";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function snoozeReason(item: GuardianInboxItem): string {
  const history = item.action_history?.find((entry) => entry.action === "snooze");
  if (history?.reason_state === "provided" && history.safe_reason) return history.safe_reason;
  if (history?.reason_state === "not_provided") return "reason not provided";
  return "reason unavailable";
}

function safeHref(value: string | null | undefined): string | null {
  if (!value || value.startsWith("//")) return null;
  try {
    const parsed = new URL(value, window.location.origin);
    if (parsed.origin !== window.location.origin) return null;
    const allowed = ["/api/artifacts/", "/api/capabilities/source-watches/", "/api/work-board/tasks/", "/cockpit"];
    return allowed.some((prefix) => parsed.pathname.startsWith(prefix))
      ? `${parsed.pathname}${parsed.search}${parsed.hash}`
      : null;
  } catch {
    return null;
  }
}

export function GuardianCandidateInspector({
  item,
  onClose,
  onOpenTask,
  onInspectArtifact,
  onAction,
  actionsEnabled = false,
  actionDisabledReason = null,
}: GuardianCandidateInspectorProps) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (item) headingRef.current?.focus();
  }, [item?.id]);
  useEffect(() => {
    if (!item || !onClose) return;
    const handler = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [item, onClose]);

  if (!item) {
    return (
      <aside className="guardian-candidate-inspector" data-testid="guardian-candidate-inspector" aria-label="Guardian candidate inspector">
        <h2 ref={headingRef} tabIndex={-1}>Guardian candidate</h2>
        <p>Select a candidate to inspect its verified evidence and decision boundary.</p>
      </aside>
    );
  }

  const supported = new Set(item.allowed_actions);
  return (
    <aside className="guardian-candidate-inspector" data-testid="guardian-candidate-inspector" aria-labelledby="guardian-candidate-heading">
      <div className="guardian-candidate-inspector-header">
        <div>
          <div className="cockpit-eyebrow">INBOX INSPECTOR</div>
          <h2 id="guardian-candidate-heading" ref={headingRef} tabIndex={-1}>{item.title}</h2>
        </div>
        {onClose ? <button type="button" aria-label="Close candidate inspector" onClick={onClose}>Close</button> : null}
      </div>
      <div className="cockpit-chip-row">
        <span className="cockpit-chip">{item.state}</span>
        <span className="cockpit-chip">revision {item.revision}</span>
        {item.degraded ? <span className="cockpit-chip">degraded</span> : null}
      </div>
      <p>{item.summary}</p>
      {item.state === "snoozed" ? <p className="cockpit-section-notice">Snoozed until {formatTime(item.snoozed_until)} · {snoozeReason(item)}.</p> : null}
      <p className="cockpit-home-muted">Why now: {item.why_now}</p>
      <dl className="guardian-candidate-details">
        <div><dt>goal</dt><dd>{item.goal_id} · revision {item.goal_revision}</dd></div>
        <div><dt>source watch</dt><dd>{item.watch_id} · plan {item.plan_revision}</dd></div>
        <div><dt>source</dt><dd>{item.source_status ?? "unknown"} · {item.source_freshness ?? "unknown"}</dd></div>
        <div><dt>evidence</dt><dd>{item.evidence_status ?? item.verification_status ?? "unknown"}</dd></div>
        <div><dt>expires</dt><dd>{formatTime(item.expires_at)}</dd></div>
      </dl>
      <p className="cockpit-section-notice">Policy boundary: {item.policy_reason ?? "unavailable in this inbox projection"} · authority / budget boundary</p>
      {item.recovery_action ? <p className="cockpit-section-notice">Recovery: {item.recovery_action}</p> : null}
      <section aria-label="Verified evidence">
        <h3>Verified evidence</h3>
        {item.evidence_refs.length === 0 ? <p>No evidence metadata was returned.</p> : item.evidence_refs.map((reference, index) => {
          const preview = item.evidence_previews?.find((candidate) => candidate.artifact_id === reference.artifact_id);
          const label = reference.label ?? reference.artifact_id ?? reference.file_path ?? `evidence ${index + 1}`;
          return (
            <div key={`${item.id}:inspector-evidence:${index}`} className="guardian-candidate-evidence">
              {onInspectArtifact && (reference.artifact_id || reference.file_path) ? (
                <button type="button" onClick={() => onInspectArtifact(reference, preview)}>Open evidence {label}</button>
              ) : safeHref(reference.artifact_url) ? (
                <a href={safeHref(reference.artifact_url) as string}>{label}</a>
              ) : <span>{label}</span>}
              <span>{reference.status ?? "unverified"} · {reference.verification ?? "metadata"}</span>
            </div>
          );
        })}
      </section>
      {item.job ? (
        <section aria-label="Durable job receipt">
          <h3>Durable job receipt</h3>
          <p>{item.job.id ?? "unknown"} · {item.job.status ?? "unknown"} · attempts {item.job.attempt_count ?? "?"}/{item.job.max_attempts ?? "?"}</p>
          {item.job.readbacks?.map((readback, index) => (
            <div key={`${item.id}:inspector-readback:${readback.readback_id ?? index}`} className="guardian-candidate-readback">
              {readback.target_path ?? "readback"} · {readback.readback_id ?? "unknown"} · {readback.status ?? "unknown"}
            </div>
          ))}
        </section>
      ) : <p>Durable job receipt unavailable.</p>}
      {item.action_history?.length || item.action_history_truncated ? (
        <section aria-label="Decision history">
          <h3>Decision history</h3>
          {(item.action_history ?? []).map((entry) => (
            <p key={`${item.id}:history:${entry.receipt_id}`}>
              {entry.action} · {entry.outcome} · receipt {entry.receipt_id || "unavailable"}
              {entry.created_at ? ` · ${formatTime(entry.created_at)}` : ""}
              {entry.reason_state === "provided" && entry.safe_reason ? ` · reason: ${entry.safe_reason}` : ` · reason ${entry.reason_state}`}
              {entry.task_id && onOpenTask ? <> · <button type="button" onClick={() => onOpenTask(entry.task_id as string)}>Open task {entry.task_id}</button></> : null}
            </p>
          ))}
          {item.action_history_truncated ? <p>Older decision history is not shown.</p> : null}
        </section>
      ) : null}
      <section className="guardian-candidate-actions" aria-label="Candidate actions">
        {onAction && actionsEnabled && supported.has("accept_followup") ? <button type="button" onClick={() => onAction("accept_followup")}>Accept follow-up</button> : null}
        {onAction && actionsEnabled && supported.has("snooze") ? <button type="button" onClick={() => onAction("snooze")}>Snooze</button> : null}
        {onAction && actionsEnabled && supported.has("dismiss") ? <button type="button" onClick={() => onAction("dismiss")}>Dismiss</button> : null}
        {onAction && !actionsEnabled ? <span className="cockpit-home-muted">{actionDisabledReason ?? "Decision controls remain in the inbox list until current evidence is confirmed."}</span> : null}
        {item.task_id ? (
          <button type="button" onClick={() => onOpenTask?.(item.task_id as string)}>Open accepted task</button>
        ) : null}
        {safeHref(item.watch_url) ? <a href={safeHref(item.watch_url) as string}>Open source watch</a> : null}
      </section>
    </aside>
  );
}
