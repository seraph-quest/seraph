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
