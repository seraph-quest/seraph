import { useEffect, useMemo, useRef, useState } from "react";
import {
  GoalLoopError,
  normalizeGoalLoopReceipt,
  useQuestStore,
} from "../../stores/questStore";
import type {
  GoalInfo,
  GoalSuccessCriterion,
  GoalLoopPayload,
  GoalLoopReceipt,
  GoalStrategyDelta,
} from "../../types";

type GoalLoopViewState =
  | "loading"
  | "empty"
  | "active"
  | "awaiting_approval"
  | "stale"
  | "degraded"
  | "blocked"
  | "failed"
  | "unauthorized"
  | "partial_metadata"
  | "recovered";

interface Props {
  goal?: GoalInfo | null;
  onEdit?: (goal: GoalInfo) => void;
}

interface ActionFailure {
  code: string | null;
  message: string;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function latestReceipt(payload: GoalLoopPayload | null): GoalLoopReceipt | null {
  if (!payload || !Array.isArray(payload.receipts) || payload.receipts.length === 0) return null;
  return normalizeGoalLoopReceipt(payload.receipts[0]);
}

function hasMalformedReceipt(payload: GoalLoopPayload | null): boolean {
  return Boolean(
    payload &&
      Array.isArray(payload.receipts) &&
      payload.receipts.some((receipt) => normalizeGoalLoopReceipt(receipt) === null),
  );
}

function validCriterion(value: unknown): GoalSuccessCriterion | null {
  const record = asRecord(value);
  if (!record) return null;
  const target = record.target;
  if (
    typeof record.criterion_id !== "string" ||
    typeof record.description !== "string" ||
    (record.verifier_kind !== null &&
      record.verifier_kind !== "artifact_readback" &&
      record.verifier_kind !== "external_readback" &&
      record.verifier_kind !== "operator_attestation") ||
    (typeof target !== "string" && !asRecord(target)) ||
    !Array.isArray(record.evidence_refs) ||
    !record.evidence_refs.every((ref) => typeof ref === "string")
  ) {
    return null;
  }
  return record as unknown as GoalSuccessCriterion;
}

function receiptAxis(receipt: GoalLoopReceipt | null, key: "execution_status" | "verification" | "usefulness" | "learning") {
  const value = receipt?.[key];
  return typeof value === "string" && value.trim() ? value : "unknown";
}

function viewState({
  goal,
  payload,
  loading,
  error,
  actionFailure,
  recovered,
}: {
  goal?: GoalInfo | null;
  payload: GoalLoopPayload | null;
  loading: boolean;
  error: { status: number; code: string | null } | null;
  actionFailure: ActionFailure | null;
  recovered: boolean;
}): GoalLoopViewState {
  if (!goal) return "empty";
  if (loading) return "loading";
  const errorCode = actionFailure?.code ?? error?.code;
  const errorStatus = typeof error?.status === "number" ? error.status : 0;
  if (errorCode === "stale_goal_revision") return "stale";
  if (errorCode === "authentication_required" || errorCode === "session_unavailable" || errorStatus === 401 || errorStatus === 403) {
    return "unauthorized";
  }
  if (error && (errorStatus === 503 || errorStatus === 0)) return "degraded";
  if (errorStatus === 502) return "partial_metadata";
  if (errorStatus === 404) return "failed";
  if (error && errorStatus >= 400) return "degraded";
  if (!payload) return "partial_metadata";
  if (!payload.goal || typeof payload.goal.revision !== "number") return "partial_metadata";
  if (payload.goal.revision !== (goal.revision ?? payload.goal.revision)) return "stale";
  if (!Array.isArray(payload.receipts) || hasMalformedReceipt(payload)) return "partial_metadata";
  const receipt = latestReceipt(payload);
  if (receipt?.goal_revision && receipt.goal_revision !== payload.goal.revision) return "stale";
  const executionStatus = typeof receipt?.execution_status === "string"
    ? receipt.execution_status.toLowerCase()
    : null;
  if (executionStatus === "failed") return "failed";
  if (executionStatus === "blocked") return "blocked";
  if (executionStatus === "awaiting_approval" || executionStatus === "pending_approval" || executionStatus === "approval_required") {
    return "awaiting_approval";
  }
  const criterion = validCriterion(payload.criterion);
  if (!criterion || !criterion.verifier_kind) return "partial_metadata";
  if (recovered) return "recovered";
  return "active";
}

function stateLabel(state: GoalLoopViewState): string {
  return state.replace("_", " ").toUpperCase();
}

function stateMessage(state: GoalLoopViewState): string {
  switch (state) {
    case "empty": return "Select a priority to inspect its governed loop.";
    case "loading": return "Loading loop receipts…";
    case "awaiting_approval": return "An action is awaiting operator approval. Do not retry it as a new execution.";
    case "stale": return "This loop is stale. Refresh and review the current revision before acting.";
    case "degraded": return "Loop metadata is degraded. Last-known evidence is retained; retry after recovery.";
    case "blocked": return "The latest attempt is blocked. No success is claimed without execution and readback.";
    case "failed": return "The latest attempt failed or its receipt is unavailable.";
    case "unauthorized": return "Operator authority is unavailable. Controls are disabled.";
    case "partial_metadata": return "A bounded success criterion or receipt field is missing; outcome remains unknown.";
    case "recovered": return "The requested control completed and the loop was read back.";
    default: return "Inspect evidence before deciding the next action.";
  }
}

function formatReceiptTime(value?: string | null): string {
  if (!value) return "time unavailable";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function actionMessage(error: ActionFailure | null): string | null {
  return error ? error.message : null;
}

export function GoalLoopPanel({ goal, onEdit }: Props) {
  const payload = useQuestStore((state) => state.goalLoop);
  const loadedGoalId = useQuestStore((state) => state.goalLoopGoalId);
  const loading = useQuestStore((state) => state.goalLoopLoading);
  const loopError = useQuestStore((state) => state.goalLoopError);
  const activeAction = useQuestStore((state) => state.goalLoopAction);
  const loadGoalLoop = useQuestStore((state) => state.loadGoalLoop);
  const runGoalSnapshot = useQuestStore((state) => state.runGoalSnapshot);
  const applyStrategyCorrection = useQuestStore((state) => state.applyStrategyCorrection);
  const rollbackStrategyCorrection = useQuestStore((state) => state.rollbackStrategyCorrection);
  const updateGoal = useQuestStore((state) => state.updateGoal);

  const [actionFailure, setActionFailure] = useState<ActionFailure | null>(null);
  const [recovered, setRecovered] = useState(false);
  const [query, setQuery] = useState("");
  const [filePath, setFilePath] = useState("");
  const [priority, setPriority] = useState("");
  const [reason, setReason] = useState("");
  const correctionId = useRef<string | null>(null);

  useEffect(() => {
    setActionFailure(null);
    setRecovered(false);
    correctionId.current = null;
    if (goal) void loadGoalLoop(goal.id);
  }, [goal?.id, loadGoalLoop]);

  const activePayload = loadedGoalId === goal?.id ? payload : null;
  const criterion = validCriterion(activePayload ? activePayload.criterion : goal?.success_criterion);
  const target = asRecord(criterion?.target);
  const latest = latestReceipt(activePayload);
  const strategyDeltas = activePayload && Array.isArray(activePayload.strategy_deltas)
    ? activePayload.strategy_deltas
    : [];
  const state = viewState({
    goal,
    payload: activePayload,
    loading,
    error: loopError,
    actionFailure,
    recovered,
  });
  const receiptRetryableFailure = state === "failed" && !loopError && latest?.execution_status?.toLowerCase() === "failed";
  const effectsDisabled = !goal || Boolean(loopError) || [
    "loading",
    "stale",
    "degraded",
    "partial_metadata",
    "awaiting_approval",
    "unauthorized",
  ].includes(state) || (state === "failed" && !receiptRetryableFailure);
  const snapshotDisabled = effectsDisabled;
  const actionBusy = activeAction !== null;
  const canSnapshot = Boolean(goal && criterion?.verifier_kind && criterion.evidence_refs.length > 0);
  const canCorrect = criterion?.verifier_kind === "artifact_readback" && Boolean(target?.query);
  const parsedPriority = priority.trim() ? Number(priority) : undefined;
  const priorityValid = parsedPriority === undefined || (
    Number.isInteger(parsedPriority) && parsedPriority >= 0 && parsedPriority <= 100
  );
  const correctionReady = canCorrect && priorityValid && Boolean(query.trim() || filePath.trim() || priority.trim());

  useEffect(() => {
    setQuery(typeof target?.query === "string" ? target.query : "");
    setFilePath(typeof target?.file_path === "string" ? target.file_path : "");
    setPriority(typeof target?.priority === "number" ? String(target.priority) : "");
    setReason("");
  }, [activePayload?.goal.id, activePayload?.goal.revision, criterion?.criterion_id]);

  const axes = useMemo(
    () => [
      ["Execution", receiptAxis(latest, "execution_status")],
      ["Verification", receiptAxis(latest, "verification")],
      ["Usefulness", receiptAxis(latest, "usefulness")],
      ["Learning", receiptAxis(latest, "learning")],
    ] as const,
    [latest],
  );

  const handleFailure = (error: unknown, fallback: string) => {
    if (error instanceof GoalLoopError) {
      setActionFailure({ code: error.code, message: error.message });
    } else if (error instanceof Error) {
      const code = typeof (error as Error & { code?: unknown }).code === "string"
        ? (error as Error & { code: string }).code
        : null;
      setActionFailure({ code, message: error.message });
    } else {
      setActionFailure({ code: null, message: fallback });
    }
  };

  const refresh = () => {
    setActionFailure(null);
    setRecovered(false);
    if (goal) void loadGoalLoop(goal.id);
  };

  const handleSnapshot = async () => {
    if (!goal || snapshotDisabled || !canSnapshot) return;
    setActionFailure(null);
    try {
      await runGoalSnapshot(goal.id, {
        expected_revision: goal.revision ?? activePayload?.goal.revision ?? 1,
        file_path: typeof target?.file_path === "string" ? target.file_path : undefined,
        evidence_refs: criterion?.evidence_refs ?? [],
        expected_outcome: criterion?.description,
      });
      setRecovered(true);
    } catch (error) {
      handleFailure(error, "Goal snapshot could not run.");
    }
  };

  const handlePauseResume = async () => {
    if (!goal || effectsDisabled) return;
    setActionFailure(null);
    try {
      const expectedRevision = goal.revision ?? activePayload?.goal.revision;
      await updateGoal(goal.id, {
        status: goal.status === "paused" ? "active" : "paused",
        ...(typeof expectedRevision === "number" ? { expected_revision: expectedRevision } : {}),
      });
      await loadGoalLoop(goal.id);
      setRecovered(true);
    } catch (error) {
      handleFailure(error, "Priority state could not be changed.");
    }
  };

  const handleCorrection = async () => {
    if (!goal || effectsDisabled || !correctionReady) return;
    setActionFailure(null);
    correctionId.current ??= `goal-correction-${goal.id}-${Date.now()}`;
    try {
      await applyStrategyCorrection(goal.id, {
        correction_id: correctionId.current,
        expected_revision: goal.revision ?? activePayload?.goal.revision ?? 1,
        query: query.trim() || undefined,
        file_path: filePath.trim() || undefined,
        priority: parsedPriority,
        reason: reason.trim() || "Operator corrected the bounded goal strategy.",
      });
      correctionId.current = null;
      setRecovered(true);
    } catch (error) {
      handleFailure(error, "Strategy correction could not be applied.");
    }
  };

  const handleRollback = async (delta: GoalStrategyDelta) => {
    if (!goal || effectsDisabled || delta.status !== "applied") return;
    setActionFailure(null);
    try {
      await rollbackStrategyCorrection(goal.id, delta.delta_id, {
        expected_revision: goal.revision ?? activePayload?.goal.revision ?? 1,
        reason: "Operator rolled back the bounded strategy correction.",
      });
      setRecovered(true);
    } catch (error) {
      handleFailure(error, "Strategy correction could not be rolled back.");
    }
  };

  const errorText = actionMessage(actionFailure) ?? loopError?.message;

  return (
    <section
      className="border border-retro-border/20 rounded-sm p-2 mt-2"
      data-testid="goal-loop-panel"
      data-state={state}
      aria-labelledby="goal-loop-title"
    >
      <div className="flex items-center justify-between gap-2">
        <div>
          <div id="goal-loop-title" className="text-[10px] uppercase tracking-wider text-retro-border font-bold">
            Goal loop
          </div>
          {goal && <div className="text-[11px] text-retro-text mt-0.5">{goal.title}</div>}
        </div>
        <div className="flex items-center gap-1">
          <span className="text-[9px] uppercase tracking-wider text-retro-text/60" data-testid="goal-loop-state">
            {stateLabel(state)}
          </span>
          {goal && (
            <button
              type="button"
              onClick={refresh}
              className="text-[9px] text-retro-text/50 hover:text-retro-highlight px-1"
              aria-label="Refresh goal loop"
            >
              refresh
            </button>
          )}
        </div>
      </div>

      <p className="text-[9px] text-retro-text/50 mt-1" role="status" aria-live="polite">
        {stateMessage(state)}
      </p>

      {errorText && (
        <div className="text-[9px] text-amber-300 mt-1" role="alert">
          {errorText}
        </div>
      )}

      {loopError && activePayload && (
        <div className="text-[9px] text-amber-300/80 mt-1" role="note" data-testid="goal-loop-read-only">
          Last-known evidence retained · read-only until loop metadata recovers.
        </div>
      )}

      {goal && (
        <>
          <div className="flex flex-wrap gap-x-2 gap-y-1 mt-2 text-[9px] text-retro-text/60">
            <span>revision {goal.revision ?? activePayload?.goal.revision ?? "unknown"}</span>
            <span>status {goal.status}</span>
            {criterion && <span>criterion {criterion.criterion_id}</span>}
          </div>

          <div className="grid grid-cols-2 gap-1 mt-2" aria-label="Goal outcome axes">
            {axes.map(([label, value]) => (
              <div key={label} className="border border-retro-text/10 rounded-sm px-1.5 py-1">
                <div className="text-[8px] uppercase tracking-wider text-retro-text/40">{label}</div>
                <div className="text-[10px] text-retro-text" data-testid={`goal-axis-${label.toLowerCase()}`}>
                  {value}
                </div>
              </div>
            ))}
          </div>

          {criterion ? (
            <div className="mt-2 text-[9px] text-retro-text/60" data-testid="goal-loop-criterion">
              <span className="text-retro-text/40 uppercase tracking-wider">Evidence target: </span>
              {criterion.description}
              {criterion.evidence_refs.length > 0 && ` · ${criterion.evidence_refs.length} evidence ref${criterion.evidence_refs.length === 1 ? "" : "s"}`}
            </div>
          ) : (
            <div className="mt-2 text-[9px] text-amber-300">No bounded success criterion is configured.</div>
          )}

          {latest && (
            <div className="mt-2 border-t border-retro-text/10 pt-1 text-[9px] text-retro-text/50">
              latest receipt · {latest.receipt_type ?? latest.event_type ?? "unknown"} · {formatReceiptTime(latest.created_at)}
              {typeof latest.reason === "string" && latest.reason && <div className="text-retro-text/40">{latest.reason}</div>}
              {typeof latest.artifact_ref === "string" && latest.artifact_ref && <div className="text-retro-text/40">artifact: {latest.artifact_ref}</div>}
            </div>
          )}

          <div className="flex flex-wrap gap-1 mt-2">
            <button
              type="button"
              onClick={() => onEdit?.(goal)}
              disabled={!onEdit || effectsDisabled}
              className="text-[9px] border border-retro-text/20 px-1.5 py-1 text-retro-text/70 hover:text-retro-highlight disabled:opacity-30"
            >
              edit
            </button>
            <button
              type="button"
              onClick={() => void handlePauseResume()}
              disabled={effectsDisabled || actionBusy}
              className="text-[9px] border border-retro-text/20 px-1.5 py-1 text-retro-text/70 hover:text-retro-highlight disabled:opacity-30"
            >
              {goal.status === "paused" ? "resume" : "pause"}
            </button>
            <button
              type="button"
              onClick={() => void handleSnapshot()}
              disabled={snapshotDisabled || actionBusy || !canSnapshot}
              title={!canSnapshot ? "Configure a verifier and evidence before requesting a snapshot." : undefined}
              className="text-[9px] border border-retro-highlight/30 px-1.5 py-1 text-retro-highlight hover:bg-retro-highlight/10 disabled:opacity-30"
            >
              {activeAction === "snapshot" ? "running…" : "run snapshot"}
            </button>
          </div>

          {canCorrect && (
            <div className="mt-2 border-t border-retro-text/10 pt-2">
              <div className="text-[9px] uppercase tracking-wider text-retro-text/50">Bounded strategy correction</div>
              <div className="grid grid-cols-1 gap-1 mt-1">
                <input
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="replacement query"
                  aria-label="Replacement query"
                  className="bg-transparent border border-retro-text/15 rounded-sm px-1.5 py-1 text-[9px] text-retro-text outline-none focus:border-retro-highlight"
                />
                <input
                  value={filePath}
                  onChange={(event) => setFilePath(event.target.value)}
                  placeholder="workspace file path"
                  aria-label="Workspace file path"
                  className="bg-transparent border border-retro-text/15 rounded-sm px-1.5 py-1 text-[9px] text-retro-text outline-none focus:border-retro-highlight"
                />
                <div className="flex gap-1">
                  <input
                    value={priority}
                    onChange={(event) => setPriority(event.target.value)}
                    placeholder="priority 0–100"
                    aria-label="Strategy priority"
                    inputMode="numeric"
                    className="w-24 bg-transparent border border-retro-text/15 rounded-sm px-1.5 py-1 text-[9px] text-retro-text outline-none focus:border-retro-highlight"
                  />
                  <input
                    value={reason}
                    onChange={(event) => setReason(event.target.value)}
                    placeholder="why change it?"
                    aria-label="Correction reason"
                    className="flex-1 bg-transparent border border-retro-text/15 rounded-sm px-1.5 py-1 text-[9px] text-retro-text outline-none focus:border-retro-highlight"
                  />
                </div>
              </div>
              <button
                type="button"
                onClick={() => void handleCorrection()}
                disabled={effectsDisabled || actionBusy || !correctionReady}
                className="mt-1 text-[9px] border border-retro-text/20 px-1.5 py-1 text-retro-text/70 hover:text-retro-highlight disabled:opacity-30"
              >
                {activeAction === "correction" ? "applying…" : "apply correction"}
              </button>
              {!priorityValid && <div className="text-[9px] text-rose-400 mt-1">Priority must be an integer from 0 to 100.</div>}
            </div>
          )}

          {strategyDeltas.length > 0 && (
            <div className="mt-2 border-t border-retro-text/10 pt-2">
              <div className="text-[9px] uppercase tracking-wider text-retro-text/50">Strategy history</div>
              <div className="flex flex-col gap-1 mt-1">
                {strategyDeltas.map((delta) => (
                  <div key={delta.delta_id} className="flex items-center justify-between gap-2 text-[9px] text-retro-text/50">
                    <span className="truncate" title={delta.reason}>
                      {delta.status} · {delta.field_name}
                    </span>
                    <button
                      type="button"
                      onClick={() => void handleRollback(delta)}
                      disabled={effectsDisabled || actionBusy || delta.status !== "applied"}
                      className="shrink-0 text-[9px] text-retro-text/50 hover:text-retro-highlight disabled:opacity-30"
                    >
                      rollback
                    </button>
                  </div>
                ))}
              </div>
            </div>
          )}
        </>
      )}
    </section>
  );
}
