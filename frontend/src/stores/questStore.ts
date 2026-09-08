import { create } from "zustand";
import { API_URL } from "../config/constants";
import type {
  GoalInfo,
  GoalLoopActionResponse,
  GoalLoopPayload,
  GoalLoopReceipt,
  GoalSnapshotInput,
  GoalStrategyCorrectionInput,
  GoalStrategyRollbackInput,
} from "../types";

interface DomainStat {
  active: number;
  completed: number;
  total: number;
  progress: number;
}

interface Dashboard {
  domains: Record<string, DomainStat>;
  active_count: number;
  completed_count: number;
  total_count: number;
}

export class GoalUpdateError extends Error {
  readonly code: string | null;
  readonly goalId: string;
  readonly currentRevision: number | null;

  constructor(
    goalId: string,
    message: string,
    options: { code?: string | null; currentRevision?: number | null } = {},
  ) {
    super(message);
    this.name = "GoalUpdateError";
    this.goalId = goalId;
    this.code = options.code ?? null;
    this.currentRevision = options.currentRevision ?? null;
  }
}

export interface GoalLoopErrorState {
  status: number;
  code: string | null;
  message: string;
  payload: unknown;
}

export class GoalLoopError extends Error {
  readonly status: number;
  readonly code: string | null;
  readonly payload: unknown;

  constructor(
    status: number,
    message: string,
    options: { code?: string | null; payload?: unknown } = {},
  ) {
    super(message);
    this.name = "GoalLoopError";
    this.status = status;
    this.code = options.code ?? null;
    this.payload = options.payload ?? null;
  }

  asState(): GoalLoopErrorState {
    return {
      status: this.status,
      code: this.code,
      message: this.message,
      payload: this.payload,
    };
  }
}

async function readResponsePayload(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

function errorDetail(payload: unknown): Record<string, unknown> | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
  const record = payload as Record<string, unknown>;
  if (record.detail && typeof record.detail === "object" && !Array.isArray(record.detail)) {
    return record.detail as Record<string, unknown>;
  }
  return record;
}

