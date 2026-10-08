import { useEffect, useRef, useState } from "react";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import type { GoalInfo } from "../../types";
import { ForgejoFormsPanel, type ForgejoFormConnection } from "./ForgejoFormsPanel";

type Target = { owner: string; repository: string; repository_id: number; issue_id: number;
  issue_index: number; old_title: string; new_title: string; updated_at: string; timeline_digest: string };
type Connection = ForgejoFormConnection & { configured: boolean; connection_id: string | null; revision: number; state: string;
  provider_user_id: number | null; provider_login: string; read_consent_revision: number;
  read_consent_expires_at: string | null; available: boolean; no_learning: true };
type Job = { job_id: string; revision: number; status: string; deadline_at: string; goal_id: string;
  attempt_count: number;
  owner: { kind: string; principal_id: string }; operator_session_id: string; lease: { fencing_token: number };
  declared_authority: { operation: string }; approval: { id: string; status: string } | null;
  forgejo: { phase: string; plaintext_digest?: string; capacity_closed?: boolean;
    approval_scope?: { target: Target };
    execution_request?: { expected_revision: number; fencing_token: number } } };
type Output = { target?: Target; no_change?: boolean; no_learning: true; readback_title?: string;
  observation_only?: boolean; observed_current_title?: string; original_unknown?: boolean;
  original_capacity_released?: boolean };
type Pending = { method: "POST" | "PUT"; path: string; body: Record<string, unknown> };
const base = "/api/capabilities/forgejo";
const jobPattern = /^forgejo:[a-f0-9]{40}$/;

function pendingValue(raw: string | null): Pending | null {
  if (raw === null) return null;
  if (new TextEncoder().encode(raw).length > 8192) throw Error("Saved Forgejo request exceeds its bound");
  const value = JSON.parse(raw) as Pending;
  if (!value || !["POST", "PUT"].includes(value.method) || typeof value.path !== "string"
    || !value.body || typeof value.body !== "object" || Array.isArray(value.body)
    || !/^(?:\/connection(?:\/read-consent|\/revoke)?|\/jobs(?:\/forgejo:[a-f0-9]{40}\/(?:execute|approve|cancel|read-only-recovery))?)$/.test(value.path)) {
    throw Error("Saved Forgejo request is outside these fixed controls");
  }
  const body = value.body;
  const integer = (key: string, min = 1) => Number.isSafeInteger(body[key]) && Number(body[key]) >= min;
  const text = (key: string, max: number) => typeof body[key] === "string" && String(body[key]).length > 0 && String(body[key]).length <= max;
  let allowed: string[] = [];
  let valid = false;
  if (value.path === "/connection" && value.method === "PUT") {
    allowed = ["vault_key", "expected_revision"]; valid = text("vault_key", 256) && integer("expected_revision", 0);
  } else if (value.path === "/connection/read-consent" && value.method === "PUT") {
    allowed = ["expected_revision", "duration_seconds", "read_ack"];
    valid = integer("expected_revision") && body.duration_seconds === 900 && body.read_ack === true;
  } else if (value.path === "/connection/revoke" && value.method === "POST") {
    allowed = ["expected_revision"]; valid = integer("expected_revision");
  } else if (value.method === "POST" && value.path === "/jobs") {
    allowed = ["operation", "fields", "request_key", "goal_id", "goal_revision", "expected_revision", "preview_job_id", "preview_digest"];
    valid = ["provision", "preview", "title"].includes(String(body.operation)) && text("goal_id", 128)
      && integer("goal_revision") && integer("expected_revision") && text("request_key", 36)
      && /^[a-f0-9-]{36}$/.test(String(body.request_key)) && !!body.fields && typeof body.fields === "object" && !Array.isArray(body.fields);
    const fields = body.fields as Record<string, unknown>;
    if (body.operation === "preview") valid &&= Object.keys(fields).sort().join(",") === "issue_index,new_title,owner,repository"
      && typeof fields.owner === "string" && typeof fields.repository === "string" && typeof fields.new_title === "string"
      && Number.isSafeInteger(fields.issue_index) && Number(fields.issue_index) >= 1;
    else valid &&= Object.keys(fields).length === 0;
    if (body.operation === "title") valid &&= typeof body.preview_job_id === "string" && jobPattern.test(body.preview_job_id)
      && typeof body.preview_digest === "string" && /^[a-f0-9]{64}$/.test(body.preview_digest);
    else valid &&= body.preview_job_id === undefined && body.preview_digest === undefined;
  } else if (value.method === "POST" && /\/(execute|cancel)$/.test(value.path)) {
    allowed = ["expected_revision", "fencing_token"]; valid = integer("expected_revision") && integer("fencing_token", 0);
  } else if (value.method === "POST" && value.path.endsWith("/approve")) {
    allowed = ["approval_id", "decision", "exact_ack"];
    valid = text("approval_id", 128) && body.decision === "approved" && body.exact_ack === true;
  } else if (value.method === "POST" && value.path.endsWith("/read-only-recovery")) {
    allowed = ["expected_revision", "original_job_revision", "original_fencing_token", "request_key", "read_ack"];
    valid = integer("expected_revision") && integer("original_job_revision") && integer("original_fencing_token", 0)
      && text("request_key", 36) && /^[a-f0-9-]{36}$/.test(String(body.request_key)) && body.read_ack === true;
  }
  if (!valid || Object.keys(body).some(key => !allowed.includes(key)) || Object.keys(value).sort().join(",") !== "body,method,path") {
    throw Error("Saved Forgejo request has invalid fixed fields");
  }
  return value;
}

