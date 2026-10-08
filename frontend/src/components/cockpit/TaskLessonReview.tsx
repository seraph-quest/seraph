import { useEffect, useRef, useState } from "react";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import type { WorkBoardTask } from "../../types";

interface Policy { enabled: boolean; policy_revision: number | null; daily_cap: number; inference_egress: "not_permitted"; adoption: "requires_separate_review" }
interface Source { task_id: string; expected_revision: number; attempt_id: string | null; source_refs: string[];
  scope: { goal_id: string; goal_revision: number; family: "general" | "research" | "software" | "knowledge" };
  eligible: boolean; reason_code: string; automatic_policy?: Policy }
interface Method { schema_version: "TaskMethod.v1"; family: string;
  steps: ({ kind: "registered_tool"; tool_id: string } | { kind: "guard"; check: string } | {
    kind: "registered_capability"; capability_id: "work.json-format.v1"; capability_version: "1";
    typed_input_digest: string; input_schema_digest: string; output_contract_digest: string; contract_snapshot_digest: string;
  })[];
  registered_tool_ids: string[]; input_parameters: Record<string, unknown>; output_contract: Record<string, unknown> }
interface Lesson { schema_version: "task_method_proposal.v1"; proposal_id: string; task_id: string; attempt_id: string;
  revision: number; status: string; result: "candidate_inert" | "no_change"; reason_code: string;
  behavior_changed: false; source_current: boolean; correction: string; old_method: Method | null; new_method: Method | null;
  source_refs: string[]; scope: Source["scope"] }
