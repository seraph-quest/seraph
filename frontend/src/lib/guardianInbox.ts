import { API_URL } from "../config/constants";
import { apiFetch } from "./api";
import type {
  GuardianInboxActionRequest,
  GuardianInboxActionResponse,
  GuardianInboxActionHistoryEntry,
  GuardianInboxEvidenceRef,
  GuardianInboxEvidencePreview,
  GuardianInboxItem,
  GuardianInboxMailOrigin,
  GuardianInboxJob,
  GuardianInboxReadback,
  GuardianInboxPage,
} from "../types";

export class GuardianInboxApiError extends Error {
  readonly status: number;
  readonly code: string | null;
  readonly recoveryAction: string | null;
  readonly payload: unknown;

  constructor(
    status: number,
    message: string,
    options: { code?: string | null; recoveryAction?: string | null; payload?: unknown } = {},
  ) {
    super(message);
    this.name = "GuardianInboxApiError";
    this.status = status;
    this.code = options.code ?? null;
    this.recoveryAction = options.recoveryAction ?? null;
    this.payload = options.payload ?? null;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function stringValue(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function integerValue(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isInteger(value) ? value : fallback;
}

function nullableInteger(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) ? value : null;
}

function normalizeEvidence(value: unknown): GuardianInboxEvidenceRef[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((entry): GuardianInboxEvidenceRef[] => {
    if (typeof entry === "string") return [{ artifact_id: entry }];
    if (!isRecord(entry)) return [];
    const optionalString = (key: string): string | null => (
      typeof entry[key] === "string" ? entry[key] as string : null
    );
    const sha256 = optionalString("sha256");
    return [{
      artifact_id: optionalString("artifact_id"),
      artifact_type: optionalString("artifact_type"),
      file_path: optionalString("file_path"),
      content_sha256: optionalString("content_sha256") ?? sha256,
      sha256,
      kind: optionalString("kind"),
      status: optionalString("status"),
      verification: optionalString("verification"),
      last_verified_at: optionalString("last_verified_at"),
      workflow_run_id: optionalString("workflow_run_id"),
      owner_session_id: optionalString("owner_session_id"),
      target_path: optionalString("target_path"),
      artifact_url: optionalString("artifact_url"),
      label: optionalString("label"),
    }];
  });
}

function normalizeJob(value: unknown): GuardianInboxJob | null {
  if (!isRecord(value)) return null;
  const readback = isRecord(value.readback) ? value.readback : null;
  const optionalString = (key: string): string | null => (
    typeof value[key] === "string" ? value[key] as string : null
  );
  const nestedString = (...keys: string[]): string | null => {
    for (const key of keys) {
      if (readback && typeof readback[key] === "string") return readback[key] as string;
    }
    return null;
  };
  const directOrNested = (directKey: string, ...nestedKeys: string[]): string | null => (
    optionalString(directKey) ?? nestedString(...nestedKeys)
  );
  const readbacks: GuardianInboxReadback[] = Array.isArray(value.readbacks)
    ? value.readbacks.flatMap((entry): GuardianInboxReadback[] => {
      if (!isRecord(entry)) return [];
      const text = (key: string): string | null => (
        typeof entry[key] === "string" ? entry[key] as string : null
      );
      return [{
        target_path: text("target_path"),
        readback_id: text("readback_id") ?? text("id"),
        verified_at: text("verified_at"),
        digest: text("digest") ?? text("content_sha256") ?? text("target_digest"),
        status: text("status"),
      }];
    })
    : [];
  const job: GuardianInboxJob = {
    id: optionalString("id"),
    status: optionalString("status"),
    attempt_count: nullableInteger(value.attempt_count),
    max_attempts: nullableInteger(value.max_attempts),
    readback_id: directOrNested("readback_id", "readback_id", "id"),
    verified_at: directOrNested("verified_at", "verified_at"),
    digest: directOrNested("digest", "digest", "content_sha256", "target_digest"),
    readback_status: directOrNested("readback_status", "status"),
    readbacks,
  };
  const hasScalar = Object.entries(job)
    .filter(([key]) => key !== "readbacks")
    .some(([, entry]) => entry !== null && entry !== undefined);
  return hasScalar || readbacks.length > 0 ? job : null;
}

function normalizeEvidencePreviews(value: unknown): GuardianInboxEvidencePreview[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((entry): GuardianInboxEvidencePreview[] => {
    if (!isRecord(entry)) return [];
    const text = (key: string): string | null => (
      typeof entry[key] === "string" ? entry[key] as string : null
    );
    return [{
      artifact_id: text("artifact_id"),
      artifact_type: text("artifact_type"),
      file_path: text("file_path"),
      sha256: text("sha256"),
      owner_session_id: text("owner_session_id"),
      workflow_run_id: text("workflow_run_id"),
      text: typeof entry.text === "string" ? entry.text.slice(0, 72 * 1024) : null,
      trust: text("trust"),
    }];
  });
}

function normalizeActionHistory(value: unknown): GuardianInboxActionHistoryEntry[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((entry): GuardianInboxActionHistoryEntry[] => {
    if (!isRecord(entry)) return [];
    const action = entry.action === "accept_followup" || entry.action === "snooze" || entry.action === "dismiss"
      ? entry.action
      : "unavailable";
    const outcome = typeof entry.outcome === "string" && KNOWN_STATES.has(entry.outcome)
      ? entry.outcome as GuardianInboxActionHistoryEntry["outcome"]
      : "unavailable";
    const reasonState = entry.reason_state === "provided"
      ? "provided"
      : entry.reason_state === "not_provided"
        ? "not_provided"
        : "unavailable";
    return [{
      receipt_id: stringValue(entry.receipt_id),
      action,
      created_at: typeof entry.created_at === "string" ? entry.created_at : null,
      expected_revision: nullableInteger(entry.expected_revision),
      result_revision: nullableInteger(entry.result_revision),
      task_id: typeof entry.task_id === "string" ? entry.task_id : null,
      outcome,
      reason_state: reasonState,
      safe_reason: typeof entry.safe_reason === "string" ? entry.safe_reason.slice(0, 500) : null,
    }];
  });
}

function normalizeMailOrigin(value: unknown, fallbackWatchId: unknown = null): GuardianInboxMailOrigin | null {
  if (!isRecord(value) || value.private !== true) return null;
  const watchId = typeof value.watch_id === "string" && value.watch_id.trim()
    ? value.watch_id
    : typeof fallbackWatchId === "string" && fallbackWatchId.trim() ? fallbackWatchId : null;
  if (!watchId) return null;
  if (typeof value.message_binding_id !== "string" || !value.message_binding_id.trim()) return null;
  if (typeof value.message_revision !== "string" || !value.message_revision.trim()) return null;
  return {
    watch_id: watchId,
    message_binding_id: value.message_binding_id,
    message_revision: value.message_revision,
    status: typeof value.status === "string" ? value.status.slice(0, 64) : "unknown",
    private: true,
  };
}

const SUPPORTED_ACTIONS = new Set(["accept_followup", "snooze", "dismiss"]);
const KNOWN_STATES = new Set(["pending", "snoozed", "accepted", "dismissed", "expired"]);

function normalizeActions(value: unknown): GuardianInboxItem["allowed_actions"] {
  if (!Array.isArray(value)) return [];
  return value.filter((entry): entry is GuardianInboxItem["allowed_actions"][number] =>
    typeof entry === "string" && SUPPORTED_ACTIONS.has(entry),
  );
}

/**
 * Normalize only the safe inbox projection. The inbox never renders source
 * bytes or executable plan text; those stay behind the existing artifact and
 * task routes.
 */
export function normalizeGuardianInboxItem(value: unknown): GuardianInboxItem | null {
  if (!isRecord(value)) return null;
  const id = stringValue(value.id).trim();
  if (!id) return null;

  const rawState = typeof value.state === "string" ? value.state : null;
  const knownState = rawState !== null && KNOWN_STATES.has(rawState);
  const evidence = normalizeEvidence(value.evidence_refs);
  const source = isRecord(value.source) ? value.source : null;
  const links = isRecord(value.links) ? value.links : null;
  return {
    id,
    revision: Math.max(1, integerValue(value.revision, 1)),
    state: (knownState ? rawState : "pending") as GuardianInboxItem["state"],
    degraded: !knownState,
    source_kind: stringValue(value.source_kind, "source_packet"),
    source_id: stringValue(value.source_id),
    title: stringValue(value.title, "Watched source changed").slice(0, 240),
    summary: stringValue(value.summary, "A verified local dossier is ready for your review").slice(0, 240),
    why_now: stringValue(value.why_now, "A permitted source produced a verified change.").slice(0, 240),
    goal_id: stringValue(value.goal_id),
    goal_revision: Math.max(1, integerValue(value.goal_revision, 1)),
    watch_id: stringValue(value.watch_id),
    plan_revision: Math.max(1, integerValue(value.plan_revision, 1)),
    task_id: typeof value.task_id === "string" ? value.task_id : null,
    expires_at: stringValue(value.expires_at),
    snoozed_until: typeof value.snoozed_until === "string" ? value.snoozed_until : null,
    evidence_refs: evidence,
    evidence_previews: normalizeEvidencePreviews(value.evidence_previews),
    job: normalizeJob(value.job),
    allowed_actions: knownState ? normalizeActions(value.allowed_actions) : [],
    evidence_status: typeof value.evidence_status === "string" ? value.evidence_status : null,
    source_status: typeof value.source_status === "string"
      ? value.source_status
      : typeof source?.status === "string" ? source.status : null,
    source_freshness: typeof value.source_freshness === "string"
      ? value.source_freshness
      : typeof source?.freshness === "string"
        ? source.freshness
        : typeof source?.age_seconds === "number" ? `${source.age_seconds}s old` : null,
    verification_status: typeof value.verification_status === "string" ? value.verification_status : null,
    memory_status: typeof value.memory_status === "string" ? value.memory_status : null,
    policy_reason: typeof value.policy_reason === "string" ? value.policy_reason.slice(0, 240) : null,
    recovery_action: typeof value.recovery_action === "string" ? value.recovery_action : null,
    evidence_url: typeof value.evidence_url === "string" ? value.evidence_url : null,
    task_url: typeof value.task_url === "string"
      ? value.task_url
      : typeof links?.board_task === "string" ? links.board_task : null,
    watch_url: typeof value.watch_url === "string"
      ? value.watch_url
      : typeof links?.source_watch === "string" ? links.source_watch : null,
    mail: normalizeMailOrigin(value.mail, value.watch_id),
    action_history: normalizeActionHistory(value.action_history),
    action_history_truncated: value.action_history_truncated === true,
  };
}

function payloadError(response: Response, payload: unknown, fallback: string): GuardianInboxApiError {
  const outer = isRecord(payload) ? payload : null;
  const detail = outer && isRecord(outer.detail) ? outer.detail : outer;
  const code = detail && typeof detail.code === "string" ? detail.code : null;
  const recoveryAction = detail && typeof detail.recovery_action === "string" ? detail.recovery_action : null;
  const message =
    detail && typeof detail.recovery === "string"
      ? detail.recovery
      : detail && typeof detail.reason === "string"
        ? detail.reason
        : detail && typeof detail.message === "string"
          ? detail.message
          : `${fallback} (HTTP ${response.status}).`;
  return new GuardianInboxApiError(response.status, message, { code, recoveryAction, payload });
}

async function responsePayload(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

export async function fetchGuardianInbox(options: { limit?: number; cursor?: string | null; signal?: AbortSignal } = {}): Promise<GuardianInboxPage> {
  const params = new URLSearchParams();
  params.set("limit", String(Math.min(50, Math.max(1, Math.floor(options.limit ?? 50)))));
  if (options.cursor) params.set("cursor", options.cursor);
  const response = await apiFetch(`${API_URL}/api/guardian/inbox?${params.toString()}`, { signal: options.signal });
  if (options.signal?.aborted) throw new DOMException("Inbox request was cancelled.", "AbortError");
  const payload = await responsePayload(response);
  if (options.signal?.aborted) throw new DOMException("Inbox request was cancelled.", "AbortError");
  if (!response.ok) throw payloadError(response, payload, "Guardian inbox could not be loaded");

  const record = isRecord(payload) ? payload : {};
  const rawItems = Array.isArray(record.items) ? record.items : [];
  return {
    items: rawItems.flatMap((item) => {
      const normalized = normalizeGuardianInboxItem(item);
      return normalized ? [normalized] : [];
    }),
    next_cursor: typeof record.next_cursor === "string" ? record.next_cursor : null,
    last_confirmed_at: typeof record.last_confirmed_at === "string" ? record.last_confirmed_at : null,
  };
}

export async function fetchGuardianInboxItem(id: string, signal?: AbortSignal): Promise<GuardianInboxItem> {
  const response = await apiFetch(`${API_URL}/api/guardian/inbox/${encodeURIComponent(id)}`, { signal });
  if (signal?.aborted) throw new DOMException("Inbox detail request was cancelled.", "AbortError");
  const payload = await responsePayload(response);
  if (signal?.aborted) throw new DOMException("Inbox detail request was cancelled.", "AbortError");
  if (!response.ok) throw payloadError(response, payload, "Guardian inbox item could not be loaded");
  const record = isRecord(payload) && isRecord(payload.item) ? payload.item : payload;
  const normalized = normalizeGuardianInboxItem(record);
  if (!normalized) throw new GuardianInboxApiError(502, "Guardian inbox returned an incomplete item.", { payload });
  return normalized;
}

export async function applyGuardianInboxAction(
  id: string,
  request: GuardianInboxActionRequest,
): Promise<GuardianInboxActionResponse> {
  const response = await apiFetch(`${API_URL}/api/guardian/inbox/${encodeURIComponent(id)}/actions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
  const payload = await responsePayload(response);
  if (!response.ok) throw payloadError(response, payload, "Guardian inbox action could not be completed");

  const record = isRecord(payload) && isRecord(payload.result) ? payload.result : payload;
  const rawState = isRecord(record) && typeof record.state === "string" ? record.state : null;
  if (
    !isRecord(record)
    || typeof record.id !== "string"
    || record.id !== id
    || typeof record.receipt_id !== "string"
    || rawState === null
    || !KNOWN_STATES.has(rawState)
  ) {
    throw new GuardianInboxApiError(502, "Guardian inbox action returned an incomplete receipt.", {
      code: "invalid_inbox_action_receipt",
      payload,
    });
  }
  return {
    id: record.id,
    revision: Math.max(1, integerValue(record.revision, request.expected_revision)),
    state: rawState as GuardianInboxActionResponse["state"],
    task_id: typeof record.task_id === "string" ? record.task_id : null,
    receipt_id: record.receipt_id,
    recovery_action: typeof record.recovery_action === "string" ? record.recovery_action : null,
  };
}

let idempotencySequence = 0;

/** Create a client gesture key; the server remains the authority for replay. */
export function createGuardianInboxIdempotencyKey(itemId: string, action: string): string {
  const random = globalThis.crypto?.randomUUID?.();
  if (random) return `guardian:${itemId}:${action}:${random}`;
  idempotencySequence += 1;
  return `guardian:${itemId}:${action}:${Date.now()}:${idempotencySequence}`;
}