async function request(path: string, init: RequestInit = {}, signal?: AbortSignal) {
  const response = await apiFetch(API_URL + base + path, { ...init, signal });
  const value = await response.json();
  if (!response.ok) throw Error(value?.detail?.code ?? `Forgejo outcome unconfirmed (${response.status})`);
  return value;
}

export function ForgejoTitlePanel({ ownerPrincipalId, ownerSessionId }: {
  ownerPrincipalId?: string | null; ownerSessionId?: string | null;
}) {
  const scope = ownerPrincipalId && ownerSessionId ? `seraph.forgejo.${ownerPrincipalId}.${ownerSessionId}` : null;
  const generation = useRef(0);
  const [connection, setConnection] = useState<Connection | null>(null);
  const [loadedScope, setLoadedScope] = useState<string | null>(null);
  const [goals, setGoals] = useState<GoalInfo[]>([]);
  const [keys, setKeys] = useState<string[]>([]);
  const [goalId, setGoalId] = useState("");
  const [vaultKey, setVaultKey] = useState("");
  const [repositoryOwner, setRepositoryOwner] = useState("");
  const [repository, setRepository] = useState("");
  const [issueIndex, setIssueIndex] = useState("1");
  const [title, setTitle] = useState("");
  const [readAck, setReadAck] = useState(false);
  const [exactAck, setExactAck] = useState(false);
  const [recoveryAck, setRecoveryAck] = useState(false);
  const [job, setJob] = useState<Job | null>(null);
  const [output, setOutput] = useState<Output | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const selectedGoal = goals.find(value => value.id === goalId);

  function acceptJob(value: Job, current: number) {
    if (current !== generation.current || !scope) return;
    if (!jobPattern.test(value.job_id) || value.owner?.kind !== "user" || value.owner.principal_id !== ownerPrincipalId
      || value.operator_session_id !== ownerSessionId) throw Error("Forgejo job belongs to another Root");
    sessionStorage.setItem(scope + ".job", value.job_id);
    setExactAck(false); setRecoveryAck(false); setJob(value); setOutput(null);
  }

  useEffect(() => {
    const current = ++generation.current;
    const abort = new AbortController();
    setLoadedScope(null); setConnection(null); setJob(null); setOutput(null); setPending(null); setError(null); setBusy(false);
    setGoals([]); setKeys([]); setGoalId(""); setVaultKey(""); setReadAck(false); setExactAck(false); setRecoveryAck(false);
    if (!scope) return () => abort.abort();
    try { setPending(pendingValue(sessionStorage.getItem(scope + ".cancel-pending"))
      ?? pendingValue(sessionStorage.getItem(scope + ".pending"))); }
    catch (failure) { setError(String(failure)); return () => abort.abort(); }
    const timer = window.setTimeout(() => abort.abort(), 15000);
    void Promise.all([request("/connection", {}, abort.signal),
      apiFetch(API_URL + "/api/goals/tree", { signal: abort.signal }),
      apiFetch(API_URL + "/api/vault/keys", { signal: abort.signal })]).then(async ([value, goalResponse, keyResponse]) => {
      if (!goalResponse.ok || !keyResponse.ok) throw Error("Local Goal or Vault metadata unavailable");
      const tree = await goalResponse.json() as GoalInfo[];
      const flat: GoalInfo[] = [];
      const visit = (entry: GoalInfo) => { flat.push(entry); entry.children?.forEach(visit); };
      tree.forEach(visit);
      const names = await keyResponse.json() as { key: string }[];
      if (current !== generation.current) return;
      setLoadedScope(scope); setConnection(value as Connection); setGoals(flat.filter(entry => entry.status === "active"));
      setKeys(names.map(entry => entry.key));
      const last = sessionStorage.getItem(scope + ".job");
      if (last !== null) {
        if (last.length > 64 || !jobPattern.test(last)) throw Error("Saved original job reference is invalid");
        acceptJob(await request("/jobs/" + last, {}, abort.signal), current);
      }
    }).catch(failure => { if (current === generation.current) setError(String(failure)); })
      .finally(() => window.clearTimeout(timer));
    return () => { ++generation.current; abort.abort(); window.clearTimeout(timer); };
  }, [scope]);

  useEffect(() => { setReadAck(false); setExactAck(false); setRecoveryAck(false); },
    [scope, connection?.revision, connection?.read_consent_revision, goalId, selectedGoal?.revision, job?.job_id, job?.revision]);

  async function act(next?: Pending) {
    if (!scope || loadedScope !== scope || busy) return;
    const command = next ?? pending;
    if (!command) return;
    const raw = JSON.stringify(command); pendingValue(raw);
    const cancelling = command.path.endsWith("/cancel");
    const storageKey = scope + (cancelling ? ".cancel-pending" : ".pending");
    sessionStorage.setItem(storageKey, raw);
    if (sessionStorage.getItem(storageKey) !== raw) throw Error("Exact request retention failed");
    setPending(command); setBusy(true); setError(null);
    const current = generation.current;
    try {
      const value = await request(command.path, { method: command.method,
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(command.body) });
      if (current !== generation.current) return;
      if (cancelling && value.job_id !== command.path.slice(6, -7)) {
        throw Error("Original cancellation is not canonically confirmed");
      }
      if (value.job_id) acceptJob(value, current);
      else { setConnection(value as Connection); setOutput(null); }
      if (cancelling && value.status !== "cancelled") {
        setError("Cancellation is not terminally confirmed; read the original state. Exact requests remain retained.");
        return;
      }
      sessionStorage.removeItem(storageKey);
      if (cancelling && value.status === "cancelled") sessionStorage.removeItem(scope + ".pending");
      setPending(null);
    } catch (failure) { if (current === generation.current) setError(String(failure)); }
    finally { if (current === generation.current) setBusy(false); }
  }

  async function inspect() {
    if (busy || !scope) return;
    setBusy(true); setError(null); const current = generation.current;
    try {
      const value = await request("/connection") as Connection;
      if (current !== generation.current) return;
      setConnection(value);
      if (job) acceptJob(await request("/jobs/" + job.job_id), current);
      else if (pending?.path === "/jobs" && typeof pending.body.request_key === "string") {
        acceptJob(await request("/requests/" + pending.body.request_key), current);
      }
    } catch (failure) { if (current === generation.current) setError(String(failure)); }
    finally { if (current === generation.current) setBusy(false); }
  }

  async function readOutput() {
    if (!job || busy) return;
    const current = generation.current; setBusy(true); setError(null);
    try { const value = await request("/jobs/" + job.job_id + "/output");
      if (current === generation.current) setOutput(value as Output);
    } catch (failure) { if (current === generation.current) setError(String(failure)); }
    finally { if (current === generation.current) setBusy(false); }
  }

  const readLive = !!connection?.read_consent_expires_at && Date.parse(connection.read_consent_expires_at) > Date.now();
  const ready = !!scope && loadedScope === scope && connection?.available === true && readLive && !!selectedGoal && !busy && !pending;
  const prepare = (operation: string, fields: Record<string, unknown> = {}, extras = {}) => void act({ method: "POST", path: "/jobs",
    body: { operation, fields, request_key: crypto.randomUUID(), goal_id: goalId,
      goal_revision: selectedGoal?.revision, expected_revision: connection?.revision, ...extras } });
  const target = output?.target ?? job?.forgejo.approval_scope?.target;
  const approvedTarget = target && job?.declared_authority.operation === "preview" && output?.no_change !== true;
  const pendingUnstartedExecution = !!job && job.status === "accepted" && job.attempt_count === 0
    && !job.forgejo.execution_request && pending?.path === `/jobs/${job.job_id}/execute`
    && pending.body.expected_revision === job.revision && pending.body.fencing_token === job.lease.fencing_token;
  return <section aria-label="Forgejo issue title transaction" className="space-y-3">
    <h3>Forgejo issue title</h3>
    <p>One ordinary issue title, reviewed literally before one Save. No payment, generated actions or learning.</p>
    <p>Production Codeberg execution is blocked until separate account, version and effect acceptance. A key cannot enable live execution.</p>
    <p>The provider has no atomic revision check or idempotency key. Concurrent edits can race the final handoff; title history and notifications remain. Unknown never retries Save or automatically reverses it.</p>
    <p>Passwords and session cookies stay in backend Vault transport, outside the browser and receipts. Only selected identity, titles, digests, intent and readback are retained privately.</p>
    <p>Connection: {connection?.state ?? "metadata unavailable"} · account {connection?.provider_login || "unverified"} {connection?.provider_user_id ?? ""}</p>
    {error && <p role="alert">{error}</p>}
    <button disabled={busy || !scope} onClick={() => void inspect()}>Read current state</button>
    <label>Credential Vault key <select aria-label="Forgejo credential Vault key" value={vaultKey} onChange={event => setVaultKey(event.target.value)}><option value="">Select owned key</option>{keys.map(key => <option key={key}>{key}</option>)}</select></label>
    <button disabled={!scope || !vaultKey || busy || !!pending} onClick={() => void act({ method: "PUT", path: "/connection", body: { vault_key: vaultKey, expected_revision: connection?.revision ?? 0 } })}>Configure fixed site</button>
    <label>Finite Goal <select aria-label="Forgejo finite Goal" value={goalId} onChange={event => setGoalId(event.target.value)}><option value="">Select active Goal</option>{goals.map(goal => <option key={goal.id} value={goal.id}>{goal.title}</option>)}</select></label>
    <label><input aria-label="Acknowledge Forgejo finite private reads" type="checkbox" checked={readAck} onChange={event => setReadAck(event.target.checked)} />Allow backend login and fixed private reads for at most 15 minutes, capped by this Root. This grants no title mutation.</label>
    <button disabled={!readAck || !connection?.configured || busy || !!pending} onClick={() => void act({ method: "PUT", path: "/connection/read-consent", body: { expected_revision: connection?.revision, duration_seconds: 900, read_ack: true } })}>Grant finite read consent</button>
    <p>Read window: {connection?.read_consent_expires_at ?? "none"}</p>
    <button disabled={!ready || !connection?.read_consent_expires_at} onClick={() => prepare("provision")}>Prepare backend session job</button>
    <label>Repository owner <input aria-label="Forgejo repository owner" value={repositoryOwner} onChange={event => setRepositoryOwner(event.target.value)} /></label>
    <label>Repository <input aria-label="Forgejo repository" value={repository} onChange={event => setRepository(event.target.value)} /></label>
    <label>Issue number <input aria-label="Forgejo issue number" value={issueIndex} onChange={event => setIssueIndex(event.target.value)} /></label>
    <label>Exact new title <input aria-label="Forgejo approved title" maxLength={245} value={title} onChange={event => setTitle(event.target.value)} /></label>
    <button disabled={!ready || connection?.state !== "active" || !repositoryOwner || !repository || !title} onClick={() => prepare("preview", { owner: repositoryOwner, repository, issue_index: Number(issueIndex), new_title: title })}>Prepare title preview job</button>
    {job && <div aria-label="Original Forgejo job"><p>{job.job_id} · {job.status} · {job.forgejo.phase} · no_learning</p><p>Original deadline: {job.deadline_at}</p>
      <button disabled={busy || !!pending || job.status !== "accepted" || (job.declared_authority.operation === "title" && job.approval?.status !== "approved")} onClick={() => void act({ method: "POST", path: `/jobs/${job.job_id}/execute`, body: { expected_revision: job.revision, fencing_token: job.lease.fencing_token } })}>Run original job once</button>
      <button disabled={busy || (!!pending && !pendingUnstartedExecution) || !["accepted", "running"].includes(job.status)} onClick={() => void act({ method: "POST", path: `/jobs/${job.job_id}/cancel`, body: { expected_revision: job.revision, fencing_token: job.lease.fencing_token } })}>Cancel original job</button>
      <button disabled={busy || job.status !== "succeeded"} onClick={() => void readOutput()}>Read protected receipt</button>
      {job.approval && <><p>Exact title approval: {job.approval.status}</p><label><input aria-label="Acknowledge exact Forgejo title edit" type="checkbox" checked={exactAck} onChange={event => setExactAck(event.target.checked)} />Approve the saved preview’s exact title, numeric issue and one original Save, with the disclosed race and history effects.</label><button disabled={busy || !!pending || !exactAck || job.approval.status !== "pending"} onClick={() => void act({ method: "POST", path: `/jobs/${job.job_id}/approve`, body: { approval_id: job.approval?.id, decision: "approved", exact_ack: true } })}>Approve exact title edit</button></>}
      {job.status === "unknown_external_effect" && <><p>{job.declared_authority.operation === "title"
        ? "Original title effect is Unknown. Capacity remains reserved; equality cannot prove who made the edit."
        : "Original session job outcome is Unknown. Capacity remains reserved; inspect its original history without starting another operation."}</p>
        {job.declared_authority.operation === "title" && <><label><input aria-label="Acknowledge Forgejo read-only recovery" type="checkbox" checked={recoveryAck} onChange={event => setRecoveryAck(event.target.checked)} />Allow a separate bounded GET-only observation; do not repeat Save.</label><button disabled={busy || !!pending || !recoveryAck || !connection?.available} onClick={() => void act({ method: "POST", path: `/jobs/${job.job_id}/read-only-recovery`, body: { expected_revision: connection?.revision, original_job_revision: job.revision, original_fencing_token: job.lease.fencing_token, request_key: crypto.randomUUID(), read_ack: true } })}>Prepare read-only recovery</button></>}
      </>}
    </div>}
    {target && <div aria-label="Verified Forgejo title preview"><p>Issue ID {target.issue_id} · repository ID {target.repository_id} · issue #{target.issue_index}</p><pre>Old title: {target.old_title}</pre><pre>Approved title: {target.new_title}</pre><p>Source revision: {target.updated_at}</p><p>{output?.no_change ? "No change: read-only preview" : "Exact saved preview; no atomic provider revision check"}</p><button disabled={!ready || !approvedTarget} onClick={() => prepare("title", {}, { preview_job_id: job?.job_id, preview_digest: job?.forgejo.plaintext_digest })}>Prepare independently approved title job</button></div>}
    {output?.readback_title && <p>Verified readback title: {output.readback_title} · no_learning</p>}
    {output?.observation_only && <p>GET-only observed title: {output.observed_current_title}. Original write remains Unknown; attribution is uncertain and original capacity is retained.</p>}
    {pending && <div aria-label="Unconfirmed exact Forgejo request"><p>The exact request is retained. Reload performs reads only; no automatic POST or Save replay.</p><button disabled={busy} onClick={() => void act()}>Retry exact retained request</button></div>}
    <ForgejoFormsPanel connection={connection} goalId={goalId} goalRevision={selectedGoal?.revision}
      ownerPrincipalId={ownerPrincipalId} ownerSessionId={ownerSessionId}
      onConnection={value => setConnection(value as Connection)} />
    <button disabled={!connection?.configured || busy || !!pending} onClick={() => void act({ method: "POST", path: "/connection/revoke", body: { expected_revision: connection?.revision } })}>Revoke backend session</button>
  </section>;
}
