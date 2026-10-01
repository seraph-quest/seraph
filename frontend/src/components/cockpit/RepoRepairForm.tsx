import { useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";

import type { GoalInfo, WorkBoardTask, WorkBoardTaskCreateRequest } from "../../types";
import {
  createRepoRepairInputArtifact,
  createRepoRepairTask,
  REPO_REPAIR_CAPABILITY,
  RepoRepairApiError,
  type RepoRepairInput,
  type RepoRepairInputArtifactCreateRequest,
  type RepoRepairInputArtifactResponse,
} from "../../lib/repoRepair";

const MAX_TITLE_BYTES = 200;
const MAX_PROBLEM_BYTES = 4_000;
const MAX_CRITERIA = 8;
const MAX_CRITERION_BYTES = 1_000;
const MAX_SOURCE_PATHS = 8;
const MAX_ALLOWED_PATHS = 32;
const MAX_TEST_ARGS = 16;
const MAX_EVIDENCE_REFS = 16;
const MAX_PATH_BYTES = 512;
const MAX_REFERENCE_BYTES = 512;
const REQUEST_TIMEOUT_MS = 15_000;
export const REPO_REPAIR_TASK_BODY = "Repository repair request submitted for server-owned inspection and governed execution.";
const ALLOWED_TEST_FLAGS = new Set(["-q", "-x", "--maxfail=1", "--disable-warnings"]);
const SECRET_PATH_PARTS = new Set([
  ".git",
  ".env",
  ".envrc",
  "secret",
  "secrets",
  "credential",
  "credentials",
  "password",
  "passwd",
  "token",
  "tokens",
  "private",
  "private_key",
  "id_rsa",
  "vault",
]);
const BINARY_SUFFIXES = new Set([
  ".7z", ".bin", ".bmp", ".class", ".db", ".dll", ".gif", ".ico", ".jar", ".jpeg", ".jpg", ".lock", ".mp3", ".mp4", ".o", ".pdf", ".png", ".pyc", ".so", ".sqlite", ".tar", ".wasm", ".webp", ".zip", ".key", ".pem", ".p12", ".pfx",
]);

export interface RepoRepairDraft {
  goalId: string;
  repositoryPath: string;
  problemStatement: string;
  acceptanceCriteria: string;
  sourcePaths: string;
  allowedPaths: string;
  testArgs: string;
  evidenceRefs: string;
}

export interface PendingRepoRepairSubmission {
  artifactRequest: RepoRepairInputArtifactCreateRequest;
  taskBase: Omit<WorkBoardTaskCreateRequest, "input_artifact_id">;
  artifact: RepoRepairInputArtifactResponse | null;
  taskRequest: WorkBoardTaskCreateRequest | null;
  draft: RepoRepairDraft;
}

export interface RepoRepairSubmissionReceipt {
  artifactId: string;
  digest: string;
}

export interface RepoRepairFormProps {
  goals: GoalInfo[];
  onCreated: (task: WorkBoardTask, receipt: RepoRepairSubmissionReceipt) => void | Promise<void>;
  onClose: () => void;
  initialPending?: PendingRepoRepairSubmission | null;
  onPendingChange?: (pending: PendingRepoRepairSubmission | null) => void;
  ownerPrincipalId?: string | null;
  ownerSessionId?: string | null;
}

function makeIdempotencyKey(prefix: string): string {
  try {
    return `${prefix}:${crypto.randomUUID()}`;
  } catch {
    return `${prefix}:${Date.now().toString(36)}:${Math.random().toString(36).slice(2)}`;
  }
}

function byteLength(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}

function errorText(error: unknown): string {
  if (error instanceof RepoRepairApiError) {
    if (error.status === 401 || error.status === 403) return "Your current operator session cannot create this repair task.";
    if (error.status === 409) return `${error.message}${error.recovery ? ` ${error.recovery}` : " Retry the unchanged request to reconcile it."}`;
    if (error.status >= 400 && error.status < 500) return `${error.message} Correct the form and submit a new request.`;
    return error.message;
  }
  if (error instanceof DOMException && error.name === "AbortError") return "The repair request was cancelled.";
  if (error instanceof Error && error.message) return error.message;
  return "The repository repair request failed.";
}

function isDefinitiveCorrection(error: unknown): boolean {
  return error instanceof RepoRepairApiError
    && error.status >= 400
    && error.status < 500
    && ![408, 409, 429].includes(error.status);
}

function isSafeRepoPath(value: string, field: string): string {
  const normalized = value.trim();
  if (!normalized || byteLength(normalized) > MAX_PATH_BYTES || normalized.includes("\\") || normalized.includes("\u0000")) {
    throw new Error(`${field} must be a bounded relative path.`);
  }
  const parts = normalized.split("/");
  if (normalized.startsWith("/") || parts.some((part) => !part || part === "." || part === "..")) {
    throw new Error(`${field} must be a bounded relative path.`);
  }
  const lowerParts = parts.map((part) => part.toLowerCase());
  if (lowerParts.some((part) => SECRET_PATH_PARTS.has(part) || part.startsWith(".env"))) {
    throw new Error(`${field} names a protected path.`);
  }
  const suffix = normalized.slice(normalized.lastIndexOf("/") + 1).toLowerCase();
  const dot = suffix.lastIndexOf(".");
  if (dot >= 0 && BINARY_SUFFIXES.has(suffix.slice(dot))) {
    throw new Error(`${field} names a binary or secret path.`);
  }
  return parts.join("/");
}

function lines(value: string, field: string, minimum: number, maximum: number, itemBytes: number): string[] {
  const values = value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
  if (values.length < minimum || values.length > maximum) {
    throw new Error(`${field} must contain between ${minimum} and ${maximum} entries.`);
  }
  if (values.some((item) => byteLength(item) > itemBytes || item.includes("\u0000"))) {
    throw new Error(`${field} contains an entry over its bounded size.`);
  }
  return values;
}

function boundedReferenceLines(value: string): string[] {
  const values = value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
  if (values.length > MAX_EVIDENCE_REFS) throw new Error(`Evidence references must contain at most ${MAX_EVIDENCE_REFS} entries.`);
  if (values.some((item) => !item || byteLength(item) > MAX_REFERENCE_BYTES || item.includes("\u0000") || item.includes("\\"))) {
    throw new Error("Evidence references must be bounded opaque references without backslashes.");
  }
  return values;
}

function buildRepoRepairInput(draft: RepoRepairDraft): RepoRepairInput {
  const repositoryPath = isSafeRepoPath(draft.repositoryPath, "Repository path");
  const problemStatement = draft.problemStatement.trim();
  if (!problemStatement || byteLength(problemStatement) > MAX_PROBLEM_BYTES || problemStatement.includes("\u0000")) {
    throw new Error("Problem statement must be between 1 and 4000 UTF-8 bytes.");
  }
  const acceptanceCriteria = lines(draft.acceptanceCriteria, "Acceptance criteria", 1, MAX_CRITERIA, MAX_CRITERION_BYTES);
  const sourcePaths = lines(draft.sourcePaths, "Source paths", 1, MAX_SOURCE_PATHS, MAX_PATH_BYTES).map((value) => isSafeRepoPath(value, "Source path"));
  const allowedPaths = lines(draft.allowedPaths, "Allowed paths", 1, MAX_ALLOWED_PATHS, MAX_PATH_BYTES).map((value) => isSafeRepoPath(value, "Allowed path"));
  if (new Set(sourcePaths).size !== sourcePaths.length || new Set(allowedPaths).size !== allowedPaths.length) {
    throw new Error("Source and allowed paths must not contain duplicates.");
  }
  const allowed = new Set(allowedPaths);
  if (sourcePaths.some((value) => !allowed.has(value))) {
    throw new Error("Every source path must also appear in allowed paths.");
  }
  const testArgs = lines(draft.testArgs, "Test arguments", 1, MAX_TEST_ARGS, 4_096);
  let namedTestPath = false;
  const normalizedTestArgs = testArgs.map((value) => {
    if (value === "pytest" || ALLOWED_TEST_FLAGS.has(value)) return value;
    const path = isSafeRepoPath(value, "Test argument");
    if (!allowed.has(path)) throw new Error("Every test path must appear in allowed paths.");
    namedTestPath = true;
    return path;
  });
  if (!namedTestPath) throw new Error("Test arguments must name at least one allowed test path.");
  return {
    repository_path: repositoryPath,
    problem_statement: problemStatement,
    acceptance_criteria: acceptanceCriteria,
    source_paths: sourcePaths,
    allowed_paths: allowedPaths,
    test_args: normalizedTestArgs,
    evidence_refs: boundedReferenceLines(draft.evidenceRefs),
  };
}

function initialDraft(goalId: string): RepoRepairDraft {
  return {
    goalId,
    repositoryPath: "repo",
    problemStatement: "Describe the bounded repository failure and the smallest safe repair.",
    acceptanceCriteria: "The focused test passes.",
    sourcePaths: "src/app.py",
    allowedPaths: "src/app.py\ntests/test_app.py",
    testArgs: "pytest\ntests/test_app.py",
    evidenceRefs: "",
  };
}

export function RepoRepairForm({
  goals: goalOptions,
  onCreated,
  onClose,
  initialPending = null,
  onPendingChange,
  ownerPrincipalId,
  ownerSessionId,
}: RepoRepairFormProps) {
  const activeGoals = useMemo(
    () => goalOptions.filter((goal) => goal.status === "active" && typeof goal.revision === "number" && Number.isSafeInteger(goal.revision) && goal.revision > 0),
    [goalOptions],
  );
  const [goalId, setGoalId] = useState(initialPending?.draft.goalId ?? activeGoals[0]?.id ?? "");
  const [repositoryPath, setRepositoryPath] = useState(initialPending?.draft.repositoryPath ?? initialDraft("").repositoryPath);
  const [problemStatement, setProblemStatement] = useState(initialPending?.draft.problemStatement ?? initialDraft("").problemStatement);
  const [acceptanceCriteria, setAcceptanceCriteria] = useState(initialPending?.draft.acceptanceCriteria ?? initialDraft("").acceptanceCriteria);
  const [sourcePaths, setSourcePaths] = useState(initialPending?.draft.sourcePaths ?? initialDraft("").sourcePaths);
  const [allowedPaths, setAllowedPaths] = useState(initialPending?.draft.allowedPaths ?? initialDraft("").allowedPaths);
  const [testArgs, setTestArgs] = useState(initialPending?.draft.testArgs ?? initialDraft("").testArgs);
  const [evidenceRefs, setEvidenceRefs] = useState(initialPending?.draft.evidenceRefs ?? initialDraft("").evidenceRefs);
  const [pending, setPending] = useState<PendingRepoRepairSubmission | null>(initialPending);
  const [submitState, setSubmitState] = useState<"idle" | "submitting">("idle");
  const [formError, setFormError] = useState<string | null>(null);
  const [submissionReceipt, setSubmissionReceipt] = useState<RepoRepairSubmissionReceipt | null>(null);
  const mountedRef = useRef(true);
  const pendingRef = useRef<PendingRepoRepairSubmission | null>(initialPending);
  const submitControllerRef = useRef<AbortController | null>(null);
  const bindingKey = `${ownerPrincipalId ?? ""}\u0000${ownerSessionId ?? ""}`;
  const previousBindingKeyRef = useRef(bindingKey);

  const updatePending = (next: PendingRepoRepairSubmission | null) => {
    pendingRef.current = next;
    setPending(next);
    onPendingChange?.(next);
  };

  const selectedGoal = useMemo(() => activeGoals.find((goal) => goal.id === goalId) ?? null, [activeGoals, goalId]);
  const goalRevision = selectedGoal?.revision ?? null;

  useEffect(() => {
    setGoalId((current) => activeGoals.some((goal) => goal.id === current) ? current : activeGoals[0]?.id ?? "");
  }, [activeGoals]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      submitControllerRef.current?.abort();
    };
  }, []);

  useEffect(() => {
    if (previousBindingKeyRef.current === bindingKey) return;
    previousBindingKeyRef.current = bindingKey;
    submitControllerRef.current?.abort();
    pendingRef.current = null;
    setPending(null);
    onPendingChange?.(null);
    setSubmissionReceipt(null);
    setFormError("The authenticated owner session changed. The private repair draft was cleared; start a new request.");
    const next = initialDraft(activeGoals[0]?.id ?? "");
    setGoalId(next.goalId);
    setRepositoryPath(next.repositoryPath);
    setProblemStatement(next.problemStatement);
    setAcceptanceCriteria(next.acceptanceCriteria);
    setSourcePaths(next.sourcePaths);
    setAllowedPaths(next.allowedPaths);
    setTestArgs(next.testArgs);
    setEvidenceRefs(next.evidenceRefs);
  }, [activeGoals, bindingKey, onPendingChange]);

  const draft = (): RepoRepairDraft => ({
    goalId,
    repositoryPath,
    problemStatement,
    acceptanceCriteria,
    sourcePaths,
    allowedPaths,
    testArgs,
    evidenceRefs,
  });

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (submitState === "submitting") return;
    setFormError(null);
    setSubmissionReceipt(null);
    let current = pendingRef.current;
    if (!current) {
      if (!ownerPrincipalId || !ownerSessionId) {
        setFormError("Sign in to an authenticated owner session before creating a repository repair.");
        return;
      }
      if (!selectedGoal || goalRevision === null) {
        setFormError("Choose an active goal with a current revision.");
        return;
      }
      try {
        const input = buildRepoRepairInput(draft());
        const artifactRequest: RepoRepairInputArtifactCreateRequest = {
          schema_version: 1,
          capability_id: REPO_REPAIR_CAPABILITY,
          goal_id: selectedGoal.id,
          goal_revision: goalRevision,
          input,
          idempotency_key: makeIdempotencyKey("repo-repair-input"),
        };
        const title = `Repository repair: ${input.repository_path}`.slice(0, MAX_TITLE_BYTES);
        const taskBase: Omit<WorkBoardTaskCreateRequest, "input_artifact_id"> = {
          title,
          body: REPO_REPAIR_TASK_BODY,
          goal_id: selectedGoal.id,
          goal_revision: goalRevision,
          status: "todo",
          capability_id: REPO_REPAIR_CAPABILITY,
          idempotency_scope: "task",
          idempotency_key: makeIdempotencyKey("repo-repair-task"),
        };
        current = { artifactRequest, taskBase, artifact: null, taskRequest: null, draft: draft() };
        updatePending(current);
      } catch (error) {
        setFormError(errorText(error));
        return;
      }
    }

    const controller = new AbortController();
    submitControllerRef.current = controller;
    let timedOut = false;
    const timeout = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, REQUEST_TIMEOUT_MS);
    setSubmitState("submitting");
    try {
      if (!current.artifact) {
        const artifact = await createRepoRepairInputArtifact(current.artifactRequest, controller.signal);
        if (!mountedRef.current || controller.signal.aborted) throw new Error("The input-artifact receipt was not confirmed.");
        current = { ...current, artifact };
        updatePending(current);
      }
      if (!current.taskRequest) {
        const artifactId = current.artifact?.artifact_id;
        if (!artifactId) throw new Error("The input-artifact receipt did not include an artifact ID.");
        const taskRequest: WorkBoardTaskCreateRequest = { ...current.taskBase, input_artifact_id: artifactId };
        current = { ...current, taskRequest };
        updatePending(current);
      }
      const created = await createRepoRepairTask(current.taskRequest!, {
        goalId: current.taskBase.goal_id,
        goalRevision: current.taskBase.goal_revision,
        artifactId: current.artifact?.artifact_id ?? "",
        artifactDigest: current.artifact?.typed_input_digest ?? "",
        ownerPrincipalId: ownerPrincipalId ?? "",
        ownerSessionId: ownerSessionId ?? "",
      }, controller.signal);
      if (!mountedRef.current || controller.signal.aborted) throw new Error("The task receipt was not confirmed.");
      const completedArtifact = current.artifact;
      if (!completedArtifact) throw new Error("The input-artifact receipt was lost before task confirmation.");
      if (completedArtifact.goal_id !== current.artifactRequest.goal_id
        || completedArtifact.goal_revision !== current.artifactRequest.goal_revision
        || completedArtifact.capability_id !== REPO_REPAIR_CAPABILITY
        || created.task.input_artifact_id !== completedArtifact.artifact_id) {
        throw new RepoRepairApiError(200, "receipt_invalid", "The task receipt does not match the requested artifact or goal.");
      }
      updatePending(null);
      setSubmitState("idle");
      const receipt = { artifactId: completedArtifact.artifact_id, digest: completedArtifact.typed_input_digest };
      setSubmissionReceipt(receipt);
      try {
        await onCreated(created.task, receipt);
      } catch {
        if (mountedRef.current) setFormError("The repair task was created, but the Work Board could not open its receipt. Refresh the board to recover it.");
      }
    } catch (error) {
      if (!mountedRef.current) return;
      if (isDefinitiveCorrection(error)) {
        updatePending(null);
        setFormError(`${errorText(error)} The draft is preserved; correct it and submit a new request.`);
      } else {
        setFormError(timedOut
          ? "The request timed out without a receipt. The exact keys and payload are preserved; retry the same request to reconcile it."
          : "The receipt was not confirmed. The exact keys and payload are preserved; retry the same request to reconcile it.");
      }
    } finally {
      window.clearTimeout(timeout);
      submitControllerRef.current = null;
      if (mountedRef.current) setSubmitState("idle");
    }
  };

  const requestClose = () => {
    if (submitState === "submitting" || pendingRef.current) {
      setFormError("An outcome is still unconfirmed. Keep this form open and retry the exact request before closing it.");
      return;
    }
    onClose();
  };

  return (
    <div className="fixed inset-0 z-[95] flex items-center justify-center bg-black/70 p-4" role="presentation">
      <form className="max-h-[94vh] w-full max-w-4xl overflow-y-auto rounded border border-white/15 bg-slate-950 p-4 text-slate-100 shadow-2xl" role="dialog" aria-modal="true" aria-labelledby="repo-repair-form-title" onSubmit={(event) => void submit(event)}>
        <div className="flex items-center justify-between gap-2">
          <div><div className="cockpit-key">bounded repository repair lane</div><h2 id="repo-repair-form-title" className="text-lg font-semibold">Repository repair</h2></div>
          <button type="button" className="cockpit-feedback-button" onClick={requestClose} disabled={submitState === "submitting"}>Close</button>
        </div>
        <p className="mt-1 text-xs opacity-75">Describe the exact source and test scope for one owner-bound repair. This creates a Todo task only; the server controls source inspection, model egress, approval, sandbox execution, and readback.</p>
        {formError && <div className="mt-3 rounded border border-amber-500/40 p-2 text-sm" role="alert">{formError}</div>}
        {submissionReceipt && <div className="mt-3 rounded border border-emerald-500/40 p-2 text-sm" role="status">Input artifact reserved: <span className="font-mono break-all">{submissionReceipt.artifactId}</span> · SHA-256 <span className="font-mono break-all">{submissionReceipt.digest}</span></div>}
        {pending && <div className="mt-3 rounded border border-cyan-500/40 p-2 text-sm" role="status"><div>Exact request is retained for manual reconciliation. No new key or payload will be generated.</div>{pending.artifact && <div className="mt-1 font-mono text-xs break-all">Artifact {pending.artifact.artifact_id} · SHA-256 {pending.artifact.typed_input_digest}</div>}</div>}
        <fieldset disabled={Boolean(pending) || submitState === "submitting"} className="mt-3 grid gap-3">
          <div className="grid gap-3 sm:grid-cols-2">
            <label>Owned active goal<select aria-label="Repair goal" className="cockpit-input mt-1 w-full" required value={goalId} onChange={(event) => setGoalId(event.currentTarget.value)}><option value="">Choose an active goal</option>{activeGoals.map((goal) => <option key={goal.id} value={goal.id}>{goal.title} · revision {goal.revision}</option>)}</select></label>
            <label>Current goal revision<input aria-label="Repair goal revision" className="cockpit-input mt-1 w-full" readOnly value={goalRevision ?? "Unavailable"} /></label>
          </div>
          <label>Repository path<input aria-label="Repository path" className="cockpit-input mt-1 w-full font-mono" maxLength={MAX_PATH_BYTES} placeholder="repos/project" required value={repositoryPath} onChange={(event) => setRepositoryPath(event.currentTarget.value)} /></label>
          <label>Problem statement<textarea aria-label="Repair problem statement" className="cockpit-input mt-1 w-full" maxLength={MAX_PROBLEM_BYTES} rows={4} required value={problemStatement} onChange={(event) => setProblemStatement(event.currentTarget.value)} /></label>
          <div className="grid gap-3 sm:grid-cols-2">
            <label>Acceptance criteria <span className="text-xs opacity-70">one per line, up to 8</span><textarea aria-label="Repair acceptance criteria" className="cockpit-input mt-1 w-full" rows={5} required value={acceptanceCriteria} onChange={(event) => setAcceptanceCriteria(event.currentTarget.value)} /></label>
            <label>Source paths <span className="text-xs opacity-70">one per line, up to 8</span><textarea aria-label="Repair source paths" className="cockpit-input mt-1 w-full font-mono" rows={5} required value={sourcePaths} onChange={(event) => setSourcePaths(event.currentTarget.value)} /></label>
          </div>
          <label>Allowed paths <span className="text-xs opacity-70">one per line; source and test paths must be included</span><textarea aria-label="Repair allowed paths" className="cockpit-input mt-1 w-full font-mono" rows={5} required value={allowedPaths} onChange={(event) => setAllowedPaths(event.currentTarget.value)} /></label>
          <label>Focused test arguments <span className="text-xs opacity-70">one per line; pytest, -q, -x, --maxfail=1, --disable-warnings, and allowed paths only</span><textarea aria-label="Repair test arguments" className="cockpit-input mt-1 w-full font-mono" rows={4} required value={testArgs} onChange={(event) => setTestArgs(event.currentTarget.value)} /></label>
          <label>Evidence references <span className="text-xs opacity-70">optional opaque references, one per line, up to 16</span><textarea aria-label="Repair evidence references" className="cockpit-input mt-1 w-full font-mono" rows={3} value={evidenceRefs} onChange={(event) => setEvidenceRefs(event.currentTarget.value)} /></label>
        </fieldset>
        <div className="mt-3 flex flex-wrap justify-end gap-2"><button type="button" className="cockpit-feedback-button" onClick={requestClose} disabled={submitState === "submitting"}>Cancel</button><button type="submit" className="cockpit-feedback-button" disabled={submitState === "submitting" || !goalRevision || activeGoals.length === 0}>{submitState === "submitting" ? "Submitting…" : pending ? "Retry exact request" : "Create repository repair task"}</button></div>
      </form>
    </div>
  );
}
