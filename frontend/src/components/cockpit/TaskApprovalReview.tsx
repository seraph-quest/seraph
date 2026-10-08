import { useEffect, useRef, useState } from "react";
import type { WorkBoardTask } from "../../types";
import type { AttentionOwner } from "../../lib/cockpitAttention";
import { attentionRequest, readAttentionTask, readTaskApproval } from "../../lib/taskAttention";
import { redactApprovalText } from "./cockpitAuthority";

function reviewedDestination(value: unknown): string[] {
  if (!value || typeof value !== "object") return [];
  const scope = value as Record<string, unknown>;
  const target = scope.target as Record<string, unknown> | undefined;
  const payload = scope.payload as Record<string, unknown> | undefined;
  const lines: string[] = [];
  if (target?.provider === "github" && typeof target.repository === "string" && /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(target.repository) && ["create_issue", "create_comment"].includes(String(scope.action))) {
    lines.push(`GitHub ${target.repository} · ${String(scope.action)}${Number.isSafeInteger(target.issue_number) ? ` · issue ${String(target.issue_number)}` : ""}`);
    for (const key of ["title_sha256", "body_sha256"]) if (typeof payload?.[key] === "string" && /^[a-f0-9]{64}$/.test(payload[key] as string)) lines.push(`${key}: ${payload[key] as string}`);
  }
  return lines;
}

export interface TaskApprovalReviewProps {
  task: WorkBoardTask;
  owner: AttentionOwner;
  approvalId?: string | null;
  metadataConfirmed: boolean;
  onRefresh: () => Promise<void>;
}

export function TaskApprovalReview({ task, owner, approvalId, metadataConfirmed, onRefresh }: TaskApprovalReviewProps) {
  const [approval, setApproval] = useState<Record<string, unknown> | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const generation = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const refresh = async () => {
    const version = ++generation.current;
    controller.current?.abort();
    const active = new AbortController(); controller.current = active;
    setBusy(true); setApproval(null); setMessage(null);
    try {
      const current = await readAttentionTask(task, owner, active.signal);
      const next = await readTaskApproval(current, owner, approvalId, active.signal);
      if (version !== generation.current || active.signal.aborted) return;
      setApproval(next);
      if (!next) setMessage("No exact current-attempt approval is pending. Refresh task detail; standalone approvals stay in Pending approvals.");
    } catch (error) {
      if (version === generation.current && !active.signal.aborted) setMessage(error instanceof Error ? error.message : "Approval metadata unavailable.");
    } finally { if (version === generation.current && !active.signal.aborted) setBusy(false); }
  };
  useEffect(() => {
    if (metadataConfirmed && task.ownership_access !== "recovered_read_only") void refresh();
    else { setApproval(null); setMessage("Task metadata is not current. Refresh detail before reviewing an approval."); }
    return () => { generation.current += 1; controller.current?.abort(); };
    // The exact owner/task/attempt/revision is the lifetime of this review.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [owner.principalId, owner.sessionId, task.task_id, task.task_revision, task.latest_attempt?.attempt_id, task.latest_attempt?.workflow_run_id, approvalId, metadataConfirmed]);

  const decide = async (action: "approve" | "deny") => {
    if (!approval || !metadataConfirmed || busy) return;
    const version = generation.current;
    setBusy(true); setApproval(null); setMessage(null);
    try {
      const current = await readAttentionTask(task, owner);
      const exact = await readTaskApproval(current, owner, String(approval.id));
      if (!exact || version !== generation.current) throw new Error("The approval or attempt changed. Refresh before acting.");
      const result = await attentionRequest<Record<string, unknown>>(`/api/approvals/${encodeURIComponent(String(exact.id))}/${action}`, undefined, {});
      if (version !== generation.current) return;
      if (result.approval_id !== exact.id || result.owner_principal_id !== owner.principalId || result.operator_session_id !== owner.sessionId || result.status !== (action === "approve" ? "approved" : "denied")) throw new Error("Approval response did not confirm this exact owner and decision. Refresh before continuing.");
      await readAttentionTask(task, owner);
      if (version !== generation.current) return;
      setMessage(`Exact approval ${result.status}. Execution and completion remain governed by the task; no work was retried here.`);
      await onRefresh();
    } catch (error) {
      if (version === generation.current) setMessage(error instanceof Error ? error.message : "Approval decision unavailable. Refresh exact state.");
    } finally { if (version === generation.current) setBusy(false); }
  };
  return (
    <section className="rounded border border-amber-500/40 p-3" aria-label="Exact task approval">
      <div className="font-semibold">Exact task approval</div>
      <p className="mt-1 text-xs">Current task, attempt, goal and operator root are checked again before a decision. Approval does not mean the task is complete.</p>
      {approval && <><p className="mt-2 break-all">{redactApprovalText(approval.summary ?? "Review the exact pending action", approval.approval_scope)} · expires {String(approval.expires_at)}</p>{reviewedDestination(approval.approval_scope).map((line) => <p key={line} className="break-all text-xs">{line}</p>)}<p className="text-xs">Approval {String(approval.id)} · run {task.latest_attempt?.workflow_run_id}</p></>}
      {message && <p role="status" className="mt-2 text-xs">{message}</p>}
      <div className="mt-2 flex flex-wrap gap-2">
        <button type="button" className="cockpit-feedback-button" disabled={busy || !metadataConfirmed || task.ownership_access === "recovered_read_only"} onClick={() => void refresh()}>Refresh exact approval</button>
        <button type="button" className="cockpit-feedback-button" disabled={busy || !approval || !metadataConfirmed} onClick={() => void decide("approve")}>Approve exact action</button>
        <button type="button" className="cockpit-feedback-button" disabled={busy || !approval || !metadataConfirmed} onClick={() => void decide("deny")}>Deny exact action</button>
      </div>
    </section>
  );
}
