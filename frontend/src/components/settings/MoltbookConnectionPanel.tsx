import { useEffect, useRef, useState } from "react";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import type { GoalInfo } from "../../types";
import { MoltbookWriteControls } from "./MoltbookWriteControls";
import { clearMoltbookPending, moltbookRequest, moltbookStorageKey, readMoltbookPending,
  submitMoltbook, originalExecution, pendingApplied, equalMoltbookBody, readMoltbookLastJob, type MoltbookConnection, type MoltbookJob, type MoltbookPending } from "../../lib/moltbook";

export function MoltbookConnectionPanel({ ownerPrincipalId, ownerSessionId }: {
  ownerPrincipalId?: string | null; ownerSessionId?: string | null;
}) {
  const generation = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const [connection, setConnection] = useState<MoltbookConnection | null>(null);
  const [keys, setKeys] = useState<string[]>([]);
  const [vaultKey, setVaultKey] = useState("");
  const [goals, setGoals] = useState<GoalInfo[]>([]);
  const [goalId, setGoalId] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [privateAcknowledgment, setPrivateAcknowledgment] = useState<string | null>(null);
  const [allowWrites, setAllowWrites] = useState(false);
  const [operation, setOperation] = useState("feed");
  const [postId, setPostId] = useState("");
  const [community, setCommunity] = useState("introductions");
  const [job, setJob] = useState<MoltbookJob | null>(null);
  const [output, setOutput] = useState("");
  const [pending, setPending] = useState<MoltbookPending | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const storageKey = ownerPrincipalId && ownerSessionId ? moltbookStorageKey(ownerPrincipalId, ownerSessionId) : null;
  const selectedGoal = goals.find(value => value.id === goalId);
  const privateScope = JSON.stringify([storageKey, connection?.id, connection?.revision, goalId, selectedGoal?.revision]);
  const privateReady = connection?.private_browser?.available === true && connection.mode === "active";
  async function localRefresh(signal?: AbortSignal) {
    const value = await moltbookRequest("/connection", {}, signal) as MoltbookConnection;
    if (typeof value.configured !== "boolean" || value.credential_is_consent !== false || value.no_learning !== true) throw Error("Local connection metadata unavailable");
    return value;
  }
  useEffect(() => {
    const current = ++generation.current;
    controller.current?.abort(); controller.current = null;
    setBusy(false); setError(null); setConnection(null); setPending(null); setJob(null); setOutput("");
    setAcknowledged(false); setAllowWrites(false); setGoals([]); setKeys([]); setVaultKey(""); setGoalId("");
    if (!storageKey) return;
    try { setPending(readMoltbookPending(storageKey)); } catch (failure) { setError(String(failure)); return; }
    const abort = new AbortController(); controller.current = abort;
    const timer = window.setTimeout(() => abort.abort(), 15000);
    void Promise.all([localRefresh(abort.signal), apiFetch(API_URL + "/api/vault/keys", { signal: abort.signal }),
      apiFetch(API_URL + "/api/goals/tree", { signal: abort.signal })]).then(async ([value, keyResponse, goalResponse]) => {
      if (!keyResponse.ok || !goalResponse.ok) throw Error("Local Goal or Vault metadata unavailable");
      const keyData = await keyResponse.json() as { key: string }[];
      const tree = await goalResponse.json() as GoalInfo[];
      const flat: GoalInfo[] = [];
      const visit = (entry: GoalInfo) => { flat.push(entry); entry.children?.forEach(visit); };
      tree.forEach(visit);
      if (current !== generation.current) return;
      setConnection(value); setKeys(keyData.map(item => item.key)); setGoals(flat.filter(item => item.status === "active"));
      setGoalId(value.consent?.goal_id ?? "");
    }).catch(failure => { if (current === generation.current) setError(String(failure)); })
      .finally(() => window.clearTimeout(timer));
    return () => { ++generation.current; abort.abort(); window.clearTimeout(timer); };
  }, [storageKey]);

  async function act(request?: MoltbookPending) {
    if (!storageKey || busy) return;
    const current = generation.current;
    const abort = new AbortController(); controller.current = abort;
    const timer = window.setTimeout(() => abort.abort(), 45000);
    setBusy(true); setError(null);
    try {
      const retained = readMoltbookPending(storageKey);
      const exact = request ?? retained;
      if (!exact || (retained && JSON.stringify(retained) !== JSON.stringify(exact))) throw Error("Inspect the original retained request before starting another");
      const result = await submitMoltbook(storageKey, exact, abort.signal);
      if (current !== generation.current) return;
      const local = await localRefresh(abort.signal);
      if (current !== generation.current) return;
      if ("job_id" in result) setJob(result as unknown as MoltbookJob);
      else if (exact.path.endsWith("/approval")) {
        const original = await moltbookRequest(`/jobs/${exact.path.split("/")[2]}`, {}, abort.signal);
        if (current !== generation.current) return;
        setJob(original as MoltbookJob);
      }
      setPending(null); setConnection(local);
      if (result.status === "succeeded") {
        const value = await moltbookRequest(`/jobs/${result.job_id}/output`, {}, abort.signal);
        if (current === generation.current) setOutput(JSON.stringify(value, null, 2));
      }
    } catch (failure) {
      if (current === generation.current) {
        setError(String(failure));
        try { setPending(readMoltbookPending(storageKey)); } catch (corrupt) { setError(String(corrupt)); }
      }
    } finally { window.clearTimeout(timer); if (current === generation.current) setBusy(false); }
  }
  async function refresh() {
    if (!storageKey || busy) return;
    const current = generation.current; const abort = new AbortController(); controller.current = abort;
    const timer = window.setTimeout(() => abort.abort(), 15000);
    setBusy(true); setError(null);
    try {
      const value = await localRefresh(abort.signal);
      const retained = readMoltbookPending(storageKey);
      if (current !== generation.current) return;
      setConnection(value);
      if (retained && ["/connection/consent", "/connection/private-home-consent"].includes(retained.path) && value.consent?.request && equalMoltbookBody(value.consent.request, retained.body)) {
        clearMoltbookPending(storageKey); setPending(null);
      }
      const originalJob = retained?.path.startsWith("/jobs/") ? retained.path.split("/")[2] : job?.job_id ?? value.active_job_id ?? readMoltbookLastJob(storageKey);
      if (originalJob) {
        const readback = await moltbookRequest(`/jobs/${originalJob}`, {}, abort.signal) as MoltbookJob;
        if (readback.job_id !== originalJob || readback.no_learning !== true) throw Error("Original job readback mismatch");
        if (current !== generation.current) return;
        setJob(readback);
        if (retained && pendingApplied(readback, retained)) { clearMoltbookPending(storageKey); setPending(null); }
        if (readback.status === "succeeded") {
          const result = await moltbookRequest(`/jobs/${originalJob}/output`, {}, abort.signal);
          if (current === generation.current) setOutput(JSON.stringify(result, null, 2));
        }
      }
    } catch (failure) { if (current === generation.current) setError(String(failure)); }
    finally { window.clearTimeout(timer); if (current === generation.current) setBusy(false); }
  }
  async function allowReads() {
    if (!selectedGoal || !connection?.revision || !acknowledged || busy || pending) return;
    await act({ method: "POST", path: "/connection/consent", body: {
        request_key: crypto.randomUUID(),
        expected_revision: connection.revision, goal_id: selectedGoal.id, goal_revision: selectedGoal.revision,
        actions: ["inspect", "feed", "post", "comments", "community", ...(allowWrites ? ["create_post", "create_comment"] : [])], duration_seconds: 300,
        personal_noncommercial: true, no_redistribution: true,
    } });
  }
  function prepare() {
    if (!selectedGoal || !connection?.revision) return;
    const fields = operation === "inspect" ? {} : operation === "feed" ? { sort: "new", limit: 1, community }
      : operation === "community" ? { community } : operation === "post" ? { post_id: postId } : { post_id: postId, sort: "new", limit: 1 };
    void act({ method: "POST", path: "/reads", body: { operation, fields, request_key: crypto.randomUUID(),
      goal_id: selectedGoal.id, goal_revision: selectedGoal.revision, expected_revision: connection.revision } });
  }
  function allowPrivateHome() {
    if (!privateReady || privateAcknowledgment !== privateScope || !selectedGoal || !connection?.revision) return;
    void act({ method: "POST", path: "/connection/private-home-consent", body: {
      request_key: crypto.randomUUID(), expected_revision: connection.revision,
      goal_id: selectedGoal.id, goal_revision: selectedGoal.revision,
      actions: ["private_home"], duration_seconds: 300, personal_noncommercial: true,
      no_redistribution: true, private_bookkeeping_ack: true,
    } });
  }
  function preparePrivateHome() {
    if (!privateReady || !selectedGoal || !connection?.revision) return;
    void act({ method: "POST", path: "/reads", body: {
      operation: "private_home", fields: {}, request_key: crypto.randomUUID(),
      goal_id: selectedGoal.id, goal_revision: selectedGoal.revision, expected_revision: connection.revision,
    } });
  }
  return <section aria-label="Moltbook connection" className="space-y-3 px-1">
    <p>Moltbook is optional. Imported credentials grant no read or write consent. Account claim remains a manual human action.</p>
    <p role="status">{connection ? `${connection.mode.replace(/_/g, " ")} · ${connection.account_name || "Account not yet inspected"}` : "Loading local metadata"}</p>
    <button disabled={busy || !storageKey} onClick={() => void refresh()}>Refresh local metadata and original job</button>
    <label className="block">Private Vault credential <select aria-label="Moltbook Vault credential" value={vaultKey} onChange={event => setVaultKey(event.target.value)}>
      <option value="">Choose an owner Vault entry</option>{keys.map(key => <option key={key}>{key}</option>)}</select></label>
    <button disabled={busy || !!pending || !vaultKey || !!connection?.active_job_id} onClick={() => void act({ method: "PUT", path: "/connection",
      body: { vault_key: vaultKey, request_key: crypto.randomUUID(), expected_revision: connection?.revision ?? null } })}>Import selected credential</button>
    <label className="block">Current Goal <select aria-label="Moltbook Goal" value={goalId} onChange={event => { setGoalId(event.target.value); setAcknowledged(false); }}>
      <option value="">Choose an active Goal</option>{goals.map(goal => <option key={goal.id} value={goal.id}>{goal.title} · revision {goal.revision}</option>)}</select></label>
    <label className="block"><input type="checkbox" checked={acknowledged} onChange={event => setAcknowledged(event.target.checked)} /> I authorize personal, noncommercial reads for five minutes; I will not redistribute community content.</label>
    <button disabled={busy || !!pending || !connection?.configured || !selectedGoal || !acknowledged || !!connection.active_job_id} onClick={() => void allowReads()}>Allow finite reads for this login and Goal</button>
    <label className="block"><input type="checkbox" checked={allowWrites} onChange={event => { setAllowWrites(event.target.checked); setAcknowledged(false); }} /> Include public text actions in this finite consent; each write still needs exact approval.</label>
    <p>Consent expires {connection?.consent?.expires_at ?? "before any remote use"}. Remote refresh is always explicit.</p>
    {connection?.cooldown_until && <p>Provider cooldown until {String(connection.cooldown_until)}. No automatic retry.</p>}
    <label className="block">Read operation <select aria-label="Moltbook read operation" value={operation} onChange={event => setOperation(event.target.value)}>
      <option value="feed">One feed item</option><option value="inspect">Inspect account and claim status</option><option value="community">Inspect public community</option><option value="post">Known post</option><option value="comments">One comment page</option></select></label>
    {(operation === "feed" || operation === "community") && <label className="block">Community <input aria-label="Moltbook community" value={community} onChange={event => setCommunity(event.target.value)} /></label>}
    {(operation === "post" || operation === "comments") && <label className="block">Known post ID <input aria-label="Moltbook post ID" value={postId} onChange={event => setPostId(event.target.value)} /></label>}
    <button disabled={busy || !!pending || !selectedGoal || !!connection?.active_job_id || !connection?.consent?.actions?.includes(operation)} onClick={prepare}>Prepare bounded read</button>
    {job && <div aria-label="Moltbook original job"><p>{job.job_id} · {job.status} · original deadline {job.deadline_at} · attempt {job.attempt_count}</p>
      <button disabled={busy || !!pending || job.status !== "accepted" || !job.lease} onClick={() => void act(originalExecution(job))}>Run original read</button>
      <button disabled={busy || !!pending} onClick={() => void act({ method: "POST", path: `/jobs/${job.job_id}/recover`, body: {} })}>Inspect or recover original written output</button>
      <button disabled={busy || !!pending || !job.lease || ["succeeded", "cancelled"].includes(job.status)} onClick={() => void act({ method: "POST", path: `/jobs/${job.job_id}/cancel`, body: { request_key: crypto.randomUUID(), expected_revision: job.revision, fencing_token: job.lease.fencing_token } })}>Cancel remaining original work</button>
      {!["accepted", "queued", "succeeded"].includes(job.status) && <p>Explicit inspection is required. No contact is replayed or deadline renewed.</p>}</div>}
    <section aria-label="Private Moltbook Home browser read">
      <h4>Private Home document in Chromium</h4>
      <p>Production Home is blocked until separately authorized effect, identity and account acceptance evidence exists. Local test availability grants no production access.</p>
      <p>The complete Home JSON document is fetched. Only account name, karma, unread count and own-post ID, title, community, notification count, latest time, commenter names and literal preview are retained with private field citations. Role briefings, unrelated activity and suggested actions are discarded; no instructions are followed.</p>
      <p>One Home contact plus two identity checks, at most 120 seconds and one attempt. Home may deliver or consume a due briefing and record access bookkeeping; this is not side-effect-free. No role execution, notification mark-read, business write, heartbeat, model call or learning.</p>
      <label><input type="checkbox" aria-label="Acknowledge private Home delivery bookkeeping"
        checked={privateAcknowledgment === privateScope} disabled={busy || !!pending || !privateReady}
        onChange={event => setPrivateAcknowledgment(event.target.checked ? privateScope : null)} />
        I separately authorize one private Home read and possible due-briefing delivery bookkeeping for this Goal and login.</label>
      <button disabled={busy || !!pending || !privateReady || !selectedGoal || privateAcknowledgment !== privateScope || !!connection?.active_job_id}
        onClick={allowPrivateHome}>Allow private Home for at most five minutes</button>
      <button disabled={busy || !!pending || !privateReady || !selectedGoal || !!connection?.active_job_id
        || !connection?.consent?.actions?.includes("private_home") || connection.consent.session !== ownerSessionId
        || connection.consent.goal_id !== selectedGoal.id || connection.consent.goal_revision !== selectedGoal.revision}
        onClick={preparePrivateHome}>Prepare one private Chromium Home read</button>
      {!privateReady && <p role="status">Private Home blocked: production acceptance is unverified or a positive owner account inspection is required.</p>}
      <p>Response loss is Unknown; no Home replay, refresh or deadline renewal. Logout/revoke removes usable session authority. Original job inspection stays read-only; no_learning.</p>
    </section>
    <MoltbookWriteControls key={storageKey} connection={connection} goal={selectedGoal} job={job} busy={busy || !!pending} act={act} />
    {pending && <div role="status"><p>An exact original request remains retained for this login. Inspect its outcome before continuing.</p><button disabled={busy} onClick={() => void act()}>Retry exact retained request</button></div>}
    {output && <pre aria-label="Moltbook literal private output" className="whitespace-pre-wrap">{output}</pre>}
    {error && <p role="alert">{error}</p>}
    <p>No model call, heartbeat, voting, follow, private message, export, or learning is performed.</p>
  </section>;
}
