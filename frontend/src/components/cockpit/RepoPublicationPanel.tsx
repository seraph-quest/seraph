import { useEffect, useRef, useState } from "react";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import { publicationKey, publicationRequest, validatePublication, validatePublicationDiscovery } from "../../lib/repoPublication";
import type { PublicationReceipt } from "../../lib/repoPublication";
import type { WorkBoardRepoRepairProjection } from "../../types";

export function RepoPublicationPanel({ repair, ownerPrincipalId, ownerSessionId, onOpenApprovals }: { repair: WorkBoardRepoRepairProjection; ownerPrincipalId: string; ownerSessionId: string; onOpenApprovals?: () => void }) {
  const [connection, setConnection] = useState<{ repository: string; revision: number; scope: string; writable: boolean } | null>(null);
  const [receipt, setReceipt] = useState<PublicationReceipt | null>(null);
  const [patch, setPatch] = useState<string | null>(null);
  const [healthy, setHealthy] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState({ base_branch: "develop", expected_base_commit: "", branch_name: "feat/", commit_message: "", title: "", body: "" });
  const [prNumber, setPrNumber] = useState("");
  const [readAcknowledged, setReadAcknowledged] = useState(false);
  const [discovered, setDiscovered] = useState<PublicationReceipt[]>([]);
  const [nextOffset, setNextOffset] = useState<number | null>(null);
  const [discoveryOk, setDiscoveryOk] = useState(false);
  const [scanLimitReached, setScanLimitReached] = useState(false);
  const pending = useRef<Record<string, unknown> | null>(null);
  const scope = `${ownerPrincipalId}\0${ownerSessionId}\0${repair.job_id}`;
  const activeScope = useRef(scope);
  const generation = useRef(0);
  if (activeScope.current !== scope) { activeScope.current = scope; generation.current += 1; }
  const controllers = useRef(new Set<AbortController>());

  async function discover(offset = 0) {
    const expected = activeScope.current;
    const expectedGeneration = generation.current;
    const controller = new AbortController(); controllers.current.add(controller);
    try {
      const value = await publicationRequest(`/repairs/${encodeURIComponent(repair.job_id)}/jobs?offset=${offset}`, { method: "GET" }, controller.signal);
      const page = validatePublicationDiscovery(value, ownerPrincipalId, ownerSessionId, repair.job_id);
      if (activeScope.current !== expected || generation.current !== expectedGeneration) return;
      setDiscovered(previous => offset ? [...previous, ...page.jobs.filter(job => !previous.some(old => old.job_id === job.job_id))] : page.jobs);
      setNextOffset(page.nextOffset); setDiscoveryOk(true); setScanLimitReached(page.scanLimitReached);
      if (offset === 0 && page.jobs.length) { setReceipt(page.jobs[0]); setPatch(null); setReadAcknowledged(false); }
    } catch (e) {
      if (activeScope.current === expected && generation.current === expectedGeneration) {
        setDiscoveryOk(false); setError(e instanceof Error ? e.message : "Publication recovery discovery unavailable.");
      }
    } finally { controllers.current.delete(controller); }
  }

  async function metadata() {
    const expected = activeScope.current;
    const expectedGeneration = generation.current;
    const controller = new AbortController(); controllers.current.add(controller);
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await apiFetch(`${API_URL}/api/capabilities/github/connection`, { signal: controller.signal });
      const value = await response.json();
      if (!response.ok || typeof value.repository !== "string" || !Number.isInteger(value.revision) || value.credential_configured !== true) throw new Error("Configure the repository and credential in GitHub settings.");
      if (activeScope.current !== expected || generation.current !== expectedGeneration) return;
      const writable = value.mode === "active" && value.consent?.state === "active" && value.consent?.root_bound === true && ["github_git_objects_write", "github_new_branch_write", "github_ready_pr_write"].every(action => value.consent?.actions?.includes(action));
      setConnection({ repository: value.repository, revision: value.revision, scope: expected, writable }); setHealthy(true); setReadAcknowledged(false); setError(writable ? null : "Fresh publication consent is required in GitHub settings. Existing uncertain jobs can still be read back.");
    } catch (e) { if (activeScope.current === expected && generation.current === expectedGeneration) { setHealthy(false); setError(e instanceof Error ? e.message : "GitHub metadata unavailable."); } }
    finally { clearTimeout(timer); controllers.current.delete(controller); }
  }

  useEffect(() => {
    setReceipt(null); setPatch(null); setConnection(null); setHealthy(false); setBusy(false); setError(null); setReadAcknowledged(false); setDiscovered([]); setNextOffset(null); setDiscoveryOk(false); setScanLimitReached(false); pending.current = null;
    void metadata(); void discover();
    return () => { for (const controller of controllers.current) controller.abort(); controllers.current.clear(); };
    // This effect invalidates only actual owner/root/repair changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope]);

  async function act(action: "prepare" | "refresh" | "execute" | "cancel" | "reconcile") {
    const expected = activeScope.current;
    const expectedGeneration = generation.current;
    const controller = new AbortController(); controllers.current.add(controller);
    setBusy(true); setError(null);
    try {
      let body: Record<string, unknown> | undefined;
      let path = "/prepare";
      if (action === "prepare") {
        if (!pending.current) pending.current = { ...form, repair_job_id: repair.job_id, expected_repair_revision: repair.revision, proposal_id: repair.proposal?.proposal_id, expected_proposal_revision: repair.proposal?.revision, expected_connection_revision: connection?.revision, idempotency_key: publicationKey() };
        body = pending.current;
      } else {
        if (!receipt) throw new Error("Refresh the exact publication receipt first.");
        path = `/jobs/${encodeURIComponent(receipt.job_id)}${action === "refresh" ? "" : `/${action}`}`;
        if (action === "reconcile") body = { ...(prNumber.trim() ? { pr_number: Number(prNumber) } : {}), acknowledged_readback: readAcknowledged, expected_connection_revision: connection?.revision };
      }
      const value = await publicationRequest(path, { method: action === "refresh" ? "GET" : "POST", ...(body ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {}) }, controller.signal);
      const next = validatePublication(value, ownerPrincipalId, ownerSessionId, repair.job_id);
      if (activeScope.current !== expected || generation.current !== expectedGeneration) return;
      setReceipt(next); setHealthy(true); pending.current = null;
      if (!patch) {
        const patchValue = await publicationRequest(`/jobs/${encodeURIComponent(next.job_id)}/patch`, { method: "GET" }, controller.signal) as { patch?: unknown; patch_sha256?: unknown };
        if (typeof patchValue.patch !== "string" || patchValue.patch_sha256 !== next.preview.repair_binding.patch_sha256) throw new Error("Exact tested patch preview unavailable.");
        if (activeScope.current === expected && generation.current === expectedGeneration) setPatch(patchValue.patch);
      }
    } catch (e) { if (activeScope.current === expected && generation.current === expectedGeneration) { setHealthy(false); setError(e instanceof Error ? e.message : "Publication outcome unavailable; refresh or reconcile the exact job."); } }
    finally { controllers.current.delete(controller); if (activeScope.current === expected && generation.current === expectedGeneration) setBusy(false); }
  }

  const current = receipt?.preview.owner_session_id === ownerSessionId && receipt.preview.owner_principal_id === ownerPrincipalId && receipt.preview.repair_binding.repair_job_id === repair.job_id ? receipt : null;
  const boundConnection = connection?.scope === scope ? connection : null;
  const canExecute = Boolean(current && healthy && boundConnection?.writable && patch !== null && current.status === "awaiting_approval" && current.approval_status === "approved" && current.approval_expires_at && Date.parse(current.approval_expires_at) > Date.now());
  return <section aria-label="Publish tested repair" className="mt-3 rounded border border-white/10 p-3">
    <div className="font-semibold">Publish tested repair</div>
    <p className="mt-1">Local Git runs with host-user access. Publication requires a fresh approval for local execution, Git objects, a new branch and a ready PR.</p>
    <div>Repository: {boundConnection?.repository ?? "unavailable"}</div>
    {discovered.length > 0 && <label className="block">Existing publication<select aria-label="Existing publication" value={receipt?.job_id ?? ""} onChange={event => {
      const selected = discovered.find(job => job.job_id === event.target.value);
      if (selected) { setReceipt(selected); setPatch(null); setReadAcknowledged(false); }
    }}>{discovered.map(job => <option key={job.job_id} value={job.job_id}>{job.job_id} · {job.status.replace(/_/g, " ")}</option>)}</select></label>}
    {nextOffset !== null && <button type="button" disabled={busy} onClick={() => void discover(nextOffset)}>Load more existing publications</button>}
    {scanLimitReached && <p role="status">The bounded discovery limit was reached. Inspect the retained jobs before preparing another publication.</p>}
    {!current && <form onSubmit={(event) => { event.preventDefault(); void act("prepare"); }}>
      {Object.entries(form).map(([name, value]) => <label className="mt-2 block" key={name}>{name.replace(/_/g, " ")}<input className="cockpit-input block w-full" aria-label={name.replace(/_/g, " ")} value={value} disabled={busy || Boolean(pending.current)} onChange={(event) => setForm({ ...form, [name]: event.target.value })} /></label>)}
      <button type="submit" className="cockpit-feedback-button mt-2" disabled={busy || !discoveryOk || scanLimitReached || !healthy || !boundConnection?.writable || !repair.proposal}>Prepare exact publication preview</button>
    </form>}
    {current && <div className="mt-2">
      <div>{current.status.replace(/_/g, " ")} · {current.reason_code ?? "exact preview retained"}</div>
      <div>{current.preview.repository} · {current.preview.base_branch}@{current.preview.base_commit} → {current.preview.branch_name}</div>
      <div>Patch {current.preview.repair_binding.patch_sha256} · test input {current.preview.repair_binding.tested_input_digest}</div>
      <div>Repair executor {current.preview.repair_binding.repair_executor}; publication {current.preview.local_posture.profile}, isolation {current.preview.local_posture.isolation_claim}</div>
      <div>Tests: {current.preview.tested_input.test_args.join(" ")} · frozen environment {current.preview.tested_input.environment.runtime_binding}</div>
      <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap">{current.preview.title}{"\n"}{current.preview.body}</pre>
      <details className="mt-2"><summary>Exact tested patch</summary><pre className="max-h-64 overflow-auto whitespace-pre-wrap">{patch ?? "unavailable"}</pre></details>
      <div>Approval {current.approval_id} · {current.approval_status} · {current.approval_expires_at ?? "expiry unavailable"}</div>
      <div className="mt-2 flex flex-wrap gap-2">
        <button type="button" disabled={busy} onClick={() => void act("refresh")}>Refresh exact job</button>
        {current.approval_status === "pending" && onOpenApprovals && <button type="button" disabled={busy || !healthy || patch === null} onClick={onOpenApprovals}>Review local execution and remote publication approval</button>}
        {canExecute && <button type="button" disabled={busy} onClick={() => void act("execute")}>Execute approved publication</button>}
        {["awaiting_approval", "queued", "running"].includes(current.status) && <button type="button" disabled={busy || !healthy} onClick={() => void act("cancel")}>Cancel publication</button>}
      </div>
      {["unknown_external_effect", "blocked", "failed"].includes(current.status) && <div className="mt-2"><p>Remote outcome may be unknown. Reconciliation only reads the exact destination; it never republishes.</p><label>PR number (optional)<input aria-label="PR number" value={prNumber} onChange={(event) => setPrNumber(event.target.value)} /></label><label className="block"><input type="checkbox" checked={readAcknowledged} onChange={event => setReadAcknowledged(event.target.checked)} /> I authorize readback of this exact publication using the current connection revision.</label><button type="button" disabled={busy || !healthy || !readAcknowledged || (Boolean(prNumber) && !/^[1-9][0-9]*$/.test(prNumber))} onClick={() => void act("reconcile")}>Reconcile destination</button></div>}
      {current.status === "succeeded" && <div>Ready PR independently verified. Outcome artifact recorded; no learning.</div>}
    </div>}
    <button type="button" disabled={busy} onClick={() => void metadata()}>Refresh GitHub metadata</button>
    <button type="button" disabled={busy} onClick={() => void discover()}>Discover original repair publications</button>
    {error && <div role="alert" className="mt-2 text-amber-200">{error}</div>}
  </section>;
}
