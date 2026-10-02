import { useEffect, useState } from "react";
import type { WorkBoardTask } from "../../types";
import type { AttentionOwner } from "../../lib/cockpitAttention";
import { attentionRequest } from "../../lib/taskAttention";

export function AccountingRecoveryLink({ task, owner, metadataConfirmed, onOpen }: { task: WorkBoardTask; owner: AttentionOwner; metadataConfirmed: boolean; onOpen?: () => void }) {
  const key = `${owner.principalId}:${owner.sessionId}:${task.task_id}:${task.task_revision}:${task.latest_attempt?.workflow_run_id ?? ""}`;
  const [receipt, setReceipt] = useState<{ key: string; ready: boolean; message: string } | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    if (!metadataConfirmed || !task.latest_attempt?.workflow_run_id) return () => controller.abort();
    void attentionRequest<{ operations?: unknown[] }>(`/api/settings/model-fabric/accounting?job_id=${encodeURIComponent(task.latest_attempt.workflow_run_id)}`, controller.signal).then((payload) => {
      if (controller.signal.aborted) return;
      const ready = payload.operations?.slice(0, 100).some((value) => {
        if (!value || typeof value !== "object") return false;
        const row = value as Record<string, unknown>;
        return row.job_id === task.latest_attempt?.workflow_run_id && row.owner_id === owner.principalId && row.goal_id === task.goal_id && row.goal_revision === task.goal_revision && typeof row.operation_id === "string" && Array.isArray(row.controls) && row.controls.some((value) => {
          if (!value || typeof value !== "object") return false;
          const control = value as Record<string, unknown>;
          return control.action === "settle" && control.method === "POST" && control.endpoint === "/api/settings/model-fabric/accounting/settle" && control.expected_revision === row.revision;
        });
      }) ?? false;
      setReceipt({ key, ready, message: ready ? "The owning accounting API advertises settlement for this exact job and goal." : "No settlement control is advertised for this exact job and goal. Cost remains unresolved; inspect the owning receipt." });
    }).catch(() => { if (!controller.signal.aborted) setReceipt({ key, ready: false, message: "Owning accounting metadata is unavailable or permission is missing. Cost remains unresolved; refresh task detail and restore accounting in Settings." }); });
    return () => controller.abort();
  }, [key, metadataConfirmed]);
  const current = receipt?.key === key ? receipt : null;
  return <><p className="mt-1 text-xs">{current?.message ?? "Accounting controls are not confirmed. Refresh task detail to check the owning ledger."}</p>{current?.ready && metadataConfirmed && onOpen && <button type="button" className="cockpit-feedback-button mt-2" onClick={onOpen}>Open owning cost accounting</button>}</>;
}
