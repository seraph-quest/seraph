import { useCallback, useEffect, useRef, useState } from "react";
import { createGuardianUuid } from "../../lib/guardianInbox";
import { programmeApi } from "../quest/goalProgrammeApi";
import { isProgrammeDigestSnapshot, programmeDigestRequest, type ProgrammeDigestSnapshot, type ProgrammeFinding, type ProgrammeStatus } from "./programmeDigestApi";

interface Props {
  ownerKey: string;
  summaryOnly?: boolean;
  active?: boolean;
  onOpenGoals?: () => void;
  onOpenInbox?: () => void;
  onOpenTask?: (id: string) => void;
}
const time = (value: string | null) => value ? new Date(value).toLocaleString() : "No actual run recorded";

export function ProgrammeDigestContent(props: Props) {
  return <ProgrammeDigestOwned key={props.ownerKey} {...props} />;
}

function ProgrammeDigestOwned({ ownerKey, summaryOnly = false, active = true, onOpenGoals, onOpenInbox, onOpenTask }: Props) {
  const [snapshot, setSnapshot] = useState<ProgrammeDigestSnapshot | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [outcomes, setOutcomes] = useState<Record<string, string>>({});
  const [dates, setDates] = useState<Record<string, string>>({});
  const [categories, setCategories] = useState("");
  const [taskLinks, setTaskLinks] = useState<Record<string, string>>({});
  const [readbacks, setReadbacks] = useState<Record<string, string>>({});
  const controller = useRef<AbortController | null>(null);
  const mounted = useRef(false);
  const lock = useRef(false);
  const uncertain = useRef(new Set<string>());
  const load = useCallback(async () => {
    controller.current?.abort();
    const request = new AbortController(); controller.current = request;
    setBusy(true); setConfirmed(false); setError("");
    try {
      const result = await programmeDigestRequest<unknown>("programme-digests", undefined, request.signal);
      if (!isProgrammeDigestSnapshot(result)) throw new Error("Programme readback incomplete. Review Goals before acting.");
      if (!mounted.current || request.signal.aborted) return;
      setSnapshot(result); setConfirmed(true); setCategories(result.notifications.deadline_categories.join(", "));
    } catch (cause) {
      if (mounted.current && !request.signal.aborted) setError(cause instanceof Error ? cause.message : "Programme readback unavailable.");
    } finally { if (mounted.current && !request.signal.aborted) setBusy(false); }
  }, []);
  useEffect(() => {
    mounted.current = true;
    if (ownerKey && active) void load();
    return () => { mounted.current = false; controller.current?.abort(); };
  }, [ownerKey, active, load]);

  const action = async (finding: ProgrammeFinding, kind: "accept_followup" | "snooze" | "dismiss") => {
    if (lock.current || !confirmed || !ownerKey || uncertain.current.has(finding.id)) return;
    if (kind === "accept_followup" && (!finding.actionable || finding.source_freshness !== "current" || !outcomes[finding.id]?.trim())) return;
    if (kind === "snooze" && (!Number.isFinite(Date.parse(dates[finding.id])) || Date.parse(dates[finding.id]) <= Date.now()
      || Date.parse(dates[finding.id]) > Date.now() + 30 * 86400000)) return;
    const until = kind === "snooze" ? new Date(dates[finding.id]).toISOString() : undefined;
    lock.current = true; setBusy(true); setError("");
    // Never replay an uncertain mutation. Durable server state is inspected by Refresh.
    uncertain.current.add(finding.id);
    try {
      const result = await programmeDigestRequest<{ finding_id: string; task_id?: string }>(`programme-findings/${encodeURIComponent(finding.id)}/actions`, {
        action: kind, desired_outcome: outcomes[finding.id]?.trim() || undefined, until, idempotency_key: createGuardianUuid(),
      });
      if (!mounted.current) return;
      if (result.finding_id !== finding.id) throw new Error("Finding action binding mismatch. Refresh before continuing.");
      if (result.task_id) setTaskLinks((current) => ({ ...current, [finding.id]: result.task_id! }));
      uncertain.current.delete(finding.id);
      await load();
    } catch (cause) { if (mounted.current) { setConfirmed(false); setError(`${cause instanceof Error ? cause.message : "Action outcome uncertain."} Refresh to inspect retained state; no automatic retry.`); } }
    finally { lock.current = false; if (mounted.current) setBusy(false); }
  };
  const pause = async (programme: ProgrammeStatus) => {
    if (lock.current || !confirmed) return;
    lock.current = true; setBusy(true); setError("");
    try {
      await programmeApi(programme.goal_id, `/${encodeURIComponent(programme.id)}/pause`, { expected_grant_revision: programme.grant_revision, recover_owner_acknowledged: false });
      if (mounted.current) await load();
    } catch (cause) { if (mounted.current) { setConfirmed(false); setError(cause instanceof Error ? cause.message : "Pause unavailable. Review owner recovery in Goals."); } }
    finally { lock.current = false; if (mounted.current) setBusy(false); }
  };
  const readBrief = async (finding: ProgrammeFinding) => {
    if (lock.current || !confirmed) return;
    lock.current = true; setBusy(true); setError("");
    try {
      const result = await programmeApi<{ job_id: string; programme_id: string; physical_readback: true; no_learning: true; brief: unknown; prepared_artifacts: unknown[] }>(finding.goal_id,
        `/${encodeURIComponent(finding.programme_id)}/discovery/${encodeURIComponent(finding.job_id)}/brief`);
      if (result.job_id !== finding.job_id || result.programme_id !== finding.programme_id || result.physical_readback !== true
        || result.no_learning !== true || !result.brief || !Array.isArray(result.prepared_artifacts)) throw new Error("Discovery readback binding mismatch. Review Goals.");
      if (mounted.current) setReadbacks((current) => ({ ...current, [finding.id]: JSON.stringify({ brief: result.brief, prepared_outputs: result.prepared_artifacts }, null, 2) }));
    } catch (cause) { if (mounted.current) { setReadbacks((current) => { const next = { ...current }; delete next[finding.id]; return next; }); setError(cause instanceof Error ? cause.message : "Current source readback unavailable."); } }
    finally { lock.current = false; if (mounted.current) setBusy(false); }
  };
  const notifications = async (enabled: boolean) => {
    if (lock.current || !confirmed) return;
    lock.current = true; setBusy(true); setError("");
    try {
      await programmeDigestRequest("programme-notifications", { enabled, deadline_categories: categories.split(",").map((c) => c.trim()).filter(Boolean) });
      if (mounted.current) await load();
    } catch (cause) { if (mounted.current) { setConfirmed(false); setError(cause instanceof Error ? cause.message : "Notification setting outcome uncertain. Refresh."); } }
    finally { lock.current = false; if (mounted.current) setBusy(false); }
  };

  return <div aria-label={summaryOnly ? "Programme status" : "Daily programme digest"} aria-busy={busy}>
    <h3>{summaryOnly ? "Public programmes" : "Daily programme digest"}</h3>
    {error ? <p role="status">{snapshot ? "Last confirmed programme data · " : ""}{error}</p> : null}
    {!ownerKey ? <p>Programme owner unavailable. Review Goals.</p> : <button type="button" disabled={busy} onClick={() => void load()}>Refresh programmes</button>}
    {!snapshot ? <p>{busy ? "Loading programme receipts…" : "Programme receipts unavailable."}</p> : <>
      {snapshot.programmes.length === 0 ? <p>No reviewed public programmes.</p> : snapshot.programmes.map((p) => <div className="cockpit-outcome-note" key={p.id}>
        <strong>{p.state}</strong>{p.reason_code ? ` · ${p.reason_code}` : ""}
        <div>last actual run · {time(p.last_run)} · sources checked {p.sources_checked}</div>
        <div>output · {p.output ?? "No output recorded"}</div>
        {p.next_digest_at ? <div>Next digest · {time(p.next_digest_at)}</div> : null}
        {p.next_run ? <div>Next source run · {time(p.next_run)}</div> : null}
        <div>remaining finite allowance · {p.remaining_allowance_microusd === null ? "Unavailable" : `${p.remaining_allowance_microusd} microUSD`}</div>
        {p.recovery ? <div>Passive recovery · {p.recovery}</div> : null}
        <button type="button" disabled={busy || !confirmed || p.state !== "active"} onClick={() => void pause(p)}>Pause programme</button>
        {onOpenGoals ? <button type="button" onClick={onOpenGoals}>Review programme in Goals</button> : null}
      </div>)}
      {summaryOnly ? (onOpenInbox ? <button type="button" onClick={onOpenInbox}>Open daily digest in Inbox</button> : null) : <>
        <p>Delivery · {snapshot.notifications.enabled ? "Notifications opted in" : "Inbox only"} · digest slots {snapshot.notifications.digest_slots_remaining}/1 · deadline slots {snapshot.notifications.deadline_slots_remaining}/1 · {snapshot.notifications.quiet_hours_active ? "Quiet hours active" : "Outside quiet hours"}</p>
        {snapshot.notifications.delivery_debt ? <p>Delivery debt · notification outcome Unknown; the slot remains consumed. No automatic replay.</p> : null}
        <label>Relevant deadline categories <input value={categories} onChange={(e) => setCategories(e.target.value)} disabled={busy || !confirmed} placeholder="Comma-separated operator categories" /></label>
        <button type="button" disabled={busy || !confirmed} onClick={() => void notifications(!snapshot.notifications.enabled)}>{snapshot.notifications.enabled ? "Use Inbox only" : "Opt in to bounded notifications"}</button>
        <p>At most one digest and one cited relevant deadline notification per local day. Quiet hours remain authoritative; model urgency cannot bypass limits.</p>
        {!snapshot.digests.length ? <p>No actual daily digest recorded.</p> : snapshot.digests.map((entry) => <div key={entry.id}>
          <h4>{entry.digest.local_date} · {entry.digest.timezone}</h4>
          {entry.digest.blocked_reasons.map((reason) => <p key={reason}>Blocked · {reason}. Review Goals; no repeated model calls.</p>)}
          {entry.findings.map((f) => <article className="source-watch-record" key={f.id} aria-label="Programme finding">
            <p>{f.text}</p>
            <div>Source freshness · {f.source_freshness}</div>
            {f.citations.map((c, index) => <div key={`${c.source_id}:${index}`}>source {c.source_id} · lines {c.first_line}–{c.last_line}</div>)}
            <button type="button" disabled={busy || !confirmed} onClick={() => void readBrief(f)}>Read discovery brief and prepared outputs</button>
            {readbacks[f.id] ? <pre aria-label="Current discovery source and output readback">{readbacks[f.id]}</pre> : null}
            {f.recovery ? <p>Recovery · {f.recovery}</p> : null}
            {f.follow_through ? <p>Follow-through · {f.follow_through.status} · {f.follow_through.desired_outcome} · planned follow-up {f.follow_through.due_at ? time(f.follow_through.due_at) : "None"}</p> : null}
            {f.prepared_outputs.map((output) => <div key={output.artifact_id}>Output · {output.artifact_id} · digest {output.digest}</div>)}
            {(taskLinks[f.id] || f.task_id) && onOpenTask ? <button type="button" onClick={() => onOpenTask(taskLinks[f.id] || f.task_id!)}>{f.follow_through?.status === "completed" ? "Open completed task and output" : "Review prepared proposal in Work"}</button> : null}
            <label>Desired outcome <input value={outcomes[f.id] ?? ""} onChange={(e) => setOutcomes((current) => ({ ...current, [f.id]: e.target.value }))} maxLength={500} disabled={busy || !confirmed} /></label>
            <button type="button" disabled={busy || !confirmed || !f.actionable || f.source_freshness !== "current" || uncertain.current.has(f.id) || !outcomes[f.id]?.trim()} onClick={() => void action(f, "accept_followup")}>Prepare next step for review</button>
            <p>Preparation is inert. Review and accept the existing C1 task proposal in Work before execution.</p>
            <label>Planned follow-up <input type="datetime-local" value={dates[f.id] ?? ""} onChange={(e) => setDates((current) => ({ ...current, [f.id]: e.target.value }))} disabled={busy || !confirmed} /></label>
            <button type="button" disabled={busy || !confirmed || uncertain.current.has(f.id) || !dates[f.id] || !Number.isFinite(Date.parse(dates[f.id])) || Date.parse(dates[f.id]) <= Date.now() || Date.parse(dates[f.id]) > Date.now() + 30 * 86400000} onClick={() => void action(f, "snooze")}>Defer finding</button>
            <button type="button" disabled={busy || !confirmed || uncertain.current.has(f.id)} onClick={() => void action(f, "dismiss")}>Dismiss finding</button>
          </article>)}
        </div>)}
      </>}
    </>}
  </div>;
}
