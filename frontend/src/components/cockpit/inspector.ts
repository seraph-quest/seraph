export interface CockpitAuditEvent {
  id: string;
  session_id?: string | null;
  actor?: string;
  event_type: string;
  tool_name?: string | null;
  risk_level: string;
  policy_mode: string;
  summary: string;
  details?: Record<string, unknown> | null;
  created_at: string;
}

export interface ArtifactRecord {
  id: string;
  source: string;
  filePath: string;
  sessionId?: string | null;
  createdAt: string;
  summary: string;
}

export interface WorkflowRunRecord {
  id: string;
  toolName: string;
  workflowName: string;
  sessionId?: string | null;
  status: "succeeded" | "failed" | "running" | "awaiting_approval";
  startedAt: string;
  updatedAt: string;
  summary: string;
  stepTools: string[];
  artifactPaths: string[];
  continuedErrorSteps: string[];
  arguments?: Record<string, unknown>;
  artifacts: ArtifactRecord[];
  riskLevel?: string;
  executionBoundaries?: string[];
  acceptsSecretRefs?: boolean;
  pendingApprovalCount?: number;
  pendingApprovalIds?: string[];
  pendingApprovals?: Array<{
    id: string;
    summary: string;
    riskLevel?: string;
    createdAt: string;
    threadId?: string | null;
    threadLabel?: string | null;
    resumeMessage?: string | null;
  }>;
  threadId?: string | null;
  threadLabel?: string | null;
  threadSource?: string | null;
  replayAllowed?: boolean;
  replayBlockReason?: string | null;
  replayDraft?: string | null;
  replayInputs?: Record<string, unknown>;
  parameterSchema?: Record<string, unknown>;
  replayRecommendedActions?: Array<Record<string, unknown>>;
  availability?: string | null;
  resumeFromStep?: string | null;
  resumeCheckpointLabel?: string | null;
  threadContinueMessage?: string | null;
  approvalRecoveryMessage?: string | null;
  timeline?: Array<{ kind: string; at: string; summary: string }>;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function readString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function readDetailsString(
  details: Record<string, unknown> | null,
  ...path: string[]
): string | null {
  let current: unknown = details;
  for (const part of path) {
    const record = asRecord(current);
    if (!record || !(part in record)) return null;
    current = record[part];
  }
  return readString(current);
}

function artifactFromEvent(event: CockpitAuditEvent): ArtifactRecord | null {
  const details = asRecord(event.details);

  if (
    event.event_type === "integration_succeeded" &&
    event.tool_name === "filesystem:workspace" &&
    readDetailsString(details, "operation") === "write"
  ) {
    const filePath = readDetailsString(details, "file_path");
    if (!filePath) return null;
    return {
      id: `${event.id}:filesystem`,
      source: "filesystem write",
      filePath,
      sessionId: event.session_id ?? null,
      createdAt: event.created_at,
      summary: event.summary,
    };
  }

  if (event.tool_name === "write_file" && event.event_type === "tool_result") {
    const filePath = readDetailsString(details, "arguments", "file_path");
    if (!filePath) return null;
    return {
      id: `${event.id}:tool`,
      source: "write_file tool",
      filePath,
      sessionId: event.session_id ?? null,
      createdAt: event.created_at,
      summary: event.summary,
    };
  }

  return null;
}

export function collectArtifacts(events: CockpitAuditEvent[]): ArtifactRecord[] {
  const artifacts: ArtifactRecord[] = [];
  const seen = new Set<string>();

  for (const event of events) {
    const artifact = artifactFromEvent(event);
    if (!artifact) continue;

    const dedupeKey = `${artifact.sessionId ?? "global"}:${artifact.filePath}`;
    if (seen.has(dedupeKey)) continue;

    seen.add(dedupeKey);
    artifacts.push(artifact);
  }

  return artifacts;
}

function workflowNameFromTool(toolName: string): string {
  return toolName.startsWith("workflow_")
    ? toolName.slice("workflow_".length).replace(/_/g, "-")
    : toolName;
}

function extractArtifactPaths(value: unknown): string[] {
  const paths: string[] = [];

  function visit(current: unknown, keyHint?: string): void {
    if (Array.isArray(current)) {
      current.forEach((item) => visit(item, keyHint));
      return;
    }
    const record = asRecord(current);
    if (record) {
      Object.entries(record).forEach(([key, inner]) => visit(inner, key));
      return;
    }
    if (
      keyHint === "file_path"
      && typeof current === "string"
      && current.trim().length > 0
      && !paths.includes(current)
    ) {
      paths.push(current);
    }
  }

  visit(value);
  return paths;
}

function workflowKey(event: CockpitAuditEvent): string {
  return `${event.session_id ?? "global"}:${event.tool_name ?? "workflow"}`;
}

export function collectWorkflowRuns(events: CockpitAuditEvent[]): WorkflowRunRecord[] {
  const artifacts = collectArtifacts(events);
  const artifactMap = new Map(artifacts.map((artifact) => [`${artifact.sessionId ?? "global"}:${artifact.filePath}`, artifact]));
  const ordered = [...events]
    .filter((event) => event.tool_name?.startsWith("workflow_"))
    .sort((left, right) => new Date(left.created_at).getTime() - new Date(right.created_at).getTime());
  const pending = new Map<string, WorkflowRunRecord[]>();
  const completed: WorkflowRunRecord[] = [];

  for (const event of ordered) {
    const details = asRecord(event.details);
    const key = workflowKey(event);
    const toolName = event.tool_name ?? "workflow";
    if (event.event_type === "tool_call") {
      const args = asRecord(details?.arguments) ?? undefined;
      const artifactPaths = extractArtifactPaths(args);
      const run: WorkflowRunRecord = {
        id: event.id,
        toolName,
        workflowName: readDetailsString(details, "workflow_name") ?? workflowNameFromTool(toolName),
        sessionId: event.session_id ?? null,
        status: "running",
        startedAt: event.created_at,
        updatedAt: event.created_at,
        summary: event.summary,
        stepTools: [],
        artifactPaths,
        continuedErrorSteps: [],
        arguments: args ?? undefined,
        artifacts: [],
      };
      const queue = pending.get(key) ?? [];
      queue.push(run);
      pending.set(key, queue);
      continue;
    }

    const queue = pending.get(key) ?? [];
    const run = queue.shift() ?? {
      id: event.id,
      toolName,
      workflowName: readDetailsString(details, "workflow_name") ?? workflowNameFromTool(toolName),
      sessionId: event.session_id ?? null,
      status: "running" as const,
      startedAt: event.created_at,
      updatedAt: event.created_at,
      summary: event.summary,
      stepTools: [],
      artifactPaths: [],
      continuedErrorSteps: [],
      arguments: asRecord(details?.arguments) ?? undefined,
      artifacts: [],
    };
    if (queue.length > 0) {
      pending.set(key, queue);
    } else {
      pending.delete(key);
    }

    const artifactPaths = [
      ...run.artifactPaths,
      ...(Array.isArray(details?.artifact_paths)
        ? details.artifact_paths.filter(
            (value): value is string => typeof value === "string" && value.trim().length > 0,
          )
        : []),
      ...extractArtifactPaths(details?.arguments),
    ].filter((value, index, list) => list.indexOf(value) === index);

    const linkedArtifacts = artifactPaths
      .map((filePath) => artifactMap.get(`${run.sessionId ?? "global"}:${filePath}`))
      .filter((artifact): artifact is ArtifactRecord => artifact != null);

    run.status = event.event_type === "tool_failed" ? "failed" : "succeeded";
    run.updatedAt = event.created_at;
    run.summary = event.summary;
    run.stepTools = Array.isArray(details?.step_tools)
      ? details.step_tools.filter((value): value is string => typeof value === "string")
      : run.stepTools;
    run.artifactPaths = artifactPaths;
    run.continuedErrorSteps = Array.isArray(details?.continued_error_steps)
      ? details.continued_error_steps.filter((value): value is string => typeof value === "string")
      : run.continuedErrorSteps;
    run.artifacts = linkedArtifacts;
    completed.push(run);
  }

  for (const queue of pending.values()) {
    completed.push(...queue);
  }

  return completed.sort(
    (left, right) => new Date(right.updatedAt).getTime() - new Date(left.updatedAt).getTime(),
  );
}

export function formatInspectorValue(value: unknown): string {
  if (value === null || value === undefined) return "n/a";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}
