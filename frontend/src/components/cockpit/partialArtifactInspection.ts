import type { SpecialistPartialOutput } from "../../lib/generalTask";

export interface PartialArtifactInspectionBinding {
  ownerPrincipalId: string;
  ownerSessionId: string;
  taskId: string;
  taskRevision: number;
  attemptId: string;
  workflowRunId: string;
  planRevision: number;
  manifestRevision: number;
  output: SpecialistPartialOutput;
}
export interface PartialArtifactInspectionRequest {
  binding: PartialArtifactInspectionBinding;
  signal: AbortSignal;
}
export interface PartialArtifactInspectionReceipt {
  binding: PartialArtifactInspectionBinding;
  presented: true;
}
export type InspectPartialArtifact = (request: PartialArtifactInspectionRequest) => Promise<PartialArtifactInspectionReceipt | null>;

export function partialInspectionKey(binding: PartialArtifactInspectionBinding): string {
  const o = binding.output;
  return JSON.stringify([binding.ownerPrincipalId, binding.ownerSessionId, binding.taskId, binding.taskRevision,
    binding.attemptId, binding.workflowRunId, binding.planRevision, binding.manifestRevision,
    o.child_task_id, o.child_job_id, o.delegation_invocation_id, o.artifact_id, o.content_sha256, o.size_bytes]);
}

// The authenticated jobs endpoint enforces canonical owner ancestry. Check its
// actual metadata before normalization supplies requested session/run fields.
export function partialJobHasExactReceipt(job: Record<string, unknown>, binding: PartialArtifactInspectionBinding): boolean {
  const o = binding.output;
  return job.job_id === o.child_job_id && job.parent_job_id === o.delegation_invocation_id
    && Array.isArray(job.artifacts) && job.artifacts.some(value => {
      if (!value || typeof value !== "object" || Array.isArray(value)) return false;
      const artifact = value as Record<string, unknown>;
      return artifact.artifact_id === o.artifact_id && artifact.content_sha256 === o.content_sha256
        && Number.isSafeInteger(artifact.size_bytes) && artifact.size_bytes === o.size_bytes
        && typeof artifact.file_path === "string" && artifact.file_path.length > 0
        && artifact.exists !== false;
    });
}