const record = (v: unknown): v is Record<string, unknown> => Boolean(v && typeof v === "object" && !Array.isArray(v));
async function request(path: string, body?: unknown): Promise<unknown> {
  const response = await apiFetch(`${API_URL}/api/memory/task-lessons${path}`, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!response.ok) throw Error(response.status === 409 ? "Task evidence changed. Refresh Work and inspect the sources again before proposing a lesson." : `Lesson review blocked (${response.status}). Restore current task ownership and verified evidence, then inspect again.`);
  return response.json();
}
function sourceRead(value: unknown, task: WorkBoardTask): Source {
  if (!record(value) || value.task_id !== task.task_id || value.expected_revision !== task.task_revision || typeof value.eligible !== "boolean"
    || !Array.isArray(value.source_refs) || !value.source_refs.every(v => typeof v === "string") || !record(value.scope)
    || value.scope.goal_id !== task.goal_id || value.scope.goal_revision !== task.goal_revision || typeof value.reason_code !== "string"
    || (value.eligible && (typeof value.attempt_id !== "string" || !value.source_refs.length))) throw Error("Source readback does not match this exact Work card. Refresh Work before learning.");
  return value as unknown as Source;
}
export function TaskLessonReview({ task, ownerPrincipalId, ownerSessionId }: { task: WorkBoardTask; ownerPrincipalId?: string | null; ownerSessionId?: string | null }) {
  const [source, setSource] = useState<Source | null>(null), [lesson, setLesson] = useState<Lesson | null>(null);
  const [correction, setCorrection] = useState("");
  const [busy, setBusy] = useState(false), [error, setError] = useState<string | null>(null);
  const [autoAck, setAutoAck] = useState(false);
  const generation = useRef(0);
  const owned = Boolean(ownerPrincipalId && ownerSessionId && task.owner_principal_id === ownerPrincipalId && task.owner_session_id === ownerSessionId && task.ownership_access !== "recovered_read_only");
  useEffect(() => { ++generation.current; setSource(null); setLesson(null); setCorrection(""); setBusy(false); setError(null); setAutoAck(false); return () => { ++generation.current; }; }, [task.task_id, task.task_revision, ownerPrincipalId, ownerSessionId]);
  async function inspect() {
    if (!owned || busy) return;
    const version = generation.current; setBusy(true); setError(null); setLesson(null); setSource(null); setAutoAck(false);
    try { const result = sourceRead(await request(`/sources/${encodeURIComponent(task.task_id)}`), task); if (version === generation.current) setSource(result); }
    catch (e) { if (version === generation.current) setError((e as Error).message); }
    finally { if (version === generation.current) setBusy(false); }
  }
  async function propose() {
    if (!owned || busy || !source?.eligible || !source.attempt_id) return;
    const version = generation.current; setBusy(true); setError(null); setLesson(null);
    try {
      const created = await request("", { task_id: source.task_id, attempt_id: source.attempt_id, correction,
        source_refs: source.source_refs, scope: source.scope, expected_revision: source.expected_revision });
      if (!record(created) || typeof created.proposal_id !== "string") throw Error("Lesson receipt is unconfirmed. Inspect Work before proposing again.");
      if (version !== generation.current) return;
      const result = await request(`/${encodeURIComponent(created.proposal_id)}`);
      if (!record(result) || result.schema_version !== "task_method_proposal.v1" || result.task_id !== source.task_id || result.attempt_id !== source.attempt_id
        || result.proposal_id !== created.proposal_id || result.behavior_changed !== false || typeof result.source_current !== "boolean"
        || !["candidate_inert", "no_change"].includes(String(result.result)) || typeof result.correction !== "string"
        || !record(result.scope) || result.scope.goal_id !== source.scope.goal_id || result.scope.goal_revision !== source.scope.goal_revision) throw Error("Private lesson readback did not match the exact source. Inspect again.");
      if (version === generation.current) setLesson(result as unknown as Lesson);
    } catch (e) { if (version === generation.current) { setSource(null); setError((e as Error).message); } }
    finally { if (version === generation.current) setBusy(false); }
  }
  async function setAutomatic(enabled: boolean) {
    if (!owned || busy || !source || (enabled && !autoAck)) return;
    const version = generation.current; setBusy(true); setError(null);
    try {
      const result = await request(`/automatic-policy/${encodeURIComponent(task.task_id)}`, { enabled, expected_revision: source.expected_revision, mutation_uuid: crypto.randomUUID() });
      if (!record(result) || result.enabled !== enabled || result.inference_egress !== "not_permitted" || result.adoption !== "requires_separate_review" || result.daily_cap !== 2) throw Error("Automatic lesson consent readback is unconfirmed. Inspect the current policy again.");
      if (version === generation.current) { setSource({ ...source, automatic_policy: result as unknown as Policy }); setAutoAck(false); }
    } catch (e) { if (version === generation.current) { setSource(null); setError((e as Error).message); } }
    finally { if (version === generation.current) setBusy(false); }
  }
  return <section aria-label="Learn this task lesson" className="mt-3 rounded border border-white/10 p-3 text-xs">
    <h3 className="font-semibold">Learn this</h3>
    <p>Save a private correction against verified ordinary task evidence. Any proposed method remains inert and requires a separate reviewed adoption.</p>
    {!owned && <p role="status">Current task ownership is required to inspect private lesson sources.</p>}
    <button type="button" className="cockpit-feedback-button" disabled={!owned || busy} onClick={() => void inspect()}>Inspect lesson sources</button>
    {error && <p role="alert" className="text-amber-200">{error}</p>}
    {source && <>
      <p role="status">{source.eligible ? "Verified sources available" : `Blocked: ${source.reason_code}. Verify the actual task attempt and readback, then inspect again.`}</p>
      <ul>{source.source_refs.map(ref => <li key={ref} className="font-mono break-all">{ref}</li>)}</ul>
      {source.eligible && <><label>Private task correction<textarea aria-label="Private task correction" className="cockpit-input w-full" rows={3} maxLength={4000} disabled={busy} value={correction} onChange={e => { setCorrection(e.target.value); setLesson(null); }} /></label>
        <p>Supported corrections: check source existence; verify readback; preserve source attribution. Other corrections are retained privately with an explicit no-change result.</p>
        <button type="button" className="cockpit-feedback-button" disabled={busy} onClick={() => void propose()}>Prepare private lesson candidate</button></>}
      {source.automatic_policy && <div className="mt-2">
        <p role="status">Automatic proposals: {source.automatic_policy.enabled ? "enabled" : "disabled"} for this exact task · at most 2 daily · no inference egress · separate adoption review.</p>
        {source.automatic_policy.enabled ? <button type="button" disabled={busy} onClick={() => void setAutomatic(false)}>Disable automatic task lesson proposals</button> : <><label><input type="checkbox" disabled={busy} checked={autoAck} onChange={e => setAutoAck(e.target.checked)} />Allow bounded private lesson proposals from this exact task after completion; this does not adopt changes or permit inference.</label><button type="button" disabled={busy || !autoAck} onClick={() => void setAutomatic(true)}>Enable automatic task lesson proposals</button></>}
      </div>}
    </>}
    {lesson && <div role="region" aria-label="Exact private lesson change" className="mt-3">
      <p role="status">{lesson.result === "no_change" ? "No change" : "Inert method candidate"} · {lesson.reason_code} · behavior unchanged · {lesson.source_current ? "source current" : "source changed; inspect again"}</p>
      <p>Proposal {lesson.proposal_id} · revision {lesson.revision} · Goal {lesson.scope.goal_id} revision {lesson.scope.goal_revision}</p>
      <p className="whitespace-pre-wrap">Correction: {lesson.correction}</p>
      <h4>Old method</h4><pre aria-label="Old task method" className="whitespace-pre-wrap break-all">{JSON.stringify(lesson.old_method, null, 2)}</pre>
      <h4>Proposed method</h4><pre aria-label="Proposed task method" className="whitespace-pre-wrap break-all">{JSON.stringify(lesson.new_method, null, 2)}</pre>
      <p>No method adoption or quality improvement is established by this candidate.</p>
    </div>}
  </section>;
}
