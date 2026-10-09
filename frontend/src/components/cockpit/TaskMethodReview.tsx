import { useEffect, useRef, useState } from "react";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import { createGuardianUuid } from "../../lib/guardianInbox";
import type { GoalInfo, WorkBoardTask } from "../../types";
import { readProcedureParameters, procedureRequest, type ProcedureParameter } from "../../lib/reusableProcedures";
import { ReusableProcedureInvoke } from "./ReusableProcedureInvoke";
import { validateProvenance } from "../../lib/researchMethods";
import type { Observation } from "../../lib/researchMethods";

interface Preview {
  status?: string;
  proposal_id: string; expected_revision: number; artifact_digest: string; scope_digest: string;
  scope: { goal_id: string; goal_revision: number; family: string };
  old_method: Record<string, unknown> | null; new_method: Record<string, unknown> | null;
  tombstone?: { id: string; created_at: string };
  active_binding: { version: string; digest: string; proposal_id: string } | null;
  quality_evidence: "unmeasured"; adoption_requires_current_owner: boolean; configured_baseline: boolean;
  task_id: string; attempt_id: string; source_refs: string[]; observed: Observation;
  pointer_revision?: number | null; version?: string | null; digest?: string;
  parameters?: ProcedureParameter[]; source_receipt?: unknown; disable_scope?: string;
  family_history?: unknown[];
}
const object = (v: unknown): v is Record<string, unknown> => Boolean(v && typeof v === "object" && !Array.isArray(v));
const sha = (v: unknown): v is string => typeof v === "string" && /^[a-f0-9]{64}$/.test(v);
function preview(value: unknown, task: WorkBoardTask, proposalId: string, attemptId?: string, sourceRefs?: string[]): Preview {
  if (object(value) && value.status === "deleted") {
    if (value.proposal_id !== proposalId || value.task_id !== task.task_id || typeof value.attempt_id !== "string"
      || attemptId && value.attempt_id !== attemptId || !object(value.scope) || value.scope.goal_id !== task.goal_id || value.scope.goal_revision !== task.goal_revision
      || value.new_method !== null || value.old_method !== null || typeof value.version !== "string" || !sha(value.digest)
      || !object(value.tombstone) || typeof value.tombstone.id !== "string" || typeof value.tombstone.created_at !== "string"
      || typeof value.configured_baseline !== "boolean" || !(value.pointer_revision === null || Number.isInteger(value.pointer_revision) && Number(value.pointer_revision) > 0)) throw Error("Deleted method readback is unconfirmed. Inspect again.");
    return value as unknown as Preview;
  }
  if (!object(value) || value.proposal_id !== proposalId || !Number.isInteger(value.expected_revision) || Number(value.expected_revision) < 1
    || !sha(value.artifact_digest) || !sha(value.scope_digest) || !object(value.scope)
    || value.scope.goal_id !== task.goal_id || value.scope.goal_revision !== task.goal_revision
    || !object(value.new_method) || !["TaskMethod.v1", "ResearchStrategy.v1", "ProcedurePlan.v3"].includes(String(value.new_method.schema_version))
    || value.quality_evidence !== "unmeasured" || typeof value.adoption_requires_current_owner !== "boolean"
    || typeof value.configured_baseline !== "boolean" || !(value.old_method === null || object(value.old_method))
    || !(value.active_binding === null || object(value.active_binding) && typeof value.active_binding.version === "string"
      && typeof value.active_binding.proposal_id === "string" && sha(value.active_binding.digest))) throw Error("Method review does not match this task and Goal. Reload the exact candidate.");
  validateProvenance(value, task, attemptId, sourceRefs);
  if (value.new_method.schema_version === "ProcedurePlan.v3") {
    if (!object(value.new_method.plan) || Object.keys(value.new_method.plan).sort().join(",") !== "output_contract,parameters,permissions_digest,source_attempt,source_task_id,steps,tool_contract_versions"
      || value.new_method.plan.source_task_id !== task.task_id || value.new_method.plan.source_attempt !== value.attempt_id
      || !sha(value.digest) || !(value.pointer_revision === null || Number.isInteger(value.pointer_revision) && Number(value.pointer_revision) > 0)) throw Error("Reusable plan or pointer readback is unconfirmed. Inspect again.");
    const parameters = readProcedureParameters(value.parameters);
    if (JSON.stringify(parameters) !== JSON.stringify(value.new_method.plan.parameters)) throw Error("Method parameter readback changed. Inspect again.");
  }
  return value as unknown as Preview;
}
interface Props { task: WorkBoardTask; proposalId: string; owned: boolean; attemptId?: string; sourceRefs?: string[]; goals?: GoalInfo[]; onCreated?: (id: string) => Promise<void> }
export function TaskMethodReview(props: Props) {
  return <OwnedMethodReview key={`${props.task.task_id}:${props.task.task_revision}:${props.task.owner_principal_id}:${props.task.owner_session_id}:${props.proposalId}:${props.owned}`} {...props} />;
}
function OwnedMethodReview({ task, proposalId, owned, attemptId, sourceRefs, goals = [], onCreated }: Props) {
  const [data, setData] = useState<Preview | null>(null), [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false), [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [ack, setAck] = useState(false);
  const [deleteAck, setDeleteAck] = useState(false);
  const locked = useRef(false);
  const generation = useRef(0);
  const reusable = data?.new_method?.schema_version === "ProcedurePlan.v3";
  const current = data?.active_binding?.proposal_id === proposalId && data?.active_binding?.version === data?.version;
  useEffect(() => { ++generation.current; setData(null); setReason(""); setBusy(false); setError(null); setNotice(null); return () => { ++generation.current; }; }, [task.task_id, task.task_revision, task.owner_session_id, proposalId, owned]);
  async function inspect() {
    if (busy || locked.current || !owned) return;
    locked.current = true; setAck(false); setDeleteAck(false);
    const version = generation.current; setBusy(true); setError(null); setNotice(null); setData(null);
    try {
      const response = await apiFetch(`${API_URL}/api/memory/task-methods/${encodeURIComponent(proposalId)}`);
      if (!response.ok) throw Error("Method inspection is blocked. Restore current ownership and inspect again.");
      const readback = preview(await response.json(), task, proposalId, attemptId, sourceRefs);
      if (version === generation.current) setData(readback);
    } catch (e) { if (version === generation.current) { setData(null); setError((e as Error).message); } }
    finally { locked.current = false; if (version === generation.current) setBusy(false); }
  }
  async function act(action: "accept" | "reject" | "rollback" | "disable" | "activate" | "delete") {
    if (!owned || busy || locked.current || !data || data.adoption_requires_current_owner || action === "accept" && !ack || action === "rollback" && !reason.trim()) return;
    if (action === "delete" && !deleteAck || ["disable", "activate", "delete"].includes(action) && !reason.trim()) return;
    locked.current = true; setAck(false); setDeleteAck(false);
    const version = generation.current; setBusy(true); setError(null);
    try {
      await procedureRequest("/api/memory/task-methods/actions", {
        proposal_id: data.proposal_id, expected_revision: data.expected_revision, artifact_digest: data.artifact_digest,
        scope_digest: data.scope_digest, action, reason, idempotency_key: createGuardianUuid(),
      });
      const readback = data.new_method?.schema_version === "ProcedurePlan.v3"
        ? preview(await procedureRequest(`/api/memory/task-methods/${encodeURIComponent(proposalId)}`), task, proposalId, attemptId, sourceRefs) : null;
      if (version === generation.current) { setData(readback); setReason(""); setNotice(readback ? "Review recorded; fresh canonical binding read back." : "Review recorded. Inspect the canonical method again for its current binding."); }
    } catch (e) { if (version === generation.current) { setData(null); setError((e as Error).message); } }
    finally { locked.current = false; if (version === generation.current) setBusy(false); }
  }
  return <section aria-label="Canonical task method review" className="mt-3">
    <button type="button" disabled={!owned || busy} onClick={() => void inspect()}>Inspect canonical method and scope</button>
    <p>Quality is unmeasured. Adoption affects new tasks in this exact Goal and family. Each tool still requires its original permissions and approvals.</p>
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    {data?.status === "deleted" && <section aria-label="Deleted canonical method"><p role="status">Canonical method deleted. Source Tasks, artifacts and audit remain; this version cannot invoke or pass its next execution boundary.</p>
      <p>Version {data.version} · digest {data.digest} · pointer revision {data.pointer_revision} · tombstone {data.tombstone?.id} · {data.tombstone?.created_at}</p>
      <p>{data.configured_baseline ? "Future general Tasks use explicit baseline." : "Current family selection remains on another method."}</p>
      <pre aria-label="Retained deleted method history">{JSON.stringify(data.family_history, null, 2)}</pre>
    </section>}
    {data?.new_method && <>
      <p>Goal {data.scope.goal_id} revision {data.scope.goal_revision} · family {data.scope.family} · proposal revision {data.expected_revision}</p>
      <p className="break-all">Artifact {data.artifact_digest} · reviewed scope {data.scope_digest}</p>
      <section aria-label="Canonical method source provenance"><p>Source Task {data.task_id} · Attempt {data.attempt_id} · observation {data.observed.status}</p><p className="break-all">{"readback_digest" in data.observed ? `Verified readback ${data.observed.readback_digest}` : `Failure receipt ${data.observed.failure_reason_digest}`}</p><ul>{data.source_refs.map(ref => <li key={ref} className="font-mono break-all">{ref}</li>)}</ul></section>
      <pre aria-label="Canonical proposed method">{JSON.stringify(data.new_method, null, 2)}</pre>
      <p>{data.active_binding ? `Active version ${data.active_binding.version} · ${data.active_binding.digest}` : data.configured_baseline ? "Future tasks use configured baseline." : "No method is selected."}</p>
      <p>{data.new_method.schema_version === "ProcedurePlan.v3" ? "Rollback restores the exact previous signed method or explicit baseline." : "Rollback selects baseline for future tasks."} Already admitted tasks keep their immutable pin while current authority remains valid; revocation or tombstone blocks their next boundary.</p>
      {data.adoption_requires_current_owner && <p role="status">Inspection only: current original ownership is required for adoption.</p>}
      <label>Review reason<textarea maxLength={500} value={reason} onChange={e => setReason(e.target.value)} /></label>
      <label><input type="checkbox" disabled={busy || data.adoption_requires_current_owner} checked={ack} onChange={e => setAck(e.target.checked)} />I reviewed this exact method and verified source evidence.</label>
      <button type="button" disabled={busy || data.adoption_requires_current_owner || !ack || reusable && data.status !== "proposed"} onClick={() => void act("accept")}>Adopt reviewed method</button>
      <button type="button" disabled={busy || data.adoption_requires_current_owner || reusable && data.status !== "proposed"} onClick={() => void act("reject")}>Reject method</button>
      <button type="button" disabled={busy || data.adoption_requires_current_owner || !reason.trim() || reusable && !current} onClick={() => void act("rollback")}>{data.new_method.schema_version === "ProcedurePlan.v3" ? "Restore exact previous method or baseline" : "Rollback to baseline"}</button>
      {data.new_method.schema_version === "ProcedurePlan.v3" && <>
        <p>Exact version {data.version ?? "not adopted"} · digest {data.digest} · pointer revision {data.pointer_revision ?? "absent"}</p>
        <pre aria-label="Reusable method source receipt">{JSON.stringify(data.source_receipt, null, 2)}</pre>
        <pre aria-label="General-task method history">{JSON.stringify(data.family_history, null, 2)}</pre>
        {onCreated && data.family_history?.map((entry, index) => object(entry) && typeof entry.task_id === "string" && typeof entry.proposal_id === "string"
          ? <button key={`${entry.proposal_id}:${index}`} disabled={busy} onClick={() => void onCreated(entry.task_id as string)}>Open source Task for method {entry.proposal_id}</button> : null)}
        <p>Disable scope: {data.disable_scope}. Disabling selects baseline for future general Tasks and retains history.</p>
        <button disabled={busy || data.adoption_requires_current_owner || !reason.trim() || !current} onClick={() => void act("disable")}>Disable general-task method selection</button>
        <button disabled={busy || data.adoption_requires_current_owner || !reason.trim() || !data.configured_baseline || data.status !== "accepted"} onClick={() => void act("activate")}>Activate exact reviewed prior method</button>
        <label><input type="checkbox" checked={deleteAck} disabled={busy} onChange={e => setDeleteAck(e.target.checked)} />Tombstone this exact canonical method version. Its next execution boundary will stop; source Tasks, artifacts and audit remain.</label>
        <button disabled={busy || data.adoption_requires_current_owner || !reason.trim() || !deleteAck || !data.version || data.status !== "accepted"} onClick={() => void act("delete")}>Delete canonical method version</button>
        {onCreated && data.active_binding?.proposal_id === proposalId && data.active_binding.version === data.version && data.active_binding.digest === data.digest && data.pointer_revision && !data.adoption_requires_current_owner && <ReusableProcedureInvoke
          key={`${data.active_binding.version}:${data.pointer_revision}`} proposalId={proposalId} version={data.active_binding.version} digest={data.active_binding.digest} pointerRevision={data.pointer_revision}
          parameters={data.parameters ?? []} goals={goals} scopeGoalId={data.scope.goal_id} scopeGoalRevision={data.scope.goal_revision} ownerSessionId={task.owner_session_id} onCreated={onCreated} onStale={() => setError("Invocation blocked. Inspect fresh canonical state before retrying.")} />}
      </>}
    </>}
  </section>;
}
