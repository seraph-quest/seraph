import { useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";

import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import {
  BrowserTaskApiError,
  browserTaskRequest,
  createBrowserInputArtifact,
  createBrowserWorkBoardTask,
} from "../../lib/browserTask";
import type {
  BrowserTaskPolicy,
  GoalInfo,
  WorkBoardExecutionLimits,
  WorkBoardInputArtifactCreateRequest,
  WorkBoardInputArtifactResponse,
  WorkBoardTask,
  WorkBoardTaskCreateRequest,
} from "../../types";

const CAPABILITY_ID = "browser.public-task.v1" as const;
const MAX_FIELD_BYTES = 2 * 1024;
const MAX_ACTIONS = 8;
const MAX_CHECKS = 8;
const MAX_HOSTS = 8;
const MAX_EXTRACT_CHARS = 65_536;
const REQUEST_TIMEOUT_MS = 15_000;

type CheckKind = "url_host" | "url_path_prefix" | "text_contains" | "text_sha256";
type ActionKind = "navigate" | "extract";

export interface BrowserTaskCheckDraft {
  kind: CheckKind;
  selector: string;
  value: string;
}

export interface BrowserTaskActionDraft {
  kind: ActionKind;
  url: string;
  selector: string;
  maxChars: string;
  attribute: string;
  expectedChecks: BrowserTaskCheckDraft[];
}

export interface BrowserTaskDraft {
  goalId: string;
  title: string;
  body: string;
  startUrl: string;
  prefixes: string;
  actions: BrowserTaskActionDraft[];
  finalChecks: BrowserTaskCheckDraft[];
}

export interface PendingBrowserSubmission {
  artifactRequest: WorkBoardInputArtifactCreateRequest;
  taskBase: Omit<WorkBoardTaskCreateRequest, "input_artifact_id">;
  artifact: WorkBoardInputArtifactResponse | null;
  taskRequest: WorkBoardTaskCreateRequest | null;
  draft: BrowserTaskDraft;
}

export interface BrowserTaskFormProps {
  goals: GoalInfo[];
  onCreated: (task: WorkBoardTask, receipt: BrowserTaskSubmissionReceipt) => void | Promise<void>;
  onClose: () => void;
  initialPending?: PendingBrowserSubmission | null;
  onPendingChange?: (pending: PendingBrowserSubmission | null) => void;
  ownerPrincipalId?: string | null;
  ownerSessionId?: string | null;
}

export interface BrowserTaskSubmissionReceipt {
  artifactId: string;
  digest: string;
  actionCount: number;
}

function makeIdempotencyKey(prefix: string): string {
  try {
    return `${prefix}:${crypto.randomUUID()}`;
  } catch {
    return `${prefix}:${Date.now().toString(36)}:${Math.random().toString(36).slice(2)}`;
  }
}

function emptyCheck(kind: CheckKind = "url_path_prefix"): BrowserTaskCheckDraft {
  return {
    kind,
    selector: kind.startsWith("text_") ? "main h1" : "",
    value: kind === "url_path_prefix" ? "/" : "",
  };
}

function emptyAction(kind: ActionKind = "navigate"): BrowserTaskActionDraft {
  return {
    kind,
    url: "",
    selector: kind === "extract" ? "main h1" : "",
    maxChars: "512",
    attribute: "",
    expectedChecks: [emptyCheck(kind === "extract" ? "text_contains" : "url_path_prefix")],
  };
}

function byteLength(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}

function parsePublicUrl(value: string, field: string): URL {
  if (!value.trim()) throw new Error(`${field} is required.`);
  if (byteLength(value) > MAX_FIELD_BYTES) throw new Error(`${field} must be at most 2 KiB.`);
  let parsed: URL;
  try {
    parsed = new URL(value.trim());
  } catch {
    throw new Error(`${field} must be a valid HTTPS URL.`);
  }
  if (parsed.protocol !== "https:" || parsed.username || parsed.password || parsed.hash || (parsed.port && parsed.port !== "443")) {
    throw new Error(`${field} must use HTTPS port 443 without credentials or a fragment.`);
  }
  if (!parsed.hostname) throw new Error(`${field} must include a hostname.`);
  if (/[\\]/.test(parsed.pathname) || /%(?:2e|2f|5c)/i.test(parsed.pathname)) {
    throw new Error(`${field} contains an ambiguous path escape.`);
  }
  return parsed;
}

function normalizedHost(parsed: URL): string {
  return parsed.hostname.toLowerCase().replace(/\.$/, "");
}

function buildCheck(check: BrowserTaskCheckDraft): Record<string, string> {
  const value = check.value.trim();
  if (!value || byteLength(value) > MAX_FIELD_BYTES) throw new Error("Every expected check needs a bounded value.");
  if ((check.kind === "text_contains" || check.kind === "text_sha256") && !check.selector.trim()) {
    throw new Error("Text checks need a CSS selector.");
  }
  if ((check.kind === "url_host" || check.kind === "url_path_prefix") && check.selector.trim()) {
    throw new Error("URL checks cannot include a selector.");
  }
  if (check.kind === "url_path_prefix" && !value.startsWith("/")) {
    throw new Error("URL path checks must start with '/'.");
  }
  if (check.kind === "text_sha256" && !/^[0-9a-f]{64}$/.test(value)) {
    throw new Error("Text SHA-256 checks must be 64 lowercase hexadecimal characters.");
  }
  const result: Record<string, string> = { kind: check.kind, value };
  if (check.kind.startsWith("text_")) result.selector = check.selector.trim();
  return result;
}

function buildBrowserInput(
  startUrl: string,
  prefixesText: string,
  actions: BrowserTaskActionDraft[],
  finalChecks: BrowserTaskCheckDraft[],
): Record<string, unknown> {
  const start = parsePublicUrl(startUrl, "Start URL");
  const prefixes = prefixesText.split("\n").map((value) => value.trim()).filter(Boolean);
  if (prefixes.length < 1 || prefixes.length > MAX_ACTIONS) throw new Error("Add between 1 and 8 approved URL prefixes.");
  const parsedPrefixes = prefixes.map((value, index) => parsePublicUrl(value, `Approved URL prefix ${index + 1}`));
  if (actions.length < 1 || actions.length > MAX_ACTIONS) throw new Error("Add between 1 and 8 browser actions.");
  if (finalChecks.length < 1 || finalChecks.length > MAX_CHECKS) throw new Error("Add between 1 and 8 final checks.");

  const hosts: string[] = [];
  const addHost = (host: string) => {
    if (!hosts.includes(host)) hosts.push(host);
  };
  addHost(normalizedHost(start));
  parsedPrefixes.forEach((prefix) => addHost(normalizedHost(prefix)));

  const serializedActions = actions.map((action, index) => {
    if (action.expectedChecks.length < 1 || action.expectedChecks.length > MAX_CHECKS) {
      throw new Error(`Action ${index + 1} needs between 1 and 8 expected checks.`);
    }
    const expectedChecks = action.expectedChecks.map(buildCheck);
    if (action.kind === "navigate") {
      const url = parsePublicUrl(action.url, `Navigate action ${index + 1} URL`);
      addHost(normalizedHost(url));
      return { kind: "navigate", url: url.toString(), expected_checks: expectedChecks };
    }
    if (!action.selector.trim() || byteLength(action.selector.trim()) > MAX_FIELD_BYTES) {
      throw new Error(`Extract action ${index + 1} needs a bounded CSS selector.`);
    }
    const maxChars = Number(action.maxChars);
    if (!Number.isSafeInteger(maxChars) || maxChars < 1 || maxChars > MAX_EXTRACT_CHARS) {
      throw new Error(`Extract action ${index + 1} max characters must be between 1 and 65536.`);
    }
    const result: Record<string, unknown> = {
      kind: "extract",
      selector: action.selector.trim(),
      max_chars: maxChars,
      expected_checks: expectedChecks,
    };
    if (action.attribute) result.attribute = action.attribute;
    return result;
  });

  const navigateCount = serializedActions.filter((action) => action.kind === "navigate").length;
  if (navigateCount + 1 > MAX_ACTIONS) {
    throw new Error("The initial page load counts as one navigation; use at most 7 navigate actions.");
  }

  if (hosts.length > MAX_HOSTS) throw new Error("Use at most 8 exact approved hosts.");
  return {
    schema_version: 1,
    start_url: start.toString(),
    allowed_hosts: hosts,
    approved_url_prefixes: parsedPrefixes.map((prefix) => prefix.toString()),
    actions: serializedActions,
    final_expected_checks: finalChecks.map(buildCheck),
  };
}

function errorText(error: unknown): string {
  if (error instanceof BrowserTaskApiError) return error.message;
  if (error instanceof Error && error.name === "AbortError") return "The request was cancelled or timed out.";
  if (error instanceof Error && error.message) return error.message;
  return "The browser task request failed.";
}

function isDefinitiveCorrection(error: unknown): boolean {
  return error instanceof BrowserTaskApiError
    && [400, 401, 403, 404, 405, 406, 415, 422].includes(error.status);
}

function isGoalStale(error: unknown): boolean {
  return error instanceof BrowserTaskApiError
    && error.status === 409
    && [
      "goal_revision_stale",
      "typed_input_goal_binding_mismatch",
      "input_artifact_goal_revision_stale",
      "input_artifact_revision_stale",
    ].includes(error.code);
}

function policyRuleLabel(policy: BrowserTaskPolicy, key: "allowlist" | "blocklist"): string {
  const rules = policy[key];
  if (!rules.known) return "Unavailable";
  if (!rules.rules.length) return "None configured";
  return `${rules.rules.join(", ")}${rules.truncated ? " (truncated; not exhaustive)" : ""}`;
}

export function BrowserTaskForm({
  goals: goalOptions,
  onCreated,
  onClose,
  initialPending = null,
  onPendingChange,
  ownerPrincipalId,
  ownerSessionId,
}: BrowserTaskFormProps) {
  const [goals, setGoals] = useState<GoalInfo[]>(goalOptions);
  const [goalId, setGoalId] = useState(initialPending?.draft.goalId ?? goalOptions[0]?.id ?? "");
  const [title, setTitle] = useState(initialPending?.draft.title ?? "Read approved public documentation");
  const [body, setBody] = useState(initialPending?.draft.body ?? "Open the approved public pages and verify the requested extract.");
  const [startUrl, setStartUrl] = useState(initialPending?.draft.startUrl ?? "");
  const [prefixes, setPrefixes] = useState(initialPending?.draft.prefixes ?? "");
  const [actions, setActions] = useState<BrowserTaskActionDraft[]>(initialPending?.draft.actions ?? [emptyAction()]);
  const [finalChecks, setFinalChecks] = useState<BrowserTaskCheckDraft[]>(initialPending?.draft.finalChecks ?? [emptyCheck("url_host")]);
  const [consentedFingerprint, setConsentedFingerprint] = useState<string | null>(null);
  const [limits, setLimits] = useState<WorkBoardExecutionLimits | null>(null);
  const [limitsError, setLimitsError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [submitState, setSubmitState] = useState<"idle" | "submitting" | "unknown">("idle");
  const [pending, setPending] = useState<PendingBrowserSubmission | null>(initialPending);
  const [goalStale, setGoalStale] = useState(false);
  const [submissionReceipt, setSubmissionReceipt] = useState<BrowserTaskSubmissionReceipt | null>(null);
  const [refreshingGoal, setRefreshingGoal] = useState(false);
  const mountedRef = useRef(true);
  const pendingRef = useRef<PendingBrowserSubmission | null>(initialPending);
  const submitControllerRef = useRef<AbortController | null>(null);
  const metadataControllerRef = useRef<AbortController | null>(null);
  const goalRefreshControllerRef = useRef<AbortController | null>(null);

  const updatePending = (next: PendingBrowserSubmission | null) => {
    pendingRef.current = next;
    setPending(next);
    onPendingChange?.(next);
  };

  useEffect(() => {
    setGoals(goalOptions);
    if (goalOptions.length > 0 && !goalOptions.some((goal) => goal.id === goalId)) {
      setGoalId(goalOptions[0]?.id ?? "");
    }
  }, [goalOptions, goalId]);

  const selectedGoal = useMemo(
    () => goals.find((goal) => goal.id === goalId) ?? null,
    [goals, goalId],
  );
  const goalRevision = selectedGoal?.revision ?? null;
  const policy = limits?.browser_task_policy ?? null;
  const consentFingerprint = useMemo(
    () => JSON.stringify({ goalId, goalRevision, startUrl, prefixes, actions, finalChecks }),
    [actions, finalChecks, goalId, goalRevision, prefixes, startUrl],
  );
  const consentAcknowledged = consentedFingerprint === consentFingerprint;
  const derivedHosts = useMemo(() => {
    const values = new Set<string>();
    for (const candidate of [startUrl, ...prefixes.split("\n"), ...actions.map((action) => action.url)]) {
      if (!candidate.trim()) continue;
      try { values.add(normalizedHost(new URL(candidate.trim()))); } catch { /* validation owns the message */ }
    }
    return Array.from(values);
  }, [actions, prefixes, startUrl]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      submitControllerRef.current?.abort();
      metadataControllerRef.current?.abort();
      goalRefreshControllerRef.current?.abort();
    };
  }, []);

  useEffect(() => {
    metadataControllerRef.current?.abort();
    setLimits(null);
    setLimitsError(null);
    if (!selectedGoal || !goalRevision) return;
    const controller = new AbortController();
    metadataControllerRef.current = controller;
    void browserTaskRequest<WorkBoardExecutionLimits>(
      `/goals/${encodeURIComponent(selectedGoal.id)}/execution-limits?goal_revision=${goalRevision}`,
      { signal: controller.signal },
    ).then((result) => {
      if (!mountedRef.current || controller.signal.aborted) return;
      if (result.goal_id !== selectedGoal.id || result.goal_revision !== goalRevision) {
        setLimitsError("The selected goal revision changed. Refresh goal metadata before submitting.");
        return;
      }
      setLimits(result);
    }).catch((error) => {
      if (mountedRef.current && !controller.signal.aborted) setLimitsError(errorText(error));
    });
    return () => controller.abort();
  }, [goalRevision, selectedGoal]);

  const updateAction = (index: number, update: Partial<BrowserTaskActionDraft>) => {
    setActions((current) => current.map((action, itemIndex) => itemIndex === index ? { ...action, ...update } : action));
  };

  const updateActionCheck = (actionIndex: number, checkIndex: number, update: Partial<BrowserTaskCheckDraft>) => {
    setActions((current) => current.map((action, itemIndex) => itemIndex !== actionIndex ? action : {
      ...action,
      expectedChecks: action.expectedChecks.map((check, itemCheckIndex) => itemCheckIndex === checkIndex ? { ...check, ...update } : check),
    }));
  };

  const updateFinalCheck = (index: number, update: Partial<BrowserTaskCheckDraft>) => {
    setFinalChecks((current) => current.map((check, itemIndex) => itemIndex === index ? { ...check, ...update } : check));
  };

  const resetPendingForCorrection = () => {
    updatePending(null);
    setSubmitState("idle");
    setGoalStale(false);
    setFormError(null);
  };

  const refreshGoalMetadata = async () => {
    goalRefreshControllerRef.current?.abort();
    setRefreshingGoal(true);
    const controller = new AbortController();
    goalRefreshControllerRef.current = controller;
    try {
      const response = await apiFetch(`${API_URL}/api/goals/tree`, { signal: controller.signal });
      const payload = await response.json().catch(() => null);
      if (!response.ok || !Array.isArray(payload)) throw new Error("Goal metadata could not be refreshed.");
      if (!mountedRef.current) return;
      setGoals(payload as GoalInfo[]);
      resetPendingForCorrection();
      setFormError("Goal metadata refreshed. Review the current revision and submit a new request.");
    } catch (error) {
      if (mountedRef.current && !controller.signal.aborted) setFormError(errorText(error));
    } finally {
      if (goalRefreshControllerRef.current === controller) goalRefreshControllerRef.current = null;
      if (mountedRef.current && !controller.signal.aborted) setRefreshingGoal(false);
    }
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (submitState === "submitting") return;
    setFormError(null);
    setSubmissionReceipt(null);
    let current = pendingRef.current;
    if (!current) {
      if (!consentAcknowledged) {
        setFormError("Review the exact HTTPS hosts and URL prefixes, then acknowledge this finite task consent.");
        return;
      }
      if (!selectedGoal || !goalRevision) {
        setFormError("Choose an owned goal with a current revision.");
        return;
      }
      try {
        const input = buildBrowserInput(startUrl, prefixes, actions, finalChecks);
        const artifactRequest: WorkBoardInputArtifactCreateRequest = {
          schema_version: 1,
          capability_id: CAPABILITY_ID,
          goal_id: selectedGoal.id,
          goal_revision: goalRevision,
          input,
          idempotency_key: makeIdempotencyKey("browser-input"),
        };
        const taskBase: Omit<WorkBoardTaskCreateRequest, "input_artifact_id"> = {
          title: title.trim(),
          body: body.trim(),
          goal_id: selectedGoal.id,
          goal_revision: goalRevision,
          status: "todo",
          capability_id: CAPABILITY_ID,
          idempotency_key: makeIdempotencyKey("browser-task"),
        };
        if (!taskBase.title || taskBase.title.length > 200) throw new Error("Enter a task title between 1 and 200 characters.");
        if (taskBase.body && taskBase.body.length > 4000) throw new Error("Task description must be at most 4000 characters.");
        current = {
          artifactRequest,
          taskBase,
          artifact: null,
          taskRequest: null,
          draft: { goalId, title: title.trim(), body: body.trim(), startUrl, prefixes, actions, finalChecks },
        };
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
        const artifact = await createBrowserInputArtifact(current.artifactRequest, controller.signal);
        if (!mountedRef.current) return;
        if (controller.signal.aborted) throw new Error("The browser task request was cancelled before its receipt was confirmed.");
        current = { ...current, artifact };
        updatePending(current);
      }
      if (!current.taskRequest) {
        if (!current.artifact?.artifact_id) throw new Error("The input-artifact receipt did not include an artifact ID.");
        const taskRequest: WorkBoardTaskCreateRequest = {
          ...current.taskBase,
          input_artifact_id: current.artifact.artifact_id,
        };
        current = { ...current, taskRequest };
        updatePending(current);
      }
      const created = await createBrowserWorkBoardTask(current.taskRequest!, controller.signal);
      if (!mountedRef.current) return;
      if (controller.signal.aborted) throw new Error("The browser task request was cancelled before its receipt was confirmed.");
      if (!created?.task?.task_id) throw new Error("The task receipt did not include a task ID.");
      const completedArtifact = current.artifact;
      if (!completedArtifact) throw new Error("The input-artifact receipt was lost before task confirmation.");
      if (completedArtifact.goal_id !== current.artifactRequest.goal_id
        || completedArtifact.goal_revision !== current.artifactRequest.goal_revision
        || completedArtifact.capability_id !== CAPABILITY_ID
        || created.task.goal_id !== current.taskRequest?.goal_id
        || created.task.goal_revision !== current.taskRequest?.goal_revision
        || created.task.capability_id !== CAPABILITY_ID
        || created.task.input_artifact_id !== completedArtifact.artifact_id
        || (ownerPrincipalId && created.task.owner_principal_id !== ownerPrincipalId)
        || (ownerSessionId && created.task.owner_session_id !== ownerSessionId)) {
        throw new BrowserTaskApiError(200, "receipt_invalid", "The task receipt does not match the requested goal, artifact, capability, or owner.");
      }
      updatePending(null);
      setSubmitState("idle");
      const receipt: BrowserTaskSubmissionReceipt = {
        artifactId: completedArtifact.artifact_id,
        digest: completedArtifact.typed_input_digest,
        actionCount: Array.isArray(current.artifactRequest.input.actions)
          ? current.artifactRequest.input.actions.length
          : 0,
      };
      setSubmissionReceipt(receipt);
      try {
        await onCreated(created.task, receipt);
      } catch {
        if (mountedRef.current) setFormError("The browser task was created, but the Work Board could not open its receipt. Refresh the board to recover it.");
      }
    } catch (error) {
      if (!mountedRef.current) return;
      if (isGoalStale(error)) {
        setSubmitState("unknown");
        setGoalStale(true);
        setFormError("The selected goal revision is stale. Refresh goal metadata before correcting and resubmitting.");
      } else if (isDefinitiveCorrection(error)) {
        resetPendingForCorrection();
        setFormError(`${errorText(error)} Correct the form and submit a new request.`);
      } else {
        setSubmitState("unknown");
        setFormError(timedOut
          ? "The request timed out without a receipt. The exact keys and payload are preserved; retry only when ready."
          : "The receipt was not confirmed. The exact keys and payload are preserved; retry the same request to reconcile it.");
      }
    } finally {
      window.clearTimeout(timeout);
      submitControllerRef.current = null;
    }
  };

  const addAction = (kind: ActionKind) => {
    if (actions.length >= MAX_ACTIONS || pending) return;
    setActions((current) => [...current, emptyAction(kind)]);
  };

  const addActionCheck = (index: number) => {
    if (pending) return;
    setActions((current) => current.map((action, itemIndex) => itemIndex === index && action.expectedChecks.length < MAX_CHECKS
      ? { ...action, expectedChecks: [...action.expectedChecks, emptyCheck()] }
      : action));
  };

  const addFinalCheck = () => {
    if (pending || finalChecks.length >= MAX_CHECKS) return;
    setFinalChecks((current) => [...current, emptyCheck()]);
  };

  const requestClose = () => {
    if (submitState === "submitting" || pendingRef.current) {
      setFormError("An outcome is still unconfirmed. Keep this form open and retry the exact request; closing would discard the recovery handle.");
      return;
    }
    onClose();
  };

  return (
    <div className="fixed inset-0 z-[95] flex items-center justify-center bg-black/70 p-4" role="presentation">
      <form
        className="max-h-[94vh] w-full max-w-4xl overflow-y-auto rounded border border-white/15 bg-slate-950 p-4 text-slate-100 shadow-2xl"
        role="dialog"
        aria-modal="true"
        aria-labelledby="browser-task-form-title"
        onSubmit={(event) => void submit(event)}
      >
        <div className="flex items-center justify-between gap-2">
          <div>
            <div className="cockpit-key">bounded public read lane</div>
            <h2 id="browser-task-form-title" className="text-lg font-semibold">Public browser task</h2>
          </div>
          <button type="button" className="cockpit-feedback-button" onClick={requestClose} disabled={submitState === "submitting"}>Close</button>
        </div>
        <p className="mt-1 text-xs opacity-75">HTTPS GET/HEAD navigation and bounded extraction only. GET/HEAD restrictions reduce the action surface; a public site may still have site-specific effects when visited, so this is not a universal no-mutation guarantee. The server owns policy, admission, durable execution, and recovery.</p>

        {formError && <div className="mt-3 rounded border border-amber-500/40 p-2 text-sm" role="alert">{formError}</div>}
        {submissionReceipt && (
        <div className="mt-3 rounded border border-emerald-500/40 p-2 text-sm" role="status">
            Input artifact reserved: <span className="font-mono break-all">{submissionReceipt.artifactId}</span> · {submissionReceipt.actionCount} browser action{submissionReceipt.actionCount === 1 ? "" : "s"} · SHA-256 <span className="font-mono break-all">{submissionReceipt.digest}</span>
          </div>
        )}
        {pending && (
          <div className="mt-3 rounded border border-cyan-500/40 p-2 text-sm" role="status">
            <div>Exact request is retained for manual reconciliation. No new key or payload will be generated.</div>
            {pending.artifact && <div className="mt-1 font-mono text-xs break-all">Artifact {pending.artifact.artifact_id} · SHA-256 {pending.artifact.typed_input_digest} · {Array.isArray(pending.artifactRequest.input.actions) ? pending.artifactRequest.input.actions.length : 0} actions</div>}
            {goalStale && (
              <button type="button" className="mt-2 cockpit-feedback-button" onClick={() => void refreshGoalMetadata()} disabled={refreshingGoal}>{refreshingGoal ? "Refreshing goal…" : "Refresh goal metadata and edit"}</button>
            )}
          </div>
        )}

        <fieldset disabled={Boolean(pending) || submitState === "submitting"} className="mt-3 grid gap-3">
          <div className="grid gap-3 sm:grid-cols-2">
            <label>Task title<input aria-label="Browser task title" className="cockpit-input mt-1 w-full" autoFocus maxLength={200} required value={title} onChange={(event) => setTitle(event.currentTarget.value)} /></label>
            <label>Owned goal<select aria-label="Browser goal" className="cockpit-input mt-1 w-full" required value={goalId} onChange={(event) => setGoalId(event.currentTarget.value)}><option value="">Choose a goal</option>{goals.map((goal) => <option key={goal.id} value={goal.id}>{goal.title} · revision {goal.revision ?? "unknown"}</option>)}</select></label>
          </div>
          <label>Bounded task description<textarea aria-label="Browser task description" className="cockpit-input mt-1 w-full" maxLength={4000} rows={2} value={body} onChange={(event) => setBody(event.currentTarget.value)} /></label>
          <div className="grid gap-3 sm:grid-cols-2">
            <label>Current goal revision<input aria-label="Browser goal revision" className="cockpit-input mt-1 w-full" readOnly value={goalRevision ?? "Unavailable"} /></label>
            <div className="rounded border border-white/10 p-2 text-xs"><div className="font-semibold">Admission</div><div className="mt-1">{limits ? `${policy?.limits.max_runtime_seconds ?? Math.min(limits.effective_max_runtime_seconds, 180)}s browser cap · ${policy?.limits.max_attempts ?? limits.attempt_limit} attempts · ${policy?.limits.max_outstanding_jobs ?? limits.max_outstanding_jobs ?? "goal limit"} outstanding` : limitsError ?? "Loading current goal policy…"}</div></div>
          </div>
          <label>Start URL<input aria-label="Browser start URL" className="cockpit-input mt-1 w-full font-mono" maxLength={MAX_FIELD_BYTES} placeholder="https://public.example/docs" required value={startUrl} onChange={(event) => setStartUrl(event.currentTarget.value)} /></label>
          <label>Exact approved URL prefixes<textarea aria-label="Approved URL prefixes" className="cockpit-input mt-1 w-full font-mono" rows={3} maxLength={MAX_FIELD_BYTES * MAX_ACTIONS} placeholder="https://public.example/docs\nhttps://public.example/reference" required value={prefixes} onChange={(event) => setPrefixes(event.currentTarget.value)} /></label>

          <section className="rounded border border-white/10 p-3" aria-label="Global browser site policy">
            <div className="font-semibold">Global site policy</div>
            {!policy && <div className="mt-1 text-xs text-amber-200">Policy metadata is unavailable. Runtime admission remains server-controlled; this display does not claim readiness.</div>}
            {policy && <div className="mt-1 grid gap-1 text-xs"><div>State: <strong>{policy.policy_state}</strong> · source: {policy.policy_source ?? "unavailable"}</div>{policy.policy_state === "unknown" && <div className="text-amber-200">Policy metadata is incomplete; this display does not claim readiness.</div>}<div>Allowlist: {policyRuleLabel(policy, "allowlist")}</div><div>Blocklist: {policyRuleLabel(policy, "blocklist")}</div><div>Limits: {policy.limits.max_actions} actions · {policy.limits.max_requests} requests · {policy.limits.max_extract_bytes} extract bytes · {policy.limits.ready_capacity} ready tasks · {policy.limits.inference} inference</div></div>}
            <div className="mt-2 text-xs">Exact hosts in this request: {derivedHosts.length ? derivedHosts.map((host) => <span key={host} className="mr-1 inline-block rounded bg-white/10 px-1 font-mono">{host}</span>) : "Add URLs to preview hosts."}</div>
            <label className="mt-3 flex items-start gap-2 text-xs"><input type="checkbox" checked={consentAcknowledged} onChange={(event) => setConsentedFingerprint(event.currentTarget.checked ? consentFingerprint : null)} /><span>I consent to this one bounded task using only the exact HTTPS hosts and URL prefixes above. The server rechecks policy, DNS, limits, ownership, and durable admission.</span></label>
          </section>

          <section className="rounded border border-white/10 p-3" aria-label="Browser actions">
            <div className="flex flex-wrap items-center justify-between gap-2"><div><div className="font-semibold">Navigate and extract actions</div><div className="text-xs opacity-75">The initial page load counts as one navigation; use at most 7 explicit navigate actions.</div></div><div className="flex gap-2"><button type="button" className="cockpit-feedback-button" onClick={() => addAction("navigate")} disabled={actions.length >= MAX_ACTIONS}>Add navigate</button><button type="button" className="cockpit-feedback-button" onClick={() => addAction("extract")} disabled={actions.length >= MAX_ACTIONS}>Add extract</button></div></div>
            {actions.map((action, actionIndex) => (
              <article key={`action-${actionIndex}`} className="mt-3 rounded border border-white/10 p-2" aria-label={`Browser action ${actionIndex + 1}`}>
                <div className="flex items-center justify-between gap-2"><strong>Action {actionIndex + 1}</strong><button type="button" className="cockpit-feedback-button" onClick={() => setActions((current) => current.length > 1 ? current.filter((_, index) => index !== actionIndex) : current)} disabled={actions.length <= 1}>Remove</button></div>
                <div className="mt-2 grid gap-2 sm:grid-cols-2"><label>Kind<select aria-label={`Browser action ${actionIndex + 1} type`} className="cockpit-input mt-1 w-full" value={action.kind} onChange={(event) => updateAction(actionIndex, { kind: event.currentTarget.value as ActionKind, expectedChecks: [emptyCheck(event.currentTarget.value === "extract" ? "text_contains" : "url_path_prefix")] })}><option value="navigate">Navigate</option><option value="extract">Extract</option></select></label>{action.kind === "navigate" ? <label>HTTPS URL<input aria-label={`Browser action ${actionIndex + 1} URL`} className="cockpit-input mt-1 w-full font-mono" maxLength={MAX_FIELD_BYTES} value={action.url} onChange={(event) => updateAction(actionIndex, { url: event.currentTarget.value })} /></label> : <><label>CSS selector<input aria-label={`Browser action ${actionIndex + 1} selector`} className="cockpit-input mt-1 w-full font-mono" maxLength={MAX_FIELD_BYTES} value={action.selector} onChange={(event) => updateAction(actionIndex, { selector: event.currentTarget.value })} /></label><label>Max extracted characters<input aria-label={`Browser action ${actionIndex + 1} max characters`} className="cockpit-input mt-1 w-full" type="number" min={1} max={MAX_EXTRACT_CHARS} value={action.maxChars} onChange={(event) => updateAction(actionIndex, { maxChars: event.currentTarget.value })} /></label><label>Read-only attribute<select aria-label={`Browser action ${actionIndex + 1} attribute`} className="cockpit-input mt-1 w-full" value={action.attribute} onChange={(event) => updateAction(actionIndex, { attribute: event.currentTarget.value })}><option value="">Inner text</option>{["href", "title", "aria-label", "alt", "datetime", "src"].map((attribute) => <option key={attribute} value={attribute}>{attribute}</option>)}</select></label></>}</div>
                <div className="mt-3"><div className="flex items-center justify-between gap-2 text-xs font-semibold"><span>Action expected checks</span><button type="button" className="cockpit-feedback-button" onClick={() => addActionCheck(actionIndex)} disabled={action.expectedChecks.length >= MAX_CHECKS}>Add check</button></div>{action.expectedChecks.map((check, checkIndex) => <CheckEditor key={`action-${actionIndex}-check-${checkIndex}`} label={`Action ${actionIndex + 1} check ${checkIndex + 1}`} value={check} onChange={(update) => updateActionCheck(actionIndex, checkIndex, update)} onRemove={() => setActions((current) => current.map((item, index) => index === actionIndex && item.expectedChecks.length > 1 ? { ...item, expectedChecks: item.expectedChecks.filter((_, index) => index !== checkIndex) } : item))} canRemove={action.expectedChecks.length > 1} />)}</div>
              </article>
            ))}
          </section>

          <section className="rounded border border-white/10 p-3" aria-label="Final browser checks"><div className="flex items-center justify-between gap-2"><div className="font-semibold">Final expected checks</div><button type="button" className="cockpit-feedback-button" onClick={addFinalCheck} disabled={finalChecks.length >= MAX_CHECKS}>Add check</button></div>{finalChecks.map((check, index) => <CheckEditor key={`final-check-${index}`} label={`Final check ${index + 1}`} value={check} onChange={(update) => updateFinalCheck(index, update)} onRemove={() => setFinalChecks((current) => current.length > 1 ? current.filter((_, itemIndex) => itemIndex !== index) : current)} canRemove={finalChecks.length > 1} />)}</section>
        </fieldset>

        <div className="mt-3 flex flex-wrap items-center justify-end gap-2"><button type="button" className="cockpit-feedback-button" onClick={requestClose} disabled={submitState === "submitting"}>Cancel</button><button type="submit" className="cockpit-feedback-button" disabled={submitState === "submitting" || !goalRevision}>{submitState === "submitting" ? "Submitting…" : pending ? "Retry exact request" : "Create public browser task"}</button></div>
      </form>
    </div>
  );
}

