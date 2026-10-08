import { useEffect, useRef, useState } from "react";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import type { GoalInfo } from "../../types";

type Kind = "fill" | "select" | "check" | "click" | "extract" | "wait";
interface Profile { id: string; url: string; name: string; read_effect: string; preparation: string; submission: string; max_actions: number; max_runtime_seconds: number; private_field_max_bytes: number }
interface CleanupCandidate { job_id: string; durable_status: string; physical_proof_state: "owned_positive_close" | "owned_no_child" | "linux_boot_changed" | "darwin_boot_changed" | "host_reboot_required" | "boot_proof_unavailable" }
interface Snapshot { capability_id: "browser.interact.v2"; job_id: string; revision: number; fencing_token: number; status: string;
  no_learning: true; live: boolean; recovery: string; blocked_reason?: string;
  page: { url: string; origin: string; document_digest: string; captured_at: string;
    accessible_nodes: { node_id: string; role: string; name: string; actions: Kind[] }[] } | null;
  history: { sequence: number; kind: string; status: string; reason?: string; phase?: string; locator_ref?: string }[];
  preview?: { field: string; value: string; checked: boolean }[] }
const record = (v: unknown): v is Record<string, unknown> => Boolean(v && typeof v === "object" && !Array.isArray(v));
const usedActions = (job: Snapshot) => job.history.filter(h => h.status === "intent" && h.kind !== "document").length;
async function request(path: string, body?: unknown): Promise<unknown> {
  const response = await apiFetch(`${API_URL}/api/capabilities/browser-interactions${path}`, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!response.ok) {
    const payload: unknown = await response.json().catch(() => null);
    const code = record(payload) && record(payload.detail) && typeof payload.detail.code === "string" ? payload.detail.code : "browser_operation_blocked";
    throw Error(`Browser operation blocked: ${code}. Inspect the original job history before taking another action.`);
  }
  return response.json();
}
function readSnapshot(value: unknown, expectedId?: string): Snapshot {
  if (!record(value) || value.capability_id !== "browser.interact.v2" || typeof value.job_id !== "string" || (expectedId && value.job_id !== expectedId)
    || !Number.isSafeInteger(value.revision) || !Number.isSafeInteger(value.fencing_token) || value.no_learning !== true || typeof value.live !== "boolean"
    || typeof value.status !== "string" || typeof value.recovery !== "string" || !Array.isArray(value.history)
    || value.history.some(h => !record(h) || typeof h.kind !== "string" || typeof h.status !== "string" || !Number.isSafeInteger(h.sequence))) throw Error("Browser job readback does not match the original job.");
  if (value.page != null && (!record(value.page) || typeof value.page.url !== "string" || typeof value.page.origin !== "string" || typeof value.page.document_digest !== "string"
    || !/^[a-f0-9]{64}$/.test(value.page.document_digest) || typeof value.page.captured_at !== "string" || !Number.isFinite(Date.parse(value.page.captured_at)) || !Array.isArray(value.page.accessible_nodes)
    || value.page.accessible_nodes.some(n => !record(n) || typeof n.node_id !== "string" || !/^node-[0-9a-f]{32}$/.test(n.node_id) || typeof n.role !== "string" || typeof n.name !== "string" || !Array.isArray(n.actions)))) throw Error("Browser snapshot locators are unavailable. Inspect the job again.");
  if (value.preview !== undefined && (!Array.isArray(value.preview) || value.preview.some(v => !record(v) || typeof v.field !== "string" || typeof v.value !== "string" || typeof v.checked !== "boolean"))) throw Error("Private form preview is invalid.");
  return { ...value, page: value.page ?? null } as unknown as Snapshot;
}
export function BrowserInteractionPanel({ goals, ownerPrincipalId, ownerSessionId }: { goals: GoalInfo[]; ownerPrincipalId?: string | null; ownerSessionId?: string | null }) {
  const [profiles, setProfiles] = useState<Profile[]>([]), [profileId, setProfileId] = useState("");
  const [profileBlocked, setProfileBlocked] = useState<string | null>(null);
  const [goalId, setGoalId] = useState(""), [ack, setAck] = useState(false);
  const [job, setJob] = useState<Snapshot | null>(null), [error, setError] = useState<string | null>(null), [busy, setBusy] = useState(false);
  const [locator, setLocator] = useState(""), [kind, setKind] = useState<Kind>("fill"), [value, setValue] = useState("");
  const [actionAck, setActionAck] = useState(false), [actionUncertain, setActionUncertain] = useState(false);
  const [pendingCreate, setPendingCreate] = useState<Record<string, unknown> | null>(null);
  const [retainedJobs, setRetainedJobs] = useState<Snapshot[]>([]), [hasMore, setHasMore] = useState(false);
  const [cleanupCandidates, setCleanupCandidates] = useState<CleanupCandidate[]>([]), [cleanupAck, setCleanupAck] = useState(false);
  const [cleanupReceipt, setCleanupReceipt] = useState<string | null>(null);
  const generation = useRef(0);
  const owned = Boolean(ownerPrincipalId && ownerSessionId);
  const activeGoals = goals.filter(g => g.status === "active" && g.revision && g.owner_session_id === ownerSessionId && g.ownership_access !== "recovered_read_only");
  const goal = activeGoals.find(g => g.id === goalId), profile = profiles.find(p => p.id === profileId);
  const node = job?.page?.accessible_nodes.find(n => n.node_id === locator);
  const exhausted = Boolean(job && usedActions(job) >= (profile?.max_actions ?? 20));
  async function refreshProfiles() {
    if (!owned || busy) return;
    const version = generation.current; setBusy(true); setError(null);
    try {
      const result = await request("/profiles");
      if (!record(result) || result.capability_id !== "browser.interact.v2" || !Array.isArray(result.profiles)
        || result.profiles.some(p => !record(p) || typeof p.id !== "string" || typeof p.name !== "string" || typeof p.url !== "string" || typeof p.read_effect !== "string" || typeof p.private_field_max_bytes !== "number")) throw Error("Reviewed browser profiles are unavailable.");
      if (version === generation.current) { setProfiles(result.profiles as Profile[]); setProfileBlocked(typeof result.blocked_reason === "string" ? result.blocked_reason : null); setAck(false); }
    } catch (e) { if (version === generation.current) setError((e as Error).message); }
    finally { if (version === generation.current) setBusy(false); }
  }
  useEffect(() => {
    const version = ++generation.current; setProfiles([]); setProfileBlocked(null); setJob(null); setRetainedJobs([]); setHasMore(false); setValue(""); setError(null); setBusy(false); setAck(false); setActionAck(false); setPendingCreate(null); setActionUncertain(false); setCleanupCandidates([]); setCleanupAck(false); setCleanupReceipt(null);
    if (owned) void request("/profiles").then(result => {
      if (!record(result) || result.capability_id !== "browser.interact.v2" || !Array.isArray(result.profiles)
        || result.profiles.some(p => !record(p) || typeof p.id !== "string" || typeof p.name !== "string" || typeof p.url !== "string" || typeof p.read_effect !== "string" || typeof p.private_field_max_bytes !== "number")) throw Error("Reviewed browser profiles are unavailable.");
      if (version === generation.current) { setProfiles(result.profiles as Profile[]); setProfileBlocked(typeof result.blocked_reason === "string" ? result.blocked_reason : null); }
    }).catch(e => { if (version === generation.current) setError((e as Error).message); });
    return () => { ++generation.current; };
  }, [ownerPrincipalId, ownerSessionId, owned]);
  async function create() {
    if (!owned || busy || (!pendingCreate && (profileBlocked || !profile || !goal?.revision || !ack))) return;
    const version = generation.current;
    const body = pendingCreate ?? { profile_id: profile!.id, goal_id: goal!.id, goal_revision: goal!.revision, request_key: crypto.randomUUID(), read_ack: true };
    setPendingCreate(body); setBusy(true); setError(null);
    try { const result = readSnapshot(await request("/jobs", body)); if (version === generation.current) { setJob(result); setPendingCreate(null); setActionUncertain(false); } }
    catch (e) { if (version === generation.current) setError((e as Error).message); }
    finally { if (version === generation.current) setBusy(false); }
  }
  async function discoverJobs() {
    if (!owned || busy) return;
    const version = generation.current; setBusy(true); setError(null);
    try {
      const result = await request("/jobs");
      if (!record(result) || !Array.isArray(result.jobs) || result.jobs.length > 20 || typeof result.has_more !== "boolean") throw Error("Original browser job discovery is unavailable.");
      const receipts = result.jobs.map(v => readSnapshot(v));
      if (version === generation.current) { setRetainedJobs(receipts); setHasMore(result.has_more); }
    } catch (e) { if (version === generation.current) setError((e as Error).message); }
    finally { if (version === generation.current) setBusy(false); }
  }
  async function inspect() {
    if (!job || busy) return;
    const version = generation.current; setBusy(true); setError(null); setValue(""); setActionAck(false); setLocator(""); setJob(current => current ? { ...current, preview: undefined } : current);
    try { const result = readSnapshot(await request(`/jobs/${encodeURIComponent(job.job_id)}`), job.job_id); if (version === generation.current) setJob(result); }
    catch (e) { if (version === generation.current) setError((e as Error).message); }
    finally { if (version === generation.current) setBusy(false); }
  }
  async function discoverCleanup() {
    if (!owned || busy) return;
    const version = generation.current; setBusy(true); setError(null); setCleanupAck(false); setCleanupReceipt(null);
    try {
      const result = await request("/jobs/cleanup-candidates");
      if (!record(result) || !Array.isArray(result.candidates) || result.candidates.length > 20
        || result.candidates.some(c => !record(c) || typeof c.job_id !== "string" || typeof c.durable_status !== "string"
          || !["owned_positive_close", "owned_no_child", "linux_boot_changed", "darwin_boot_changed", "host_reboot_required", "boot_proof_unavailable"].includes(String(c.physical_proof_state)))) throw Error("Physical browser cleanup candidates are unavailable.");
      if (version === generation.current) setCleanupCandidates(result.candidates as CleanupCandidate[]);
    } catch (e) { if (version === generation.current) setError((e as Error).message); }
    finally { if (version === generation.current) setBusy(false); }
  }
  async function reconcileCleanup(candidate: CleanupCandidate) {
    if (!owned || busy || !cleanupAck || !["owned_positive_close", "owned_no_child", "linux_boot_changed", "darwin_boot_changed"].includes(candidate.physical_proof_state)) return;
    const version = generation.current; setBusy(true); setError(null); setCleanupAck(false); setValue(""); setLocator(""); setActionAck(false);
    setJob(current => current ? { ...current, preview: undefined } : current);
    try {
      const result = await request(`/jobs/${encodeURIComponent(candidate.job_id)}/cleanup`, { cleanup_ack: true });
      if (!record(result) || result.job_id !== candidate.job_id || !Number.isSafeInteger(result.revision) || typeof result.status !== "string"
        || !record(result.receipt) || result.receipt.scope !== "physical_cleanup_only" || result.receipt.no_learning !== true
        || !["owned_positive_close", "owned_no_child", "linux_boot_changed", "darwin_boot_changed"].includes(String(result.receipt.proof_kind))) throw Error("Exact physical cleanup receipt is unavailable. Find the original cleanup candidate again.");
      if (version === generation.current) { setCleanupCandidates(current => current.filter(c => c.job_id !== candidate.job_id)); setCleanupReceipt(`Job ${candidate.job_id}: physical cleanup recorded; original durable status remains ${result.status}.`); }
    } catch (e) { if (version === generation.current) setError((e as Error).message); }
    finally { if (version === generation.current) setBusy(false); }
  }
  async function refreshSnapshot() {
    if (!owned || !job?.live || !job.page || busy || exhausted) return;
    const version = generation.current, jobId = job.job_id;
    setBusy(true); setError(null); setActionUncertain(true); setLocator(""); setValue(""); setActionAck(false);
    setJob(current => current ? { ...current, preview: undefined } : current);
    try {
      // Cached inspection supplies the current job fence, never fresh DOM evidence.
      const current = readSnapshot(await request(`/jobs/${encodeURIComponent(jobId)}`), jobId);
      if (version !== generation.current) return;
      setJob(current);
      if (!current.live || !current.page || usedActions(current) >= (profile?.max_actions ?? 20)) throw Error("The original browser context has no remaining snapshot action capacity.");
      const fresh = readSnapshot(await request(`/jobs/${encodeURIComponent(jobId)}/snapshot`, {
        expected_revision: current.revision, fencing_token: current.fencing_token,
      }), jobId);
      const last = fresh.history[fresh.history.length - 1];
      const oldNodes = new Set(current.page.accessible_nodes.map(n => n.node_id));
      if (!fresh.live || !fresh.page || fresh.revision <= current.revision || fresh.fencing_token !== current.fencing_token
        || last?.kind !== "snapshot" || last.status !== "completed"
        || fresh.page.accessible_nodes.some(n => oldNodes.has(n.node_id))) throw Error("Fresh browser snapshot evidence is unavailable. Inspect the original job history.");
      if (version === generation.current) { setJob(fresh); setActionUncertain(false); }
    } catch (e) { if (version === generation.current) setError((e as Error).message); }
    finally { if (version === generation.current) setBusy(false); }
  }
  async function action() {
    if (!job?.live || !job.page || busy || exhausted || !actionAck || actionUncertain || (["fill", "select", "check", "click"].includes(kind) && (!node || !node.actions.includes(kind)))) return;
    const version = generation.current; setBusy(true); setError(null); setActionAck(false); setJob(current => current ? { ...current, preview: undefined } : current);
    const privateValue = kind === "check" ? value === "true" : value;
    if (new TextEncoder().encode(String(privateValue)).length > (profile?.private_field_max_bytes ?? 2048)) { setBusy(false); setError("Private field exceeds the reviewed 2048-byte bound."); return; }
    const usesInput = ["fill", "select", "check"].includes(kind);
    const body = { expected_revision: job.revision, fencing_token: job.fencing_token, action: { kind,
      expected_page_revision: job.page.document_digest, ...(["fill", "select", "check", "click"].includes(kind) ? { locator_ref: locator } : {}),
      ...(usesInput ? { input_value_ref: "value-" + crypto.randomUUID().replace(/-/g, "") } : {}) }, ...(usesInput ? { private_input: privateValue } : {}) };
    try { const result = readSnapshot(await request(`/jobs/${encodeURIComponent(job.job_id)}/actions`, body), job.job_id); if (version === generation.current) { setJob(result); setLocator(""); setValue(""); } }
    catch (e) { if (version === generation.current) { setActionUncertain(true); setValue(""); setError((e as Error).message); } }
    finally { if (version === generation.current) setBusy(false); }
  }
  async function close() {
    if (!job || busy) return;
    const version = generation.current; setBusy(true); setError(null); setValue(""); setJob(current => current ? { ...current, preview: undefined } : current);
    try { const result = readSnapshot(await request(`/jobs/${encodeURIComponent(job.job_id)}/close`, { expected_revision: job.revision, fencing_token: job.fencing_token }), job.job_id); if (version === generation.current) { setJob(result); setActionUncertain(false); } }
    catch (e) { if (version === generation.current) { setActionUncertain(true); setError((e as Error).message); } }
    finally { if (version === generation.current) setBusy(false); }
  }
  return <section aria-label="Reviewed public browser interaction" className="rounded border border-white/10 p-3 text-xs">
    <h3>Public form preparation</h3><p>The fixed reviewed public profile permits private, offline preparation after one public page read. Submission remains blocked until exact effect authority exists. No authenticated browser or arbitrary URL, selector or script.</p>
    {error && <p role="alert" className="text-amber-200">{error}</p>}
    {profileBlocked && <p role="status">Browser preparation blocked: {profileBlocked}.{profileBlocked === "browser_operator_continuity_required" && " Open Operator ownership and recovery, select Enroll this authenticated scope, then refresh reviewed browser profiles. Enrollment binds recovery of the original physical browser."}</p>}
    <button type="button" disabled={busy || !owned} onClick={() => void discoverJobs()}>Find original browser jobs</button>
    <button type="button" disabled={busy || !owned} onClick={() => void discoverCleanup()}>Find physical browser cleanup</button>
    {cleanupCandidates.length > 0 && <div role="region" aria-label="Physical browser cleanup">
      <p>Cleanup releases only the original physical browser reservation. It does not resolve an external outcome, adopt a preview, reopen a page or renew the original Goal.</p>
      <label><input type="checkbox" checked={cleanupAck} onChange={e => setCleanupAck(e.target.checked)} />Approve physical cleanup only for the displayed original browser job.</label>
      <ul>{cleanupCandidates.map(c => <li key={c.job_id}>{c.job_id} · {c.durable_status} · {c.physical_proof_state}
        {c.physical_proof_state === "host_reboot_required" && <p>The lost owner cannot prove cleanup on this host boot. An operator-managed reboot is required before this cleanup proof can be accepted.</p>}
        {c.physical_proof_state === "boot_proof_unavailable" && <p>A verified native boot proof is unavailable. Cleanup stays blocked until the native kernel boot witness can be verified.</p>}
        <button type="button" disabled={busy || !owned || !cleanupAck || !["owned_positive_close", "owned_no_child", "linux_boot_changed", "darwin_boot_changed"].includes(c.physical_proof_state)} onClick={() => void reconcileCleanup(c)}>Record physical cleanup for {c.job_id}</button>
      </li>)}</ul>
    </div>}
    {cleanupReceipt && <p role="status">{cleanupReceipt}</p>}
    {retainedJobs.length > 0 && <ul aria-label="Original browser jobs">{retainedJobs.map(receipt => <li key={receipt.job_id}><button type="button" disabled={busy} onClick={() => { setJob(receipt); setActionAck(false); setAck(false); setLocator(""); setValue(""); setPendingCreate(null); setActionUncertain(receipt.live); }}>{receipt.job_id} · {receipt.status} · {receipt.live ? "live" : "read-only history"}</button></li>)}</ul>}
    {hasMore && <p>The latest 20 original jobs are shown. Older receipts remain in the canonical audit history.</p>}
    {!job && <>
      <button type="button" disabled={busy || !owned || Boolean(pendingCreate)} onClick={() => void refreshProfiles()}>Refresh reviewed browser profiles</button>
      <fieldset disabled={busy || !owned || Boolean(profileBlocked) || Boolean(pendingCreate)}>
        <label>Reviewed browser profile<select aria-label="Reviewed browser profile" value={profileId} onChange={e => { setProfileId(e.target.value); setAck(false); }}><option value="">Choose reviewed profile</option>{profiles.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
        <label>Interaction Goal<select aria-label="Interaction Goal" value={goalId} onChange={e => { setGoalId(e.target.value); setAck(false); }}><option value="">Choose current Goal</option>{activeGoals.map(g => <option key={g.id} value={g.id}>{g.title} · revision {g.revision}</option>)}</select></label>
        {profile && <p>{profile.url} · {profile.read_effect} · {profile.max_actions} actions · {profile.max_runtime_seconds}s · {profile.preparation} preparation · {profile.submission}</p>}
        <label><input type="checkbox" checked={ack} onChange={e => setAck(e.target.checked)} />Approve this exact public page contact for this Goal. Private prepared fields remain local and do not approve submission.</label>
      </fieldset>
      <button type="button" disabled={busy || !owned || (!pendingCreate && (Boolean(profileBlocked) || !profile || !goal || !ack))} onClick={() => void create()}>{pendingCreate ? "Reconcile original browser read" : "Open reviewed public page"}</button>
    </>}
    {job && <>
      <p role="status">{job.status} · {job.live ? "live private context" : "read-only history"} · {job.blocked_reason ?? job.recovery} · no_learning</p>
      <p>Job {job.job_id} · revision {job.revision}</p><button type="button" disabled={busy} onClick={() => void inspect()}>Inspect original browser history</button>
      <button type="button" disabled={busy || !owned || !job.live || !job.page || exhausted} onClick={() => void refreshSnapshot()}>Refresh current page snapshot</button>
      <button type="button" disabled={busy || !job.live} onClick={() => void close()}>Close private browser context</button>
      {job.page && <>
        <p>{job.page.origin} · snapshot {job.page.document_digest}</p>
        <fieldset disabled={busy || !job.live || actionUncertain || exhausted}>
          <label>Snapshot field<select aria-label="Snapshot field" value={locator} onChange={e => { setLocator(e.target.value); setValue(""); setActionAck(false); }}><option value="">Choose current accessible node</option>{job.page.accessible_nodes.map(n => <option key={n.node_id} value={n.node_id}>{n.role}: {n.name}</option>)}</select></label>
          <label>Browser action<select aria-label="Browser action" value={kind} onChange={e => { setKind(e.target.value as Kind); setValue(""); setActionAck(false); }}>{["fill", "select", "check", "click", "extract", "wait"].map(k => <option key={k} value={k} disabled={!["extract", "wait"].includes(k) && !node?.actions.includes(k as Kind)}>{k}</option>)}</select></label>
          {["fill", "select"].includes(kind) && <label>Private prepared value<input aria-label="Private prepared value" value={value} maxLength={2048} onChange={e => { setValue(e.target.value); setActionAck(false); }} /></label>}
          {kind === "check" && <label><input type="checkbox" checked={value === "true"} onChange={e => { setValue(String(e.target.checked)); setActionAck(false); }} />Prepared checked value</label>}
          <p>Preview: {kind} · {node?.role ?? "page"} {node?.name ?? "snapshot"} · current snapshot only. {kind === "click" ? "Click may require separate exact effect approval and remains blocked without it." : "Preparation remains offline and private."}</p>
          <label><input type="checkbox" checked={actionAck} onChange={e => setActionAck(e.target.checked)} />Review this exact bounded action against the current snapshot.</label>
          <button type="button" disabled={!actionAck || (["fill", "select", "check", "click"].includes(kind) && !node?.actions.includes(kind))} onClick={() => void action()}>Run reviewed browser action</button>
        </fieldset>
      </>}
      {actionUncertain && <p role="status">The action outcome requires inspection. No action is replayed automatically; inspect the original history and fresh snapshot before continuing.</p>}
      {exhausted && <p role="status">The 20-action bound is exhausted. History inspection and context cleanup remain available.</p>}
      {!job.live && <p>The original context is unavailable. This receipt permits history inspection only; a new public read requires fresh explicit approval.</p>}
      {!job.live && <button type="button" disabled={busy} onClick={() => { setJob(null); setAck(false); setActionAck(false); setLocator(""); setValue(""); setPendingCreate(null); setActionUncertain(false); }}>Start a new explicitly approved browser read</button>}
      {job.preview && <div role="region" aria-label="Private prepared form preview"><h4>Private form preview</h4><ul>{job.preview.map((v, i) => <li key={i}>{v.field}: {v.value} · checked {String(v.checked)}</li>)}</ul></div>}
      <ol aria-label="Browser action history">{job.history.map(h => <li key={h.sequence}>{h.sequence} · {h.kind} · {h.status} · {h.phase ?? ""}{h.reason ? ` · ${h.reason}` : ""}</li>)}</ol>
    </>}
  </section>;
}
