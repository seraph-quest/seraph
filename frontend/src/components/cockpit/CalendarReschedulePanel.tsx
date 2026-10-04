import { useEffect, useRef, useState } from "react";
import * as exact from "../../lib/calendarRescheduleApi";
import type { RescheduleConsent, RescheduleJob, RescheduleProfile } from "../../lib/calendarRescheduleApi";

interface Props {
  ownerPrincipalId?: string | null; ownerSessionId?: string | null;
  eventBindingId: string; eventBindingRevision: number; goalId: string; goalRevision: number;
  goals: { id: string; title: string; revision?: number | null }[];
  onTaskCreated?: () => void;
}
interface Receipt { kind: RescheduleJob["kind"]; uuid: string; jobId?: string }
const button = "cockpit-feedback-button";
export function CalendarReschedulePanel(props: Props) {
  const { ownerPrincipalId, ownerSessionId, eventBindingId, eventBindingRevision, goalId, goalRevision, goals } = props;
  const key = ownerPrincipalId && ownerSessionId ? `seraph:calendar-reschedule:${encodeURIComponent(ownerPrincipalId)}:${encodeURIComponent(ownerSessionId)}:${encodeURIComponent(eventBindingId)}` : null;
  const [profiles, setProfiles] = useState<RescheduleProfile[]>([]);
  const [grants, setGrants] = useState<RescheduleConsent[]>([]);
  const [readId, setReadId] = useState(""); const [writeId, setWriteId] = useState("");
  const [verified, setVerified] = useState(false);
  const [ackIdentity, setAckIdentity] = useState(false);
  const [acks, setAcks] = useState([false, false, false]);
  const [ackPreview, setAckPreview] = useState(false); const [ackRecovery, setAckRecovery] = useState(false);
  const [start, setStart] = useState(""); const [end, setEnd] = useState(""); const [zone, setZone] = useState("");
  const [recoveryGoal, setRecoveryGoal] = useState("");
  const [job, setJob] = useState<RescheduleJob | null>(null);
  const [receipt, setReceipt] = useState<Receipt | null>(null);
  const [busy, setBusy] = useState(false); const [message, setMessage] = useState<string | null>(null);
  const [storageBlocked, setStorageBlocked] = useState(false);
  const generation = useRef(0); const controller = useRef<AbortController | null>(null);
  const read = profiles.find(p => p.connection_id === readId && p.state === "active");
  const write = profiles.find(p => p.connection_id === writeId && p.state === "active");
  const grant = grants.find(g => g.state === "active" && g.event_binding_id === eventBindingId);
  const permissionCurrent = grant && grant.goal_id === goalId && grant.goal_revision === goalRevision && Date.parse(grant.expires_at) > Date.now();
  useEffect(() => {
    const clearPrivate = () => setJob(current => current ? { ...current, preview: undefined } : current);
    window.addEventListener("blur", clearPrivate); document.addEventListener("visibilitychange", clearPrivate);
    const expiry = job?.preview?.expires_at;
    const timer = expiry ? setTimeout(clearPrivate, Math.max(0, expiry * 1000 - Date.now())) : undefined;
    return () => { window.removeEventListener("blur", clearPrivate); document.removeEventListener("visibilitychange", clearPrivate); if (timer !== undefined) clearTimeout(timer); };
  }, [job?.preview?.expires_at]);
  useEffect(() => {
    generation.current++; controller.current?.abort(); setBusy(false); setJob(null); setReceipt(null);
    setProfiles([]); setGrants([]); setReadId(""); setWriteId(""); setVerified(false); setAckIdentity(false);
    setAcks([false, false, false]); setAckPreview(false); setAckRecovery(false); setStart(""); setEnd(""); setZone(""); setMessage(null); setStorageBlocked(false);
    if (key) try {
      const saved = sessionStorage.getItem(key);
      if (saved) {
        const r = JSON.parse(saved) as Receipt;
        if (!/^[a-f0-9-]{36}$/.test(r.uuid) || !["calendar_reschedule_v1", "calendar_reschedule_identity_v1", "calendar_reschedule_observation_v1"].includes(r.kind)
          || (r.jobId !== undefined && (typeof r.jobId !== "string" || r.jobId.length > 256))) throw Error("Invalid receipt");
        setReceipt(r);
      }
    } catch { setStorageBlocked(true); }
    return () => { generation.current++; controller.current?.abort(); };
  }, [key, eventBindingRevision, goalId, goalRevision]);
  async function run(action: (signal: AbortSignal, version: number) => Promise<void>) {
    if (!key || busy || storageBlocked) return;
    const version = generation.current, abort = new AbortController(); controller.current = abort;
    const timer = setTimeout(() => abort.abort(), 125000); setBusy(true); setMessage(null);
    // Clear private content before every operation. It is shown again only by
    // the current private-read endpoint after its authority check.
    setJob(current => current ? { ...current, preview: undefined } : current);
    try { await action(abort.signal, version); }
    catch { if (version === generation.current) setMessage("The request is unconfirmed. Inspect the original local receipt; no conditional write is retried automatically."); }
    finally { clearTimeout(timer); if (version === generation.current) { setBusy(false); controller.current = null; } }
  }
  function retain(r: Receipt) {
    if (!key) throw Error("Missing Root"); sessionStorage.setItem(key, JSON.stringify(r));
    if (sessionStorage.getItem(key) !== JSON.stringify(r)) { setStorageBlocked(true); throw Error("Receipt unavailable"); }
    setReceipt(r);
  }
  async function accept(result: RescheduleJob, signal: AbortSignal, version: number) {
    if (generation.current !== version) return;
    retain({ kind: result.kind, uuid: result.request_uuid, jobId: result.job_id });
    if (result.kind === "calendar_reschedule_v1" && result.source_task_id && ownerPrincipalId && ownerSessionId) {
      sessionStorage.setItem(exact.rescheduleTaskReceiptKey(ownerPrincipalId, ownerSessionId, result.source_task_id), JSON.stringify({ jobId: result.job_id, uuid: result.request_uuid }));
    }
    // Responses from actions never authorize cached private content.
    setJob({ ...result, preview: undefined });
    if (result.kind === "calendar_reschedule_v1" && result.private_read_available) {
      const current = await exact.inspectPrivateReschedule(result.job_id, signal);
      if (generation.current === version) setJob(current);
    }
  }
  function pair() {
    if (!read || !write) throw Error("Choose separate profiles");
    return { read_connection_id: read.connection_id, expected_read_revision: read.revision,
      write_connection_id: write.connection_id, expected_write_revision: write.revision,
      event_binding_id: eventBindingId, expected_event_binding_revision: eventBindingRevision,
      goal_id: goalId, goal_revision: goalRevision, acknowledge_identity_and_selected_calendar_read: true };
  }
  async function refresh(signal: AbortSignal, version: number) {
    const [p, g] = await Promise.all([exact.listRescheduleProfiles(signal), exact.listRescheduleConsents(signal)]);
    if (generation.current === version) { setProfiles(p); setGrants(g); setVerified(false); }
  }
  return <section className="rounded border border-amber-500/30 p-3 sm:col-span-2" aria-label="Reschedule one owned calendar event">
    <h3 className="font-semibold">Reschedule this owned event</h3>
    <p className="text-xs">One timed, nonrecurring event without attendees, conference, attachments or unsupported fields. Separate exact scopes, fresh identity and ownership proof, finite permission, then an exact approval are required. No model or learning. Requested sendUpdates=none; provider reminders may still produce messages.</p>
    <button type="button" className={button} disabled={busy || !key || storageBlocked} onClick={() => void run(refresh)}>Inspect local reschedule profiles and permissions</button>
    <div className="grid gap-2 sm:grid-cols-2 mt-2">
      {(["read", "write"] as const).map(role => <label key={role}>Reschedule {role} profile<select className="cockpit-input w-full" value={role === "read" ? readId : writeId} disabled={busy || !!job} onChange={e => { (role === "read" ? setReadId : setWriteId)(e.target.value); setVerified(false); setAckIdentity(false); setAcks([false, false, false]); }}>
        <option value="">Choose separate profile from Settings</option>{profiles.filter(p => p.service === `calendar_reschedule_${role}`).map(p => <option key={p.connection_id} value={p.connection_id} disabled={p.state !== "active"}>{p.label} · {p.state} · revision {p.revision}</option>)}
      </select></label>)}
      <label className="sm:col-span-2"><input type="checkbox" checked={ackIdentity} disabled={busy} onChange={e => setAckIdentity(e.target.checked)} /> Read both account identities and the selected calendar-list metadata (six bounded contacts).</label>
      <button type="button" className={button} disabled={busy || !read || !write || !ackIdentity || !!job} onClick={() => void run(async (signal, version) => {
        const uuid = crypto.randomUUID(); retain({ kind: "calendar_reschedule_identity_v1", uuid });
        const result = await exact.verifyReschedulePair({ ...pair(), request_uuid: uuid }, signal);
        await accept(result, signal, version); if (generation.current === version) { setVerified(result.status === "succeeded"); setJob(null); }
      })}>Verify exact account and calendar ownership</button>
      {grant && <div role="status">Permission {grant.state} · expires {grant.expires_at}. {permissionCurrent ? "Current finite permission." : "Expired or changed permission retains its slot; explicitly revoke before a fresh grant."}
        <button type="button" className={button} disabled={busy || !!job} onClick={() => void run(async (signal, version) => {
          await exact.revokeRescheduleConsent(grant.consent_id, { expected_revision: grant.revision, idempotency_key: crypto.randomUUID() }, signal);
          await refresh(signal, version); if (generation.current === version) setAcks([false, false, false]);
        })}>Revoke this permission locally</button></div>}
      {!grant && <fieldset className="sm:col-span-2" disabled={busy || !verified}>
        {['Read the full owned event through the separate narrow profile.', 'Read calendar-list metadata to prove data owner and timezone.', 'Permit one exactly approved conditional reschedule during a maximum five-minute window.'].map((label, i) => <label className="block text-xs" key={label}><input type="checkbox" checked={acks[i]} onChange={e => setAcks(values => values.map((v, index) => index === i ? e.target.checked : v))} /> {label}</label>)}
        <button type="button" className={button} disabled={!acks.every(Boolean)} onClick={() => void run(async (signal, version) => {
          const current = await exact.createRescheduleConsent({ ...pair(), request_uuid: crypto.randomUUID(), expires_at: new Date(Date.now() + 300000).toISOString(), acknowledge_owned_event_read: true,
            acknowledge_calendar_list_metadata_read: true, acknowledge_one_conditional_reschedule: true }, signal);
          if (generation.current === version) { setGrants(values => [current, ...values]); setAcks([false, false, false]); }
        })}>Grant finite reschedule permission</button>
      </fieldset>}
      <label>New start with literal UTC offset<input className="cockpit-input w-full" placeholder="2026-10-05T09:00:00+02:00" value={start} disabled={busy || !!job} onChange={e => setStart(e.target.value)} /></label>
      <label>New end with literal UTC offset<input className="cockpit-input w-full" placeholder="2026-10-05T10:00:00+02:00" value={end} disabled={busy || !!job} onChange={e => setEnd(e.target.value)} /></label>
      <label>Explicit IANA timezone<input className="cockpit-input w-full" placeholder="Europe/Warsaw" value={zone} disabled={busy || !!job} onChange={e => setZone(e.target.value)} /></label>
      <label><input type="checkbox" checked={ackPreview} disabled={busy || !!job} onChange={e => setAckPreview(e.target.checked)} /> Read a fresh exact preview (four contacts); no write yet.</label>
      <button type="button" className={button} disabled={busy || !permissionCurrent || !read || !write || !start || !end || !zone || !ackPreview || !!job} onClick={() => void run(async (signal, version) => {
        if (!grant || !read || !write) return;
        const task = await exact.createRescheduleTask({ request_uuid: crypto.randomUUID(), input: { schema_version: 1, consent_id: grant.consent_id, expected_consent_revision: grant.revision,
          event_binding_id: eventBindingId, expected_event_binding_revision: eventBindingRevision, goal_id: goalId, goal_revision: goalRevision,
          new_start: { dateTime: start, timeZone: zone }, new_end: { dateTime: end, timeZone: zone } } }, signal);
        if (generation.current !== version) return; props.onTaskCreated?.();
        const uuid = crypto.randomUUID(); retain({ kind: "calendar_reschedule_v1", uuid });
        await accept(await exact.previewReschedule({ task_id: task.task_id, expected_task_revision: task.task_revision, read_connection_id: read.connection_id, expected_read_revision: read.revision,
          write_connection_id: write.connection_id, expected_write_revision: write.revision, acknowledge_fresh_preview_read: true, request_uuid: uuid }, signal), signal, version);
      })}>Create native task and exact approval preview</button>
    </div>
    {receipt && <button type="button" className={button} disabled={busy} onClick={() => void run(async (signal, version) => {
      const result = receipt.jobId ? await exact.readReschedule(receipt.jobId, signal) : await exact.recoverRescheduleOperation(receipt.kind, receipt.uuid, signal);
      if (!result) { if (generation.current === version) setMessage("No original job is recorded. No request was replayed."); return; }
      await accept(result, signal, version);
      if (result.kind === "calendar_reschedule_identity_v1" && generation.current === version) { setVerified(result.status === "succeeded"); setJob(null); }
    })}>Inspect original native receipt and current private availability</button>}
    {job && <div className="mt-2 text-xs" role="status">
      Native job {job.job_id} · {job.status} · {job.outcome ?? job.failure_reason ?? "awaiting outcome"} · contacts {job.contacts_spent}/13 · transport {job.transport_quiescent ? "closed" : "not proved closed"} · {job.private_read_available ? "current private read available" : job.private_read_reason ?? "private read blocked"}.
      {job.preview && <div className="mt-2"><p>{job.preview.title} · account {job.preview.account_email} · calendar {job.preview.calendar_id}</p>
        <p>Original: {job.preview.old_start.dateTime} → {job.preview.old_end.dateTime} ({job.preview.old_start.timeZone})</p>
        <p>Proposed: {job.preview.new_start.dateTime} → {job.preview.new_end.dateTime} ({job.preview.new_start.timeZone})</p>
        <p>UTC: {job.preview.new_start_utc} → {job.preview.new_end_utc} · exact ETag {job.preview.source_etag}</p>
        <p>Request digest {job.preview.request_digest} · protected digest {job.preview.protected_digest} · expires {new Date(job.preview.expires_at * 1000).toISOString()}</p>
        {job.status === "paused" && <><button type="button" className={button} disabled={busy || job.preview.approval_status !== "pending"} onClick={() => void run(async (signal, version) => {
          await accept(await exact.actReschedule(job.job_id, "decision", { decision: "approved", expected_digest: job.preview!.decision_digest }, signal), signal, version);
        })}>Approve this exact reschedule</button><button type="button" className={button} disabled={busy || job.preview.approval_status !== "pending"} onClick={() => void run(async (signal, version) => {
          await accept(await exact.actReschedule(job.job_id, "decision", { decision: "denied", expected_digest: job.preview!.decision_digest }, signal), signal, version);
        })}>Deny this exact reschedule</button><button type="button" className={button} disabled={busy || job.preview.approval_status !== "approved"} onClick={() => void run(async (signal, version) => {
          await accept(await exact.actReschedule(job.job_id, "execute", {}, signal), signal, version);
        })}>Execute once and independently read back</button></>}
      </div>}
      {["queued", "running", "paused"].includes(job.status) && <button type="button" className={button} disabled={busy} onClick={() => void run(async (signal, version) => {
        await accept(await exact.actReschedule(job.job_id, "cancel", { expected_revision: job.revision, request_uuid: crypto.randomUUID() }, signal), signal, version);
      })}>Cancel original native job</button>}
      {job.status === "unknown_external_effect" && <div><p>Original liability remains Unknown. Readonly observation cannot resend or alter the original approval, deadline or status.</p>
        <label>Current recovery goal<select value={recoveryGoal} disabled={busy} onChange={e => { setRecoveryGoal(e.target.value); setAckRecovery(false); }}><option value="">Choose a newly reviewed finite goal</option>{goals.filter(g => g.revision).map(g => <option key={g.id} value={g.id}>{g.title} · revision {g.revision}</option>)}</select></label>
        <label><input type="checkbox" checked={ackRecovery} disabled={busy} onChange={e => setAckRecovery(e.target.checked)} /> Permit four readonly contacts for this original event.</label>
        <button type="button" className={button} disabled={busy || !read || !recoveryGoal || !ackRecovery || !job.transport_quiescent} onClick={() => void run(async (signal, version) => {
          const g = goals.find(item => item.id === recoveryGoal); if (!g?.revision || !read) return;
          const uuid = crypto.randomUUID(); retain({ kind: "calendar_reschedule_observation_v1", uuid });
          await accept(await exact.actReschedule(job.job_id, "observe", { expected_original_revision: job.revision, read_connection_id: read.connection_id, expected_read_revision: read.revision,
            goal_id: g.id, goal_revision: g.revision, acknowledge_readonly_recovery: true, request_uuid: uuid }, signal), signal, version);
        })}>Observe original event without resending</button>
      </div>}
      {job.observations?.map(o => <p key={o.auxiliary_job_id}>Readonly observation {o.auxiliary_job_id}: {o.outcome}. Original liability retained.</p>)}
    </div>}
    {storageBlocked && <p role="alert">Private receipt storage unavailable. Provider operations are blocked.</p>}
    {message && <p role="alert">{message}</p>}
  </section>;
}