function goalLoopError(
  response: { status?: number },
  payload: unknown,
  fallback: string,
): GoalLoopError {
  const detail = errorDetail(payload);
  const code = typeof detail?.code === "string" ? detail.code : null;
  const message =
    typeof detail?.recovery === "string"
      ? detail.recovery
      : typeof detail?.reason === "string"
        ? detail.reason
        : `${fallback} (HTTP ${response.status ?? 0}).`;
  return new GoalLoopError(response.status ?? 0, message, { code, payload });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function isGoalCriterion(value: unknown): boolean {
  if (!isRecord(value)) return false;
  const target = value.target;
  return (
    typeof value.criterion_id === "string" &&
    typeof value.description === "string" &&
    (value.verifier_kind === null ||
      value.verifier_kind === "artifact_readback" ||
      value.verifier_kind === "external_readback" ||
      value.verifier_kind === "operator_attestation") &&
    (typeof target === "string" || isRecord(target)) &&
    Array.isArray(value.evidence_refs) &&
    value.evidence_refs.every((ref) => typeof ref === "string")
  );
}

const RECEIPT_STRING_FIELDS = [
  "event_type",
  "receipt_version",
  "receipt_type",
  "candidate_id",
  "outcome_id",
  "goal_id",
  "execution_status",
  "verification",
  "usefulness",
  "learning",
  "learning_record_id",
  "artifact_ref",
  "reason",
  // These fields are emitted by the candidate receipt contract and are kept
  // safe even though the cockpit currently renders only the outcome summary.
  "dedupe_key",
  "criterion_id",
  "action",
  "capability_id",
  "capability_version",
  "input_digest",
  "expected_outcome",
  "expires_at",
] as const;

/**
 * Keep receipt metadata renderable even when a backend or proxy returns an
 * unexpected shape.  Receipts are evidence, so silently stringifying an
 * object here would make the operator UI claim a value that was never part of
 * the typed contract.
 */
export function normalizeGoalLoopReceipt(value: unknown): GoalLoopReceipt | null {
  const record = isRecord(value) ? value : null;
  if (!record) return null;

  for (const field of RECEIPT_STRING_FIELDS) {
    const fieldValue = record[field];
    if (fieldValue !== undefined && fieldValue !== null && typeof fieldValue !== "string") {
      return null;
    }
  }

  const auditEventId = record.audit_event_id;
  if (
    auditEventId !== undefined &&
    auditEventId !== null &&
    typeof auditEventId !== "string" &&
    !(typeof auditEventId === "number" && Number.isFinite(auditEventId))
  ) {
    return null;
  }

  const goalRevision = record.goal_revision;
  if (
    goalRevision !== undefined &&
    goalRevision !== null &&
    (typeof goalRevision !== "number" || !Number.isInteger(goalRevision) || goalRevision < 1)
  ) {
    return null;
  }

  if (record.proposal_only !== undefined && typeof record.proposal_only !== "boolean") {
    return null;
  }

  if (
    record.evidence_refs !== undefined &&
    (!Array.isArray(record.evidence_refs) || !record.evidence_refs.every((ref) => typeof ref === "string"))
  ) {
    return null;
  }

  if (
    record.input_keys !== undefined &&
    (!Array.isArray(record.input_keys) || !record.input_keys.every((key) => typeof key === "string"))
  ) {
    return null;
  }

  if (record.content_redacted !== undefined && typeof record.content_redacted !== "boolean") {
    return null;
  }

  const createdAt = record.created_at;
  if (createdAt !== undefined && createdAt !== null && typeof createdAt !== "string") {
    return null;
  }

  // Preserve unknown safe receipt fields for forward compatibility, while
  // ensuring all fields rendered by GoalLoopPanel have passed the checks above.
  return { ...record } as GoalLoopReceipt;
}

function isStrategyDelta(value: unknown): boolean {
  if (!isRecord(value)) return false;
  return (
    typeof value.delta_id === "string" &&
    typeof value.goal_id === "string" &&
    typeof value.status === "string" &&
    typeof value.field_name === "string"
  );
}

function normalizeGoalLoopPayload(payload: unknown): GoalLoopPayload | null {
  if (!isRecord(payload) || !isRecord(payload.goal)) return null;
  const goal = payload.goal;
  if (
    typeof goal.id !== "string" ||
    typeof goal.title !== "string" ||
    typeof goal.status !== "string" ||
    typeof goal.revision !== "number" ||
    !Number.isInteger(goal.revision) ||
    goal.revision < 1 ||
    (payload.criterion !== null && !isGoalCriterion(payload.criterion)) ||
    !Array.isArray(payload.receipts) ||
    !Array.isArray(payload.strategy_deltas) ||
    !payload.strategy_deltas.every(isStrategyDelta)
  ) {
    return null;
  }

  const receipts = payload.receipts.map(normalizeGoalLoopReceipt);
  if (receipts.some((receipt) => receipt === null)) return null;

  return {
    ...payload,
    receipts: receipts as GoalLoopReceipt[],
  } as GoalLoopPayload;
}

let goalLoopRequestSequence = 0;

interface QuestStore {
  goals: GoalInfo[];
  goalTree: GoalInfo[];
  dashboard: Dashboard | null;
  loading: boolean;
  goalLoop: GoalLoopPayload | null;
  goalLoopGoalId: string | null;
  goalLoopLoading: boolean;
  goalLoopError: GoalLoopErrorState | null;
  goalLoopAction: string | null;

  loadGoals: (filters?: { level?: string; domain?: string; status?: string }) => Promise<void>;
  loadTree: () => Promise<void>;
  loadDashboard: () => Promise<void>;
  createGoal: (goal: Partial<GoalInfo>) => Promise<void>;
  updateGoal: (id: string, updates: {
    status?: string; title?: string; description?: string;
    level?: string; domain?: string; due_date?: string | null;
    expected_revision?: number;
  }) => Promise<void>;
  deleteGoal: (id: string) => Promise<void>;
  loadGoalLoop: (id: string) => Promise<void>;
  runGoalSnapshot: (id: string, input: GoalSnapshotInput) => Promise<GoalLoopActionResponse>;
  applyStrategyCorrection: (
    id: string,
    input: GoalStrategyCorrectionInput,
  ) => Promise<GoalLoopActionResponse>;
  rollbackStrategyCorrection: (
    id: string,
    deltaId: string,
    input: GoalStrategyRollbackInput,
  ) => Promise<GoalLoopActionResponse>;
  refresh: () => Promise<void>;
}

export const useQuestStore = create<QuestStore>((set, get) => ({
  goals: [],
  goalTree: [],
  dashboard: null,
  loading: false,
  goalLoop: null,
  goalLoopGoalId: null,
  goalLoopLoading: false,
  goalLoopError: null,
  goalLoopAction: null,

  loadGoals: async (filters) => {
    const params = new URLSearchParams();
    if (filters?.level) params.set("level", filters.level);
    if (filters?.domain) params.set("domain", filters.domain);
    if (filters?.status) params.set("status", filters.status);
    const qs = params.toString();
    try {
      const res = await fetch(`${API_URL}/api/goals${qs ? `?${qs}` : ""}`);
      if (res.ok) set({ goals: await res.json() });
    } catch (err) { console.error("Failed to load goals:", err); }
  },

  loadTree: async () => {
    try {
      const res = await fetch(`${API_URL}/api/goals/tree`);
      if (res.ok) set({ goalTree: await res.json() });
    } catch (err) { console.error("Failed to load goal tree:", err); }
  },

  loadDashboard: async () => {
    try {
      const res = await fetch(`${API_URL}/api/goals/dashboard`);
      if (res.ok) set({ dashboard: await res.json() });
    } catch (err) { console.error("Failed to load dashboard:", err); }
  },

  createGoal: async (goal) => {
    try {
      await fetch(`${API_URL}/api/goals`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(goal),
      });
      await get().refresh();
    } catch (err) { console.error("Failed to create goal:", err); }
  },

  updateGoal: async (id, updates) => {
    try {
      const res = await fetch(`${API_URL}/api/goals/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(updates),
      });
      if (!res.ok) {
        let detail: unknown = null;
        try {
          detail = await res.json();
        } catch {
          // Preserve the HTTP status in the user-facing error when the server
          // cannot provide structured recovery metadata.
        }
        const detailRecord =
          detail && typeof detail === "object" && !Array.isArray(detail)
            ? (detail as Record<string, unknown>)
            : null;
        const nestedDetail =
          detailRecord?.detail && typeof detailRecord.detail === "object" && !Array.isArray(detailRecord.detail)
            ? (detailRecord.detail as Record<string, unknown>)
            : detailRecord;
        const code = typeof nestedDetail?.code === "string" ? nestedDetail.code : null;
        const currentRevision =
          typeof nestedDetail?.current_revision === "number" ? nestedDetail.current_revision : null;
        const message =
          typeof nestedDetail?.recovery === "string"
            ? nestedDetail.recovery
            : `Goal update failed (HTTP ${res.status}).`;
        throw new GoalUpdateError(id, message, { code, currentRevision });
      }
      await get().refresh();
    } catch (err) {
      if (err instanceof GoalUpdateError) throw err;
      console.error("Failed to update goal:", err);
      throw new GoalUpdateError(id, "Goal update could not be completed.");
    }
  },

  deleteGoal: async (id) => {
    try {
      await fetch(`${API_URL}/api/goals/${id}`, { method: "DELETE" });
      await get().refresh();
    } catch (err) { console.error("Failed to delete goal:", err); }
  },

  loadGoalLoop: async (id) => {
    const requestSequence = ++goalLoopRequestSequence;
    const previous = get().goalLoopGoalId === id ? get().goalLoop : null;
    set({
      goalLoopGoalId: id,
      goalLoop: previous,
      goalLoopLoading: true,
      goalLoopError: null,
    });
    try {
      const res = await fetch(`${API_URL}/api/goals/${id}/loop`);
      const payload = await readResponsePayload(res);
      if (!res.ok) throw goalLoopError(res, payload, "Goal loop could not be loaded");
      const normalizedPayload = normalizeGoalLoopPayload(payload);
      if (!normalizedPayload || normalizedPayload.goal.id !== id) {
        throw new GoalLoopError(502, "Goal loop returned incomplete or mismatched metadata.", { payload });
      }
      if (requestSequence !== goalLoopRequestSequence || get().goalLoopGoalId !== id) return;
      set({ goalLoop: normalizedPayload, goalLoopError: null });
    } catch (err) {
      if (requestSequence !== goalLoopRequestSequence || get().goalLoopGoalId !== id) return;
      const failure =
        err instanceof GoalLoopError
          ? err
          : new GoalLoopError(0, "Goal loop is unavailable. Last-known evidence may be stale.");
      console.error("Failed to load goal loop:", err);
      set({ goalLoopError: failure.asState() });
    } finally {
      if (requestSequence === goalLoopRequestSequence && get().goalLoopGoalId === id) {
        set({ goalLoopLoading: false });
      }
    }
  },

  runGoalSnapshot: async (id, input) => {
    set({ goalLoopAction: "snapshot", goalLoopError: null });
    try {
      const res = await fetch(`${API_URL}/api/goals/${id}/snapshot`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(input),
      });
      const payload = await readResponsePayload(res);
      if (!res.ok) throw goalLoopError(res, payload, "Goal snapshot could not run");
      await get().loadGoalLoop(id);
      return (payload ?? {}) as GoalLoopActionResponse;
    } catch (err) {
      const failure =
        err instanceof GoalLoopError
          ? err
          : new GoalLoopError(0, "Goal snapshot could not run.");
      set({ goalLoopError: failure.asState() });
      throw failure;
    } finally {
      set({ goalLoopAction: null });
    }
  },

  applyStrategyCorrection: async (id, input) => {
    set({ goalLoopAction: "correction", goalLoopError: null });
    try {
      const res = await fetch(`${API_URL}/api/goals/${id}/strategy-corrections`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(input),
      });
      const payload = await readResponsePayload(res);
      if (!res.ok) throw goalLoopError(res, payload, "Strategy correction could not be applied");
      await get().refresh();
      await get().loadGoalLoop(id);
      return (payload ?? {}) as GoalLoopActionResponse;
    } catch (err) {
      const failure =
        err instanceof GoalLoopError
          ? err
          : new GoalLoopError(0, "Strategy correction could not be applied.");
      set({ goalLoopError: failure.asState() });
      throw failure;
    } finally {
      set({ goalLoopAction: null });
    }
  },

  rollbackStrategyCorrection: async (id, deltaId, input) => {
    set({ goalLoopAction: "rollback", goalLoopError: null });
    try {
      const res = await fetch(`${API_URL}/api/goals/${id}/strategy-corrections/${deltaId}/rollback`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(input),
      });
      const payload = await readResponsePayload(res);
      if (!res.ok) throw goalLoopError(res, payload, "Strategy correction could not be rolled back");
      await get().refresh();
      await get().loadGoalLoop(id);
      return (payload ?? {}) as GoalLoopActionResponse;
    } catch (err) {
      const failure =
        err instanceof GoalLoopError
          ? err
          : new GoalLoopError(0, "Strategy correction could not be rolled back.");
      set({ goalLoopError: failure.asState() });
      throw failure;
    } finally {
      set({ goalLoopAction: null });
    }
  },

  refresh: async () => {
    set({ loading: true });
    await Promise.all([get().loadTree(), get().loadDashboard()]);
    set({ loading: false });
  },
}));
