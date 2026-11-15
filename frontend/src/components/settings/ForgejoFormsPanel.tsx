import { useEffect, useRef, useState } from "react";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";

type Profile = "forgejo.issue-create.v1" | "forgejo.issue-comment.v1";
export type ForgejoFormConnection = { configured: boolean; revision: number; form_profiles_revision: number;
  reviewed_form_profile_ids: Profile[]; state: string; available: boolean; provider_login: string;
  provider_user_id: number | null; read_consent_expires_at: string | null };
type Target = { profile: Profile; owner: string; repository: string; repository_id: number;
  provider_user_id: number; issue_id: number | null; issue_index: number | null; title: string; content: string;
  encoded_body_digest: string; page_digest: string };
type Job = { job_id: string; revision: number; status: string; deadline_at: string; attempt_count: number;
  owner: { principal_id: string }; operator_session_id: string; lease: { fencing_token: number };
  declared_authority: { operation: string }; approval: { id: string; status: string } | null;
  forgejo: { plaintext_digest?: string; exact_destination_id?: number; execution_request?: unknown; phase: string;
    approval_scope?: { private_preview_job_id?: string } } };
type Output = { target?: Target; no_learning: true; exact_id?: number; readback_body?: string;
  readback_title?: string; observation_only?: boolean; original_unknown?: boolean; observed_body?: string };
const profiles: Profile[] = ["forgejo.issue-comment.v1", "forgejo.issue-create.v1"];
const base = "/api/capabilities/forgejo";
const jobPattern = /^forgejo:[a-f0-9]{40}$/;

async function read(path: string, body?: Record<string, unknown>, method = "POST") {
  // Execution owns an original 180s backend deadline. A browser timeout never
  // retries the effect; the operator can inspect the canonical job afterwards.
  const signal = AbortSignal.timeout(path.endsWith("/execute") ? 190_000 : 15_000);
  const response = await apiFetch(API_URL + base + path, body ? {
    method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    signal,
  } : { signal });
  const value = await response.json();
  if (!response.ok) throw Error(value?.detail?.code ?? `Forgejo outcome unconfirmed (${response.status})`);
  return value;
}

