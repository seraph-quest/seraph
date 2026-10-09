import { useCallback, useEffect, useRef, useState } from "react";
import { FirstResultSetup } from "./FirstResultSetup";
import { moveAttentionFocus } from "./AttentionList";
import "./CockpitHome.css";
import type { AttentionItem, AttentionOwner } from "../../lib/cockpitAttention";
import { fetchHomeContinuation, homeSections, HomeContinuationError, type HomeContinuation, type HomeItem, type HomeSectionKey, type HomeTarget } from "../../lib/homeContinuation";

export interface CockpitHomeProps {
  active?: boolean;
  authenticated?: boolean;
  onOpenSection: (section: "inbox" | "work" | "goals" | "library" | "connections") => void;
  onOpenApprovals?: (approvalId?: string) => void;
  onOpenTask?: (taskId: string) => void;
  onOpenAttention?: (item: AttentionItem) => void;
  onOpenContinuation?: (target: HomeTarget, item: HomeItem, focusId: string) => void;
  owner?: AttentionOwner | null;
  focusAttentionId?: string | null;
  goalSummary?: { goalId: string; goalRevision: number; ownerSessionId: string; title?: string | null; status?: string | null; criterion?: string | null } | null;
}
const labels: Record<HomeSectionKey, string> = { active_goals: "Active goals", programme_status: "Programme progress", task_next_actions: "Next steps", prepared_outputs: "Prepared results", approvals: "Pending approvals", blocked_items: "Recovery and Unknown work" };
export function continuationItemId(item: HomeItem): string {
  if (item.kind === "inbox_decision") return `inbox_decision:${item.inbox_id}:${item.inbox_revision}`;
  return `${item.kind}:${"task_id" in item ? `${item.task_id}:${"attempt_id" in item ? item.attempt_id : item.task_revision}` : "approval_id" in item ? item.approval_id : "programme_id" in item ? `${item.goal_id}:${item.programme_id}` : item.goal_id}`;
}
function description(item: HomeItem) {
  switch (item.kind) {
    case "active_goal": return `${item.title ?? "Goal title unavailable"} · Goal ${item.goal_id} · ${item.status} · revision ${item.goal_revision}`;
    case "inbox_decision": return `${item.title} · ${item.state}`;
    case "programme": return `Programme ${item.programme_id} · ${item.state.replace(/_/g, " ")}`;
    case "task_next_action": return `Task ${item.task_id} · ${item.status} · ${item.action.replace(/_/g, " ")}`;
    case "prepared_output": return `Task ${item.task_id} · result ${item.output_state}`;
    case "approval": return `Approval ${item.approval_id} · pending`;
    case "blocked_task": return `Task ${item.task_id} · ${item.reason_code.replace(/_/g, " ")}`;
    case "blocked_approval": return `Approval ${item.approval_id} · expired`;
    case "blocked_programme": return `Programme ${item.programme_id} · ${item.reason_code.replace(/_/g, " ")}`;
  }
}
export function CockpitHome(props: CockpitHomeProps) {
  return <OwnedHome key={`${props.owner?.principalId ?? ""}:${props.owner?.sessionId ?? ""}`} {...props} />;
}
function OwnedHome({ onOpenSection, onOpenApprovals, onOpenTask, onOpenAttention, onOpenContinuation, owner, focusAttentionId, goalSummary, active = true, authenticated = true }: CockpitHomeProps) {
  const [snapshot, setSnapshot] = useState<HomeContinuation | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [restartRequired, setRestartRequired] = useState(false);
  const [setupOpen, setSetupOpen] = useState(false);
  const snapshotRef = useRef(snapshot); snapshotRef.current = snapshot;
  const initialized = useRef(false);
  const controller = useRef<AbortController | null>(null);
  const surface = useRef<HTMLElement>(null);
  const load = useCallback(async (nextCursor: string | null = null) => {
    controller.current?.abort();
    const active = new AbortController(); controller.current = active;
    setLoading(true);
    try {
      const result = await fetchHomeContinuation(active.signal, nextCursor);
      if (active.signal.aborted || controller.current !== active) return;
      if (nextCursor && snapshotRef.current && result.snapshot.as_of !== snapshotRef.current.as_of) throw new HomeContinuationError("continuation_stale", 409);
      // A confirmed blocked source is current truth, not a failed refresh.
      // In particular, absent stable identity must not freeze current Tasks.
      const partial = homeSections.some(key => result.snapshot[key].state === "degraded");
      if (!partial || !snapshotRef.current) { setSnapshot(result.snapshot); setCursor(result.nextCursor); }
      setError(partial ? "Some source metadata is unavailable. Existing confirmed metadata remains historical." : null); setRestartRequired(false);
    } catch (cause) {
      if (active.signal.aborted || controller.current !== active) return;
      setError(cause instanceof Error ? cause.message : "Home metadata unavailable.");
      setRestartRequired(cause instanceof HomeContinuationError && /continuation_(stale|expired|invalid)/.test(cause.code));
    } finally { if (!active.signal.aborted && controller.current === active) setLoading(false); }
  }, []);
  useEffect(() => {
    if (active && !authenticated) { setLoading(false); setError("Current operator ownership is unavailable. Sign in or restore the existing session."); return; }
    if (active && !initialized.current) { initialized.current = true; void load(); }
  }, [active, authenticated, load]);
  useEffect(() => () => controller.current?.abort(), []);
  useEffect(() => {
    if (!active || !focusAttentionId || !snapshot) return;
    const button = [...surface.current?.querySelectorAll<HTMLButtonElement>("[data-attention-id]") ?? []].find(entry => entry.dataset.attentionId === focusAttentionId);
    (button ?? surface.current)?.focus();
  }, [snapshot, focusAttentionId, active]);
  const open = (target: HomeTarget, item: HomeItem) => {
    const id = continuationItemId(item);
    if (onOpenContinuation) { onOpenContinuation(target, item, id); return; }
    if (target.kind === "inbox") {
      if (onOpenAttention) onOpenAttention({id, kind:"inbox", inboxId:target.inbox_id,title:description(item),reason:"Inspect the current Inbox decision",updatedAt:item.source_at,goalId:"goal_id" in item?item.goal_id:null,threadId:null,recoveryAction:null,readOnly:false,metadataConfirmed:!error,priority:4});
      else onOpenSection("inbox");
      return;
    }
    if (target.kind === "approval") { onOpenApprovals?.(target.approval_id); return; }
    if (target.kind === "task" || target.kind === "output") {
      if (onOpenAttention && owner) onOpenAttention({ id, kind: "task", taskId: target.task_id, title: description(item), reason: "Inspect current owning metadata", updatedAt: item.source_at, goalId: "goal_id" in item ? item.goal_id : null, threadId: null, recoveryAction: null, readOnly: item.ownership_access === "recovered_read_only", metadataConfirmed: !error, priority: 0 });
      else onOpenTask?.(target.task_id);
    }
  };
  const stale = Boolean(error && snapshot);
  const summaryMatches = !stale && goalSummary && owner && goalSummary.ownerSessionId===owner.sessionId
    && snapshot?.active_goals.items.some(item=>item.kind==="active_goal" && item.ownership_access==="current"
      && item.goal_id===goalSummary.goalId && item.goal_revision===goalSummary.goalRevision);
  const displaySummary = (value?:string|null) => value && value.length<=2048 ? value : "Unavailable";
  return <section ref={surface} tabIndex={-1} onKeyDown={moveAttentionFocus} className="cockpit-section-surface cockpit-home" data-testid="cockpit-home" aria-busy={loading}>
    <div className="cockpit-section-header"><div><div className="cockpit-eyebrow">HOME</div><h2>Continue your work</h2><p>Goals, next steps, prepared results and recovery in one local snapshot.</p></div>
      <button type="button" disabled={loading || !authenticated} onClick={() => void load()}>{loading ? "Refreshing…" : restartRequired ? "Restart Home snapshot" : "Refresh Home"}</button></div>
    <p>Snapshot · {snapshot?.as_of ?? "unavailable"}{stale ? " · last confirmed, refresh failed" : ""}. Inspectors recheck current ownership and permissions.</p>
    {error ? <p role="status">{snapshot ? "Showing last confirmed metadata" : "Home metadata unavailable"} · {error}{restartRequired ? " · Restart to obtain a new snapshot. The original continuation was not renewed." : ""}</p> : null}
    <nav aria-label="Available cockpit controls"><button onClick={() => onOpenSection("inbox")}>Open Inbox</button><button onClick={() => onOpenSection("work")}>Open Work</button><button onClick={() => onOpenSection("goals")}>Open Goals</button><button onClick={() => onOpenSection("library")}>Open Library</button><button onClick={() => onOpenSection("connections")}>Open Connections</button></nav>
    {summaryMatches ? <article aria-label="Selected Goal context"><h3>{displaySummary(goalSummary.title)}</h3><p>Status · {displaySummary(goalSummary.status)}</p><p>Criterion · {displaySummary(goalSummary.criterion)}</p><p>Current Goal context; inspect the Goal before execution.</p></article> : goalSummary ? <p>Selected Goal context unavailable for this page or current revision.</p> : null}
    <div className="cockpit-home-grid">{(["task_next_actions", "approvals", "blocked_items", "active_goals", "programme_status", "prepared_outputs"] as const).map(key => {
      const section = snapshot?.[key];
      return <article key={key} className="cockpit-home-card" aria-label={labels[key]}><h3>{key==="task_next_actions"?"Needs attention and next steps":labels[key]}</h3>
        <p className="cockpit-home-muted">{section?.state === "ready" ? "Metadata confirmed" : section?.state === "empty" ? "Empty on this page" : section ? `Metadata ${section.state}` : "Metadata unavailable"}{stale ? " · last confirmed" : ""} · source {section?.source_as_of ?? "unavailable"}</p>
        {section?.items.map(item => <div className="cockpit-home-continuation-item" key={continuationItemId(item)}>
          <button type="button" data-attention-id={continuationItemId(item)} onClick={() => open(item.target, item)}><strong>{description(item)}</strong><span> · Inspect {item.target.kind}</span></button>
          <p>Source · {item.source_at} · {item.ownership_access === "recovered_read_only" ? "Recovered history · read only" : "Current operator metadata"}</p>
          {item.kind === "prepared_output" ? <p>Attempt {item.attempt_id}. Physical output availability is checked by the owning inspector.</p> : null}
          {item.kind === "programme" ? <p>Next passive digest · {item.next_digest_at ?? "unavailable"}. This timing does not promise execution.</p> : null}
          {item.kind === "approval" ? <p>Expires · {item.expires_at ?? "unavailable"}</p> : null}
          {item.kind === "inbox_decision" ? <p>Decision revision {item.inbox_revision} · source metadata {item.source_availability} · snoozed until {item.snoozed_until ?? "not snoozed"} · expires {item.expires_at}. The Inbox inspector rechecks current evidence and allowed actions.</p> : null}
          {item.kind === "task_next_action" ? <p>Priority {item.priority} · scheduled {item.scheduled_at ?? "unscheduled"}</p> : null}
          {"method" in item ? <div aria-label="Historical method metadata"><p>{item.method?.status === "admitted" ? `Originally admitted method ${item.method.method_id} · version ${item.method.version} · ${item.method.lifecycle.replace(/_/g, " ")} · admitted ${item.method.admitted_at}` : item.method?.status === "baseline" ? "Originally admitted configured baseline" : "Historical method Unknown"}. This metadata does not establish execution readiness or measured improvement.</p>
            {item.method?.target ? <button type="button" onClick={() => open(item.method!.target!, item)}>Inspect original method and rollback controls</button> : null}</div> : null}
        </div>)}
        {!section ? <p>Metadata unavailable. Existing cockpit controls remain available.</p> : section.items.length === 0 ? <p>{section.state === "empty" ? "No items on this page." : "Source metadata unavailable; absence of rows does not confirm empty work."}</p> : null}
      </article>;
    })}</div>
    {cursor ? <button type="button" disabled={loading || restartRequired || Boolean(error)} onClick={() => void load(cursor)}>Next Home page</button> : null}
    <p>At most 20 metadata items per page. Continuation keeps its original creation cutoff and five-minute or operator-session expiry. Each page rechecks current metadata; existing decisions may change. Refresh to include newly created work.</p>
    <button type="button" onClick={() => setSetupOpen(value => !value)}>{setupOpen ? "Hide first-result setup" : "Open first-result setup"}</button>
    {setupOpen ? <FirstResultSetup onOpenSection={onOpenSection} onOpenTask={onOpenTask} /> : null}
  </section>;
}
