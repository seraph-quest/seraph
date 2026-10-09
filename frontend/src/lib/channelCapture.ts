import type { WorkBoardReceiptReference } from "../types";

export interface ChannelOutputReview {
  taskId: string;
  taskRevision: number;
  attemptId: string;
  ownerSessionId: string;
  workflowRunId: string;
  reference: WorkBoardReceiptReference;
}

/** Decode the authenticated source owner's exact output projection. */
export function decodeChannelOutputReview(value: unknown, ownerSessionId: string): ChannelOutputReview {
  const fail = () => { throw new Error("Exact current output receipt is unavailable. Inspect current Work."); };
  if (!value || typeof value !== "object" || Array.isArray(value)) return fail();
  const row = value as Record<string, unknown>;
  const fields = ["attempt_id", "no_learning", "owner_session_id", "parent_workflow_run_id", "reference", "task_id", "task_revision", "workflow_run_id"];
  if (Object.keys(row).sort().join(",") !== fields.join(",") || row.no_learning !== true
    || row.owner_session_id !== ownerSessionId || row.parent_workflow_run_id !== null
    || !Number.isSafeInteger(row.task_revision) || Number(row.task_revision) < 1) return fail();
  for (const key of ["task_id", "attempt_id", "workflow_run_id"]) {
    if (typeof row[key] !== "string" || !(row[key] as string).length || (row[key] as string).length > 300) return fail();
  }
  if (!row.reference || typeof row.reference !== "object" || Array.isArray(row.reference)) return fail();
  const ref = row.reference as Record<string, unknown>;
  if (typeof ref.artifact_id !== "string" || !/^art_[a-f0-9]{24}$/.test(ref.artifact_id)
    || ref.artifact_type !== "general_task_step" || ref.exists !== true
    || typeof ref.content_sha256 !== "string" || !/^[a-f0-9]{64}$/.test(ref.content_sha256)
    || typeof ref.file_path !== "string" || !new RegExp(`^artifacts/work-board/general-tasks/[a-f0-9]{64}-${ref.content_sha256}\\.json$`).test(ref.file_path)
    || !Number.isSafeInteger(ref.size_bytes) || Number(ref.size_bytes) < 1 || Number(ref.size_bytes) > 65536) return fail();
  return { taskId: row.task_id as string, taskRevision: row.task_revision as number,
    attemptId: row.attempt_id as string, ownerSessionId, workflowRunId: row.workflow_run_id as string,
    reference: { artifact_id: ref.artifact_id, artifact_type: ref.artifact_type,
      file_path: ref.file_path, content_sha256: ref.content_sha256, size_bytes: ref.size_bytes as number, exists: true } };
}
