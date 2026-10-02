import { useEffect, useRef, useState } from "react";
import type { WorkBoardTask } from "../../types";
import type { AttentionOwner } from "../../lib/cockpitAttention";
import { attentionRequest, readAttentionTask } from "../../lib/taskAttention";
import { AccountingRecoveryLink } from "./AccountingRecoveryLink";

interface RecoveryJob {
  job_id: string; operation_id: string; goal_id: string; goal_revision: number; status: string;
  recovery_reason?: string | null;
  effects?: Array<{ effect_type?: string; status?: string; details?: { verified?: boolean } }>;
}

export function TaskEffectRecovery({ task, owner, metadataConfirmed, onRefresh, onOpenAccounting }: {
  task: WorkBoardTask; owner: AttentionOwner; metadataConfirmed: boolean; onRefresh: () => Promise<void>; onOpenAccounting?: () => void;
}) {
  const [job, setJob] = useState<RecoveryJob | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const generation = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const supported = task.capability_id === "work.github-followthrough.v1" && task.recovery_action === "reconcile_external_effect" && task.block_kind === "unknown_effect" && task.ownership_access !== "recovered_read_only";
  const jobPath = `/api/capabilities/github/jobs/${encodeURIComponent(task.latest_attempt?.workflow_run_id ?? "")}`;
  const read = async (signal?: AbortSignal) => {
    await readAttentionTask(task, owner, signal);
    const current = await attentionRequest<RecoveryJob>(jobPath, signal);
    if (!current.job_id || current.job_id !== task.latest_attempt?.workflow_run_id || !current.operation_id || current.goal_id !== task.goal_id || current.goal_revision !== task.goal_revision) throw new Error("The owning job is not bound to this exact task and goal. Refresh task detail.");
    return current;
  };
  const refresh = async () => {
    const version = ++generation.current;
    controller.current?.abort();
    const active = new AbortController(); controller.current = active;
    setBusy(true); setMessage(null);
    try {
      const current = await read(active.signal);
      if (version === generation.current && !active.signal.aborted) setJob(current);
    } catch (error) {
      if (version === generation.current && !active.signal.aborted) { setJob(null); setMessage(error instanceof Error ? error.message : "Readback metadata unavailable."); }
    } finally { if (version === generation.current && !active.signal.aborted) setBusy(false); }
  };
  useEffect(() => {
    setJob(null);
    if (supported && metadataConfirmed) void refresh();
    return () => { generation.current += 1; controller.current?.abort(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [task.task_id, task.task_revision, task.latest_attempt?.attempt_id, task.latest_attempt?.workflow_run_id, owner.principalId, owner.sessionId, supported, metadataConfirmed]);
  const reconcile = async () => {
    if (!job || !supported || !metadataConfirmed || busy) return;
    const version = generation.current;
    setBusy(true); setMessage(null);
    try {
      const current = await read();
      if (version !== generation.current) return;
      if (current.operation_id !== job.operation_id) throw new Error("The original operation binding changed. Refresh before reconciling.");
      const result = await attentionRequest<RecoveryJob>(`${jobPath}/reconcile`, undefined, {});
      if (version !== generation.current) return;
      if (result.job_id !== current.job_id || result.operation_id !== current.operation_id || result.goal_id !== current.goal_id || result.goal_revision !== current.goal_revision) throw new Error("Readback did not confirm the original operation. Inspect the owning job before continuing.");
      await readAttentionTask(task, owner);
      if (version !== generation.current) return;
      setJob(result);
      const verified = result.effects?.some((effect) => effect.effect_type === "github_publication" && effect.status === "succeeded" && effect.details?.verified === true);
      setMessage(verified ? `Independent readback is verified; owning job is ${result.status}. Task completion remains the board's confirmed state. No publication was resent.` : `Independent readback remains ${result.status}. Inspect the owning receipt; retry stays unavailable. No publication was resent.`);
      await onRefresh();
    } catch (error) {
      if (version === generation.current) { setJob(null); setMessage(error instanceof Error ? error.message : "Readback unresolved. Inspect exact task metadata."); }
    } finally { if (version === generation.current) setBusy(false); }
  };
  return (
    <section className="mt-3 rounded border border-amber-500/40 p-3" aria-label="Unknown outcome recovery">
      <div className="font-semibold">Unknown outcome recovery</div>
      <p className="mt-1 text-xs">The original attempt remains bound. Readback can reconcile its effect; it never sends or retries a publication.</p>
      {supported ? <><p className="mt-1 text-xs">Owning GitHub job · {job?.status ?? "readback metadata unavailable"}{job?.recovery_reason ? ` · ${job.recovery_reason}` : ""}</p><div className="mt-2 flex flex-wrap gap-2"><button type="button" className="cockpit-feedback-button" disabled={busy || !metadataConfirmed} onClick={() => void refresh()}>Refresh owning readback</button><button type="button" className="cockpit-feedback-button" disabled={busy || !job || !metadataConfirmed} onClick={() => void reconcile()}>Reconcile recorded GitHub effect</button></div></> : task.block_kind === "cost_liability" ? <><p className="mt-1 text-xs">Cost remains reserved until the owning accounting API confirms settlement. No settlement amount is inferred here.</p><AccountingRecoveryLink task={task} owner={owner} metadataConfirmed={metadataConfirmed} onOpen={onOpenAccounting} /></> : <p className="mt-1 text-xs">This capability has no supported readback control in this inspector. Inspect its workflow and receipts; preserve its unknown outcome and use only the server's advertised cancel or review actions.</p>}
      {!metadataConfirmed && <p className="mt-1 text-xs">Last confirmed metadata · refresh task detail before acting.</p>}
      {message && <p className="mt-2 text-xs" role="status">{message}</p>}
    </section>
  );
}
