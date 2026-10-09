import { useEffect, useRef, useState } from "react";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import { createGuardianUuid } from "../../lib/guardianInbox";
import type { WorkBoardTask } from "../../types";

interface Preview {
  proposal_id: string; expected_revision: number; artifact_digest: string; scope_digest: string;
  scope: { goal_id: string; goal_revision: number; family: string };
  old_method: Record<string, unknown> | null; new_method: Record<string, unknown>;
  active_binding: { version: string; digest: string; proposal_id: string } | null;
  quality_evidence: "unmeasured"; adoption_requires_current_owner: boolean; configured_baseline: boolean;
}
const object = (v: unknown): v is Record<string, unknown> => Boolean(v && typeof v === "object" && !Array.isArray(v));
const sha = (v: unknown): v is string => typeof v === "string" && /^[a-f0-9]{64}$/.test(v);
function preview(value: unknown, task: WorkBoardTask, proposalId: string): Preview {
  if (!object(value) || value.proposal_id !== proposalId || !Number.isInteger(value.expected_revision) || Number(value.expected_revision) < 1
    || !sha(value.artifact_digest) || !sha(value.scope_digest) || !object(value.scope)
    || value.scope.goal_id !== task.goal_id || value.scope.goal_revision !== task.goal_revision
    || !object(value.new_method) || !["TaskMethod.v1", "ResearchStrategy.v1"].includes(String(value.new_method.schema_version))
    || value.quality_evidence !== "unmeasured" || typeof value.adoption_requires_current_owner !== "boolean"
    || typeof value.configured_baseline !== "boolean" || !(value.old_method === null || object(value.old_method))
    || !(value.active_binding === null || object(value.active_binding) && typeof value.active_binding.version === "string"
      && typeof value.active_binding.proposal_id === "string" && sha(value.active_binding.digest))) throw Error("Method review does not match this task and Goal. Reload the exact candidate.");
  return value as unknown as Preview;
}
export function TaskMethodReview({ task, proposalId, owned }: { task: WorkBoardTask; proposalId: string; owned: boolean }) {
  const [data, setData] = useState<Preview | null>(null), [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false), [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const generation = useRef(0);
  useEffect(() => { ++generation.current; setData(null); setReason(""); setBusy(false); setError(null); setNotice(null); return () => { ++generation.current; }; }, [task.task_id, task.task_revision, task.owner_session_id, proposalId, owned]);
  async function inspect() {
    if (busy || !owned) return;
    const version = generation.current; setBusy(true); setError(null); setNotice(null);
    try {
      const response = await apiFetch(`${API_URL}/api/memory/task-methods/${encodeURIComponent(proposalId)}`);
      if (!response.ok) throw Error("Method inspection is blocked. Restore current ownership and inspect again.");
      const readback = preview(await response.json(), task, proposalId);
      if (version === generation.current) setData(readback);
    } catch (e) { if (version === generation.current) { setData(null); setError((e as Error).message); } }
    finally { if (version === generation.current) setBusy(false); }
  }
  async function act(action: "accept" | "reject" | "rollback") {
    if (!owned || busy || !data || data.adoption_requires_current_owner || action === "rollback" && !reason.trim()) return;
    const version = generation.current; setBusy(true); setError(null);
    try {
      const response = await apiFetch(`${API_URL}/api/memory/task-methods/actions`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
        proposal_id: data.proposal_id, expected_revision: data.expected_revision, artifact_digest: data.artifact_digest,
        scope_digest: data.scope_digest, action, reason, idempotency_key: createGuardianUuid(),
      }) });
      if (!response.ok) throw Error("Method or scope changed. Inspect again before another review action.");
      await response.json();
      if (version === generation.current) { setData(null); setReason(""); setNotice("Review recorded. Inspect the canonical method again for its current binding."); }
    } catch (e) { if (version === generation.current) { setData(null); setError((e as Error).message); } }
    finally { if (version === generation.current) setBusy(false); }
  }
  return <section aria-label="Canonical task method review" className="mt-3">
    <button type="button" disabled={!owned || busy} onClick={() => void inspect()}>Inspect canonical method and scope</button>
    <p>Quality is unmeasured. Adoption affects new tasks in this exact Goal and family. Each tool still requires its original permissions and approvals.</p>
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    {data && <>
      <p>Goal {data.scope.goal_id} revision {data.scope.goal_revision} · family {data.scope.family} · proposal revision {data.expected_revision}</p>
      <p className="break-all">Artifact {data.artifact_digest} · reviewed scope {data.scope_digest}</p>
      <pre aria-label="Canonical proposed method">{JSON.stringify(data.new_method, null, 2)}</pre>
      <p>{data.active_binding ? `Active version ${data.active_binding.version} · ${data.active_binding.digest}` : data.configured_baseline ? "Future tasks use configured baseline." : "No method is selected."}</p>
      <p>Rollback selects baseline for future tasks. Already admitted tasks keep their immutable pin while current authority remains valid; revocation or tombstone blocks their next boundary.</p>
      {data.adoption_requires_current_owner && <p role="status">Inspection only: current original ownership is required for adoption.</p>}
      <label>Review reason<textarea maxLength={500} value={reason} onChange={e => setReason(e.target.value)} /></label>
      <button type="button" disabled={busy || data.adoption_requires_current_owner} onClick={() => void act("accept")}>Adopt reviewed method</button>
      <button type="button" disabled={busy || data.adoption_requires_current_owner} onClick={() => void act("reject")}>Reject method</button>
      <button type="button" disabled={busy || data.adoption_requires_current_owner || !reason.trim()} onClick={() => void act("rollback")}>Rollback to baseline</button>
    </>}
  </section>;
}