interface CheckEditorProps {
  label: string;
  value: BrowserTaskCheckDraft;
  onChange: (update: Partial<BrowserTaskCheckDraft>) => void;
  onRemove: () => void;
  canRemove: boolean;
}

function CheckEditor({ label, value, onChange, onRemove, canRemove }: CheckEditorProps) {
  return <div className="mt-2 grid gap-2 rounded bg-black/15 p-2 sm:grid-cols-[12rem_minmax(0,1fr)_auto]"><label className="text-xs">Type<select aria-label={`${label} type`} className="cockpit-input mt-1 w-full" value={value.kind} onChange={(event) => { const kind = event.currentTarget.value as CheckKind; onChange({ kind, selector: kind.startsWith("text_") ? value.selector || "main h1" : "", value: kind === "url_path_prefix" ? "/" : "" }); }}><option value="url_host">URL host</option><option value="url_path_prefix">URL path prefix</option><option value="text_contains">Text contains</option><option value="text_sha256">Text SHA-256</option></select></label>{value.kind.startsWith("text_") && <label className="text-xs">CSS selector<input aria-label={`${label} selector`} className="cockpit-input mt-1 w-full font-mono" maxLength={MAX_FIELD_BYTES} value={value.selector} onChange={(event) => onChange({ selector: event.currentTarget.value })} /></label>}<label className="text-xs sm:col-span-2">Expected value<input aria-label={`${label} value`} className="cockpit-input mt-1 w-full font-mono" maxLength={MAX_FIELD_BYTES} value={value.value} onChange={(event) => onChange({ value: event.currentTarget.value })} /></label><button type="button" className="cockpit-feedback-button self-end" onClick={onRemove} disabled={!canRemove}>Remove</button></div>;
}