export function ForgejoFormsPanel({ connection, goalId, goalRevision, ownerPrincipalId, ownerSessionId, onConnection }: {
  connection: ForgejoFormConnection | null; goalId: string; goalRevision?: number;
  ownerPrincipalId?: string | null; ownerSessionId?: string | null;
  onConnection: (connection: ForgejoFormConnection) => void;
}) {
  const scope = ownerPrincipalId && ownerSessionId ? `seraph.forgejo.forms.${ownerPrincipalId}.${ownerSessionId}` : null;
  const generation = useRef(0);
  const accepted = useRef(0);
  const [selected, setSelected] = useState<Profile[]>([]);
  const [profileAck, setProfileAck] = useState(false);
  const [profile, setProfile] = useState<Profile>("forgejo.issue-create.v1");
  const [owner, setOwner] = useState(""); const [repository, setRepository] = useState("");
  const [issue, setIssue] = useState("1"); const [title, setTitle] = useState(""); const [content, setContent] = useState("");
  const [job, setJob] = useState<Job | null>(null); const [output, setOutput] = useState<Output | null>(null);
  const [exactAck, setExactAck] = useState(false); const [recoveryAck, setRecoveryAck] = useState(false);
  const [busy, setBusy] = useState(false); const [error, setError] = useState<string | null>(null);
  function accept(value: Job) {
    if (!scope || !jobPattern.test(value.job_id) || value.owner?.principal_id !== ownerPrincipalId
      || value.operator_session_id !== ownerSessionId) throw Error("Exact form job belongs to another Root");
    sessionStorage.setItem(scope + ".job", value.job_id);
    setJob(value); setOutput(null); setExactAck(false); setRecoveryAck(false);
    const nonce = ++accepted.current; const version = generation.current;
    const preview = value.forgejo.approval_scope?.private_preview_job_id;
    if (value.declared_authority.operation === "form-submit" && preview && jobPattern.test(preview)) {
      void read(`/jobs/${preview}/output`).then(receipt => {
        if (version === generation.current && nonce === accepted.current) setOutput(receipt);
      }).catch(failure => { if (version === generation.current && nonce === accepted.current) setError(String(failure)); });
    }
  }
  async function perform(action: () => Promise<void>) {
    if (busy || !scope) return;
    const version = generation.current; setBusy(true); setError(null);
    try { await action(); }
    catch (failure) { if (version === generation.current) setError(String(failure)); }
    finally { if (version === generation.current) setBusy(false); }
  }
  useEffect(() => {
    const version = ++generation.current; setJob(null); setOutput(null); setContent(""); setTitle("");
    setExactAck(false); setRecoveryAck(false); setProfileAck(false); setError(null); setBusy(false);
    if (!scope) return;
    const saved = sessionStorage.getItem(scope + ".job");
    if (saved && jobPattern.test(saved)) {
      setBusy(true);
      void read("/jobs/" + saved).then(value => { if (version === generation.current) accept(value); })
        .catch(failure => { if (version === generation.current) setError(String(failure)); })
        .finally(() => { if (version === generation.current) setBusy(false); });
    }
    return () => { ++generation.current; };
    // Restoring history is GET-only; it never restores a field or approval.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope]);
  useEffect(() => {
    setSelected(connection?.reviewed_form_profile_ids ?? []); setProfileAck(false); setExactAck(false);
  }, [connection?.revision, connection?.form_profiles_revision]);
  useEffect(() => { setExactAck(false); setRecoveryAck(false); }, [goalId, goalRevision, profile, owner, repository, issue, title, content]);
  const active = connection?.reviewed_form_profile_ids ?? [];
  const readLive = !!connection?.read_consent_expires_at && Date.parse(connection.read_consent_expires_at) > Date.now();
  const ready = !!scope && connection?.available === true && connection.state === "active" && readLive && !!goalId && !!goalRevision && !busy;
  const fields = { profile, owner, repository, content, ...(profile === "forgejo.issue-create.v1" ? { title } : { issue_index: Number(issue) }) };
  const formJob = job?.declared_authority.operation === "form-submit";
  const target = output?.target;
  const create = profile === "forgejo.issue-create.v1";
  async function prepare(operation: "form-prepare" | "form-submit") {
    const version = generation.current;
    const value = await read("/jobs", { operation, fields: operation === "form-prepare" ? fields : {},
      request_key: crypto.randomUUID(), goal_id: goalId, goal_revision: goalRevision,
      expected_revision: connection?.revision,
      ...(operation === "form-submit" ? { preview_job_id: job?.job_id, preview_digest: job?.forgejo.plaintext_digest } : {}) });
    if (version === generation.current) accept(value);
  }
  return <section aria-label="Forgejo exact issue forms" className="space-y-2">
    <h4>Exact issue creation and comments</h4>
    <p>Fixed Codeberg Forgejo 15.0.9 profiles. Availability needs a separate unchecked acknowledgement; every effect then needs its own exact approval. Production acceptance remains blocked.</p>
    {profiles.map(id => <label key={id}><input type="checkbox" aria-label={id} checked={selected.includes(id)}
      onChange={event => { setSelected(event.target.checked ? [...selected, id].sort() : selected.filter(value => value !== id)); setProfileAck(false); }} />{id}</label>)}
    <label><input type="checkbox" aria-label="Acknowledge reviewed Forgejo form profiles" checked={profileAck}
      onChange={event => setProfileAck(event.target.checked)} />Make only these reviewed profiles available for this account; this grants no POST permission.</label>
    <button disabled={busy || !profileAck || !connection?.configured || (selected.length > 0 && !connection.available)} onClick={() => void perform(async () => {
      const version = generation.current;
      const value = await read("/connection/form-profiles", { expected_revision: connection?.revision,
        expected_form_profiles_revision: connection?.form_profiles_revision, profile_ids: selected, profile_ack: true }, "PUT");
      if (version === generation.current) onConnection(value);
    })}>Save reviewed form availability</button>
    <p>Active profiles: {active.join(", ") || "none"} · revision {connection?.form_profiles_revision ?? 0}</p>
    <label>Exact operation <select aria-label="Forgejo exact form operation" value={profile} onChange={event => setProfile(event.target.value as Profile)}>
      <option value="forgejo.issue-create.v1">Create ordinary issue</option><option value="forgejo.issue-comment.v1">Add ordinary comment</option>
    </select></label>
    <label>Owned repository owner <input aria-label="Forgejo form repository owner" value={owner} onChange={event => setOwner(event.target.value)} /></label>
    <label>Repository <input aria-label="Forgejo form repository" value={repository} onChange={event => setRepository(event.target.value)} /></label>
    {!create && <label>Existing ordinary issue <input aria-label="Forgejo form issue number" value={issue} onChange={event => setIssue(event.target.value)} /></label>}
    {create && <label>Literal title <input aria-label="Forgejo form title" maxLength={245} value={title} onChange={event => setTitle(event.target.value)} /></label>}
    <label>Literal body <textarea aria-label="Forgejo form body" maxLength={16384} value={content} onChange={event => setContent(event.target.value)} /></label>
    <p>Body {new TextEncoder().encode(content).length} UTF-8 bytes. The complete escaped form must fit 16,384 bytes; excess is rejected before approval. Ordinary site notifications and history may be created.</p>
    <button disabled={!ready || !active.includes(profile) || !owner || !repository || !content.trim() || (create && !title)} onClick={() => void perform(() => prepare("form-prepare"))}>Prepare exact form preview</button>
    {error && <p role="alert">{error}</p>}
    {job && <div aria-label="Exact Forgejo form job"><p>{job.job_id} · {job.status} · {job.forgejo.phase}</p><p>Original deadline: {job.deadline_at}</p>
      <button disabled={busy} onClick={() => void perform(async () => { const version = generation.current; const value = await read("/jobs/" + job.job_id); if (version === generation.current) accept(value); })}>Inspect exact form history</button>
      <button disabled={busy || job.status !== "accepted" || (formJob && job.approval?.status !== "approved")} onClick={() => void perform(async () => {
        const version = generation.current; const value = await read(`/jobs/${job.job_id}/execute`, { expected_revision: job.revision, fencing_token: job.lease.fencing_token }); if (version === generation.current) accept(value);
      })}>Run exact form job once</button>
      <button disabled={busy || job.status !== "accepted" || !!job.forgejo.execution_request} onClick={() => void perform(async () => {
        const version = generation.current; const value = await read(`/jobs/${job.job_id}/cancel`, { expected_revision: job.revision, fencing_token: job.lease.fencing_token }); if (version === generation.current) accept(value);
      })}>Cancel unstarted form job</button>
      <button disabled={busy || job.status !== "succeeded"} onClick={() => void perform(async () => {
        const version = generation.current; const value = await read(`/jobs/${job.job_id}/output`); if (version === generation.current) { setOutput(value); setExactAck(false); }
      })}>Read protected form preview or receipt</button>
      {job.approval && <><p>Exact approval: {job.approval.status}. The saved preview below is the approved source; input edits do not change it.</p>
        <label><input type="checkbox" aria-label="Acknowledge exact Forgejo form effect" disabled={!target} checked={exactAck} onChange={event => setExactAck(event.target.checked)} />I reviewed the saved account, repository, literal fields and exact one-POST effect. Unknown will never repeat it.</label>
        <button disabled={busy || !target || !exactAck || job.approval.status !== "pending"} onClick={() => void perform(async () => {
          const version = generation.current; const value = await read(`/jobs/${job.job_id}/approve`, { approval_id: job.approval?.id, decision: "approved", exact_ack: true }); if (version === generation.current) accept(value);
        })}>Approve saved exact form once</button></>}
      {job.status === "unknown_external_effect" && <><p>The original effect is Unknown and retains its liability. Reload and execution replay inspect history only.</p>
        {job.forgejo.exact_destination_id ? <><label><input type="checkbox" aria-label="Acknowledge exact-ID form recovery" checked={recoveryAck} onChange={event => setRecoveryAck(event.target.checked)} />Allow only a fresh bounded GET of retained exact ID {job.forgejo.exact_destination_id}; the original stays Unknown.</label>
          <button disabled={busy || !recoveryAck || !readLive} onClick={() => void perform(async () => { const version = generation.current; const value = await read(`/jobs/${job.job_id}/read-only-recovery`, { expected_revision: connection?.revision, original_job_revision: job.revision, original_fencing_token: job.lease.fencing_token, request_key: crypto.randomUUID(), read_ack: true }); if (version === generation.current) accept(value); })}>Prepare exact-ID GET-only recovery</button></>
          : <p>No trustworthy exact ID was retained. This Unknown cannot be resolved by listing, searching or sending another POST.</p>}</>}
    </div>}
    {target && <div aria-label="Protected literal Forgejo form preview"><p>Account ID {target.provider_user_id} · {target.owner}/{target.repository} · repository ID {target.repository_id}{target.issue_id ? ` · issue ID ${target.issue_id} (#${target.issue_index})` : ""}</p>
      <p>{target.profile} · {target.encoded_body_digest}</p><pre>{target.title}</pre><pre>{target.content}</pre><p>Ordinary site notifications and history may be created. One native POST, then exact numeric API readback; no automatic destination navigation.</p>
      <button disabled={!ready || job?.declared_authority.operation !== "form-prepare"} onClick={() => void perform(() => prepare("form-submit"))}>Prepare approval for this saved form</button></div>}
    {output?.readback_body && <div aria-label="Verified exact Forgejo form receipt"><p>Verified numeric ID {output.exact_id} · no_learning</p><pre>{output.readback_title}</pre><pre>{output.readback_body}</pre></div>}
    {output?.observation_only && <p>GET-only exact-ID observation: {output.observed_body}. The original effect stays Unknown and its liability remains reserved.</p>}
  </section>;
}
