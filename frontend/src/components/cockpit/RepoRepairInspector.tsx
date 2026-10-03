import { useEffect, useMemo, useRef, useState } from "react";

import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import type {
  RepoRepairExecutorKind,
  WorkBoardRepoRepairProjection,
  WorkBoardRepoRepairExecutorPosture,
  WorkBoardRepoRepairSourcePreview,
  WorkBoardRepoRepairProcessCleanup,
} from "../../types";

interface RepoRepairInspectorProps {
  jobId: string;
  onOpenApprovals?: () => void;
  /** The authenticated operator binding currently mounted in the cockpit. */
  ownerPrincipalId?: string | null;
  ownerSessionId?: string | null;
  /** The owner binding returned by the selected WorkBoard task. */
  taskOwnerPrincipalId?: string | null;
  taskOwnerSessionId?: string | null;
}

interface ApiErrorPayload {
  detail?: string | { code?: string; message?: string; recovery_action?: string };
}

function errorMessage(payload: unknown, fallback: string): string {
  if (!payload || typeof payload !== "object") return fallback;
  const detail = (payload as ApiErrorPayload).detail;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (detail && typeof detail === "object") {
    const message = typeof detail.message === "string" ? detail.message : detail.code;
    if (message) return message;
  }
  return fallback;
}

function statusLabel(value: string | null | undefined): string {
  return String(value || "unknown").replace(/_/g, " ");
}

function safeDigest(value: string | null | undefined): string {
  if (!value) return "unavailable";
  return `${value.slice(0, 12)}…${value.slice(-8)}`;
}

function isFiniteFutureTimestamp(value: unknown): value is string {
  if (typeof value !== "string" || !value.trim()) return false;
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) && timestamp > Date.now();
}

function makeRequestKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return `repo-repair-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

const REPAIR_REQUEST_TIMEOUT_MS = 15_000;
const SOURCE_PREVIEW_MAX_FILES = 8;
const SOURCE_PREVIEW_MAX_PACKET_BYTES = 64 * 1024;
const SOURCE_PREVIEW_MAX_OUTPUT_TOKENS = 4096;

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

const REPO_REPAIR_EXECUTOR_KINDS: RepoRepairExecutorKind[] = ["local", "docker_rootless", "docker_rootful"];
const REPO_SANDBOX_PROFILE = "repo-python-pytest-v1";
const LOCAL_HOST_ACCESS = "explicit_job_approval_required";

const POSTURE_VALUES: Record<RepoRepairExecutorKind, {
  isolation: string[];
  network: string[];
  resources: string[];
}> = {
  local: {
    isolation: ["none"],
    network: ["not_verified"],
    resources: ["admission_and_wall_timeout_only"],
  },
  docker_rootless: {
    isolation: ["rootless_container", "unverified"],
    network: ["none", "unverified"],
    resources: ["verified_fixed_limits", "unverified"],
  },
  docker_rootful: {
    isolation: ["rootful_container", "unverified"],
    network: ["none", "unverified"],
    resources: ["verified_fixed_limits", "unverified"],
  },
};

function boundedMetadataString(value: unknown, fallback: string, maxBytes = 512): string {
  return typeof value === "string" && value.length <= maxBytes && !value.includes("\u0000") ? value : fallback;
}

function isSafeDigest(value: unknown): value is string | null | undefined {
  return value === undefined || value === null || (typeof value === "string" && /^[0-9a-f]{64}$/.test(value));
}

const MAX_PERMISSION_BYTES = 128;

function isBoundedPermission(value: unknown): value is string {
  return typeof value === "string"
    && value.trim().length > 0
    && value.length <= MAX_PERMISSION_BYTES
    && !value.includes("\u0000");
}

function normalizeExecutorMetadata(payload: Record<string, unknown>): Pick<
  WorkBoardRepoRepairProjection,
  "executor_kind" | "executor_profile" | "executor_posture" | "executor_posture_digest" | "required_permissions" | "local_host_execution_required" | "preparation_ready" | "execution_ready"
> {
  const explicitExecutorKind = payload.executor_kind !== undefined;
  const kind = payload.executor_kind === undefined ? "docker_rootless" : payload.executor_kind;
  if (!REPO_REPAIR_EXECUTOR_KINDS.includes(kind as RepoRepairExecutorKind)) {
    throw new Error("The repair status response has an unsupported executor.");
  }
  const executorKind = kind as RepoRepairExecutorKind;
  const rawPosture = payload.executor_posture;
  if (rawPosture !== undefined && !isRecord(rawPosture)) {
    throw new Error("The repair status response has malformed executor posture metadata.");
  }
  if (explicitExecutorKind && rawPosture === undefined) {
    throw new Error("The repair status response is missing executor posture metadata.");
  }
  const posture = (rawPosture ?? {
    kind: executorKind,
    profile: REPO_SANDBOX_PROFILE,
    isolation_claim: "unverified",
    network_isolation: "unverified",
    resource_enforcement: "unverified",
    image_digest: null,
    limits_digest: null,
    local_host_execution_required: false,
  }) as Record<string, unknown>;
  if (explicitExecutorKind && ["kind", "profile", "isolation_claim", "network_isolation", "resource_enforcement", "limits_digest"].some((key) => !Object.prototype.hasOwnProperty.call(posture, key))) {
    throw new Error("The repair status response has incomplete executor posture metadata.");
  }
  if (posture.kind !== undefined && posture.kind !== executorKind) {
    throw new Error("The repair status response has mismatched executor posture metadata.");
  }
  if (posture.profile !== undefined && ![REPO_SANDBOX_PROFILE, "repo-node24-npm-v1"].includes(String(posture.profile))) {
    throw new Error("The repair status response has an unsupported executor profile.");
  }
  const imageDigest = posture.image_digest;
  if (imageDigest !== undefined && imageDigest !== null && (typeof imageDigest !== "string" || imageDigest.length > 512 || imageDigest.includes("\u0000"))) {
    throw new Error("The repair status response has malformed executor posture metadata.");
  }
  if (executorKind === "local" && imageDigest !== undefined && imageDigest !== null) {
    throw new Error("The local executor cannot carry an image digest.");
  }
  const rawIsolation = posture.isolation_claim;
  const rawNetwork = posture.network_isolation;
  const rawResources = posture.resource_enforcement;
  for (const item of [rawIsolation, rawNetwork, rawResources]) {
    if (item !== undefined && (typeof item !== "string" || item.length > 128 || item.includes("\u0000"))) {
      throw new Error("The repair status response has malformed executor posture metadata.");
    }
  }
  if (rawIsolation !== undefined && !POSTURE_VALUES[executorKind].isolation.includes(rawIsolation as string)) {
    throw new Error("The repair status response has contradictory isolation metadata.");
  }
  if (rawNetwork !== undefined && !POSTURE_VALUES[executorKind].network.includes(rawNetwork as string)) {
    throw new Error("The repair status response has contradictory network metadata.");
  }
  if (rawResources !== undefined && !POSTURE_VALUES[executorKind].resources.includes(rawResources as string)) {
    throw new Error("The repair status response has contradictory resource metadata.");
  }
  if (!isSafeDigest(posture.limits_digest)) {
    throw new Error("The repair status response has malformed limits metadata.");
  }
  const hostAccess = posture.host_access;
  if (hostAccess !== undefined && (!isBoundedPermission(hostAccess) || (executorKind !== "local" || hostAccess !== LOCAL_HOST_ACCESS))) {
    throw new Error("The repair status response has contradictory host access metadata.");
  }
  const localHost = payload.local_host_execution_required ?? posture.local_host_execution_required;
  const effectiveLocalHost = localHost === undefined && hostAccess === LOCAL_HOST_ACCESS ? true : localHost;
  if (effectiveLocalHost !== undefined && typeof effectiveLocalHost !== "boolean") {
    throw new Error("The repair status response has malformed host permission metadata.");
  }
  if (executorKind === "local" && effectiveLocalHost === false) {
    throw new Error("The local executor cannot disable its host permission boundary.");
  }
  if (executorKind !== "local" && effectiveLocalHost === true) {
    throw new Error("A Docker executor cannot claim local host permission.");
  }
  if (hostAccess === LOCAL_HOST_ACCESS && effectiveLocalHost !== true) {
    throw new Error("The repair status response has contradictory host permission metadata.");
  }
  const permissions = payload.required_permissions;
  if (permissions !== undefined && (!Array.isArray(permissions) || permissions.some((item) => !isBoundedPermission(item)))) {
    throw new Error("The repair status response has malformed permission metadata.");
  }
  const postureValue: WorkBoardRepoRepairExecutorPosture = {
    kind: executorKind,
    profile: boundedMetadataString(posture.profile, REPO_SANDBOX_PROFILE),
    isolation_claim: boundedMetadataString(posture.isolation_claim, executorKind === "local" ? "none" : "unverified"),
    network_isolation: boundedMetadataString(posture.network_isolation, executorKind === "local" ? "not_verified" : "unverified"),
    resource_enforcement: boundedMetadataString(posture.resource_enforcement, executorKind === "local" ? "admission_and_wall_timeout_only" : "unverified"),
    image_digest: typeof imageDigest === "string" ? imageDigest : null,
    limits_digest: typeof posture.limits_digest === "string" ? posture.limits_digest : null,
    ...(hostAccess === undefined ? {} : { host_access: hostAccess }),
    local_host_execution_required: effectiveLocalHost ?? executorKind === "local",
  };
  if (posture.profile === "repo-node24-npm-v1") {
    if (posture.execution_plan !== undefined) {
      if (!isRecord(posture.execution_plan) || !Array.isArray(posture.execution_plan.commands) || posture.execution_plan.commands.length > 2) {
        throw new Error("The Node repair execution plan is malformed.");
      }
      const commands = posture.execution_plan.commands;
      if (commands.some((command) => !isRecord(command) || !["test", "build"].includes(String(command.script)) || typeof command.body !== "string" || command.body.length > 4096 || !Array.isArray(command.argv) || command.argv.length > 10 || command.argv.some((arg) => typeof arg !== "string" || arg.length > 512))) {
        throw new Error("The Node repair command preview is malformed.");
      }
      postureValue.execution_plan = posture.execution_plan;
    }
    for (const key of ["node_version", "node_sha256", "npm_version", "dependency_limits", "process_supervision"]) {
      postureValue[key] = posture[key];
    }
  }
  const optionalDigest = payload.executor_posture_digest;
  if (explicitExecutorKind
    ? (typeof optionalDigest !== "string" || !/^[0-9a-f]{64}$/.test(optionalDigest))
    : !isSafeDigest(optionalDigest)) {
    throw new Error("The repair status response has malformed posture digest metadata.");
  }
  const expectedExecutorProfile = `${executorKind}:${postureValue.profile}`;
  if (explicitExecutorKind && payload.executor_profile !== expectedExecutorProfile) {
    throw new Error("The repair status response has an unsupported executor profile.");
  }
  if (!explicitExecutorKind && payload.executor_profile !== undefined && payload.executor_profile !== expectedExecutorProfile) {
    throw new Error("The repair status response has an unsupported executor profile.");
  }
  const readiness = (name: "preparation_ready" | "execution_ready") => {
    const value = payload[name];
    if (value !== undefined && typeof value !== "boolean") throw new Error("The repair status response has malformed readiness metadata.");
    return value as boolean | undefined;
  };
  const preparationReady = readiness("preparation_ready");
  const executionReady = readiness("execution_ready");
  if (explicitExecutorKind) {
    if (typeof payload.local_host_execution_required !== "boolean"
      || typeof preparationReady !== "boolean"
      || typeof executionReady !== "boolean"
      || !Array.isArray(permissions)
      || !isRecord(payload.preflight)
      || typeof payload.preflight.status !== "string") {
      throw new Error("The repair status response is missing complete executor readiness metadata.");
    }
    if (typeof effectiveLocalHost !== "boolean"
      || (payload.local_host_execution_required !== undefined && payload.local_host_execution_required !== effectiveLocalHost)
      || postureValue.local_host_execution_required !== effectiveLocalHost) {
      throw new Error("The repair status response has contradictory host permission metadata.");
    }
    if (executorKind === "local" && !permissions.includes("local_host_execution")) {
      throw new Error("The local repair status is missing its required host permission.");
    }
    if (executorKind === "local" && executionReady === true) {
      throw new Error("The local repair status cannot claim execution readiness without per-job approval.");
    }
  }
  return {
    executor_kind: executorKind,
    executor_profile: typeof payload.executor_profile === "string"
      ? payload.executor_profile
      : expectedExecutorProfile,
    executor_posture: postureValue,
    executor_posture_digest: typeof optionalDigest === "string" ? optionalDigest : null,
    required_permissions: Array.isArray(permissions) ? permissions as string[] : [],
    local_host_execution_required: explicitExecutorKind
      ? payload.local_host_execution_required as boolean
      : typeof localHost === "boolean" ? localHost : executorKind === "local",
    preparation_ready: preparationReady,
    execution_ready: executionReady,
  };
}

function utf8ByteLength(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}

function isBoundedString(value: unknown, maxBytes: number, allowEmpty = false): value is string {
  return typeof value === "string"
    && (allowEmpty || value.length > 0)
    && !value.includes("\u0000")
    && utf8ByteLength(value) <= maxBytes;
}

function isSha256Digest(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
}

function isSafeNonNegativeInteger(value: unknown, maximum: number): value is number {
  return typeof value === "number"
    && Number.isSafeInteger(value)
    && value >= 0
    && value <= maximum;
}

function isNullableBoundedString(value: unknown, maxBytes: number): value is string | null {
  return value === null || isBoundedString(value, maxBytes);
}

function normalizeProcessCleanup(
  value: unknown,
  projection: WorkBoardRepoRepairProjection,
): WorkBoardRepoRepairProcessCleanup | null {
  if (value === undefined || value === null) return null;
  const fail = () => { throw new Error("The process cleanup receipt is malformed or belongs to another execution."); };
  if (!isRecord(value)
    || projection.status !== "unknown_external_effect"
    || projection.executor_kind !== "local"
    || projection.executor_posture?.profile !== "repo-node24-npm-v1") return fail();
  const baseKeys = ["status", "physical_capacity_released", "cleanup_receipt_verified", "readback_scope"];
  if (value.status === "held" || value.status === "unverified") {
    if (Object.keys(value).some((key) => !baseKeys.includes(key))
      || value.physical_capacity_released !== false || value.cleanup_receipt_verified !== false
      || value.readback_scope !== null) return fail();
    return { status: value.status, physical_capacity_released: false, cleanup_receipt_verified: false, readback_scope: null };
  }
  const releaseKeys = [...baseKeys, "job_id", "attempt_id", "fencing_token", "authority_digest", "process_cleanup_readback_sha256"];
  if (value.status !== "released" || Object.keys(value).some((key) => !releaseKeys.includes(key))
    || value.physical_capacity_released !== true || value.cleanup_receipt_verified !== true
    || value.readback_scope !== "process_cleanup_only" || value.job_id !== projection.job_id
    || !isBoundedString(value.job_id, 128) || !isBoundedString(value.attempt_id, 128)
    || value.attempt_id !== projection.attempt_id || !isSafeNonNegativeInteger(value.fencing_token, Number.MAX_SAFE_INTEGER)
    || value.fencing_token === 0 || !isSha256Digest(value.authority_digest)
    || value.authority_digest !== projection.authority_digest || !isSha256Digest(value.process_cleanup_readback_sha256)) return fail();
  return {
    status: "released", physical_capacity_released: true, cleanup_receipt_verified: true, readback_scope: "process_cleanup_only",
    job_id: value.job_id, attempt_id: value.attempt_id, fencing_token: value.fencing_token,
    authority_digest: value.authority_digest, process_cleanup_readback_sha256: value.process_cleanup_readback_sha256,
  };
}

class RepairRequestTimeout extends Error {
  constructor() {
    super("The repair request exceeded its deadline.");
    this.name = "RepairRequestTimeout";
  }
}

class RepairRequestCancelled extends Error {
  constructor() {
    super("The repair request was cancelled.");
    this.name = "RepairRequestCancelled";
  }
}

class StaleRepairRequest extends Error {
  constructor() {
    super("The repair request belongs to an earlier operator view.");
    this.name = "StaleRepairRequest";
  }
}

class RepairHttpError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "RepairHttpError";
    this.status = status;
  }
}

interface PersistedMutationContext {
  key: string;
  fingerprint: string;
}

function mutationStorageKey(jobId: string, kind: "consent" | "resume"): string {
  return `seraph:repo-repair:${jobId}:${kind}`;
}

async function boundedJsonRequest(
  operation: () => Promise<Response>,
  controller: AbortController,
): Promise<{ response: Response; payload: unknown }> {
  let deadlineTimer: number | undefined;
  let deadlineExpired = false;
  let onAbort: (() => void) | undefined;
  const operationPromise = (async () => {
    const response = await operation();
    const payload = await response.json().catch((cause) => {
      if (controller.signal.aborted) throw cause;
      return null;
    });
    return { response, payload };
  })();
  const deadlinePromise = new Promise<never>((_, reject) => {
    deadlineTimer = window.setTimeout(() => {
      deadlineExpired = true;
      controller.abort();
      reject(new RepairRequestTimeout());
    }, REPAIR_REQUEST_TIMEOUT_MS);
  });
  const cancellationPromise = new Promise<never>((_, reject) => {
    onAbort = () => {
      if (!deadlineExpired) reject(new RepairRequestCancelled());
    };
    controller.signal.addEventListener("abort", onAbort, { once: true });
  });
  try {
    return await Promise.race([operationPromise, deadlinePromise, cancellationPromise]);
  } finally {
    if (deadlineTimer !== undefined) window.clearTimeout(deadlineTimer);
    if (onAbort) controller.signal.removeEventListener("abort", onAbort);
    // A timed-out fetch/body may settle later. Keep that late rejection
    // handled without allowing it to update the inspector.
    void operationPromise.catch(() => undefined);
  }
}

export function RepoRepairInspector({
  jobId,
  onOpenApprovals,
  ownerPrincipalId,
  ownerSessionId,
  taskOwnerPrincipalId,
  taskOwnerSessionId,
}: RepoRepairInspectorProps) {
  const [projection, setProjection] = useState<WorkBoardRepoRepairProjection | null>(null);
  const [sourcePreview, setSourcePreview] = useState<WorkBoardRepoRepairSourcePreview | null>(null);
  const [loading, setLoading] = useState(true);
  const [sourceLoading, setSourceLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sourceError, setSourceError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const generationRef = useRef(0);
  const controllersRef = useRef<Set<AbortController>>(new Set());
  const ownerBindingRef = useRef<string | null>(null);
  const mutationFallbackRef = useRef<Map<string, PersistedMutationContext>>(new Map());

  const endpoint = useMemo(
    () => `${API_URL}/api/workflows/repo-repair/${encodeURIComponent(jobId)}`,
    [jobId],
  );
  const bindingKey = useMemo(
    () => [ownerPrincipalId, ownerSessionId, taskOwnerPrincipalId, taskOwnerSessionId].map((value) => value ?? "").join("\u0000"),
    [ownerPrincipalId, ownerSessionId, taskOwnerPrincipalId, taskOwnerSessionId],
  );
  const activeScopeRef = useRef({ jobId, bindingKey });
  const generationScopeRef = useRef({ generation: 0, jobId, bindingKey });
  // This render-time scope is intentionally updated before effects run.  A
  // response settling in the prop-rotation window must fail closed even for
  // the one render before the invalidating effect executes.
  activeScopeRef.current = { jobId, bindingKey };
  const hasCurrentBinding = Boolean(
    ownerPrincipalId
    && ownerSessionId
    && taskOwnerPrincipalId
    && taskOwnerSessionId
    && ownerPrincipalId === taskOwnerPrincipalId
    && ownerSessionId === taskOwnerSessionId,
  );

  function isCurrent(generation: number): boolean {
    const generationScope = generationScopeRef.current;
    const activeScope = activeScopeRef.current;
    return generationRef.current === generation
      && generationScope.generation === generation
      && generationScope.jobId === activeScope.jobId
      && generationScope.bindingKey === activeScope.bindingKey;
  }

  function invalidateRequests(): number {
    generationRef.current += 1;
    generationScopeRef.current = { generation: generationRef.current, jobId, bindingKey };
    for (const controller of controllersRef.current) controller.abort();
    controllersRef.current.clear();
    return generationRef.current;
  }

  function clearOwnerState(message: string): void {
    invalidateRequests();
    ownerBindingRef.current = null;
    setProjection(null);
    setSourcePreview(null);
    setSourceError(null);
    setNotice(null);
    setBusy(false);
    setSourceLoading(false);
    setLoading(false);
    setError(message);
  }

  async function requestJson(path: string, init: RequestInit = {}, generation: number): Promise<unknown> {
    if (!isCurrent(generation) || !hasCurrentBinding) throw new StaleRepairRequest();
    const controller = new AbortController();
    controllersRef.current.add(controller);
    try {
      const { response, payload } = await boundedJsonRequest(
        () => apiFetch(path, { ...init, signal: controller.signal }),
        controller,
      );
      if (!isCurrent(generation)) throw new StaleRepairRequest();
      if (!response.ok) throw new RepairHttpError(response.status, errorMessage(payload, "The repair request failed."));
      return payload;
    } catch (cause) {
      if (cause instanceof StaleRepairRequest) throw cause;
      if (cause instanceof RepairHttpError && (cause.status === 401 || cause.status === 403)) {
        if (isCurrent(generation)) clearOwnerState("The operator session is no longer authorized for this repair. Select it again after signing in.");
        throw new StaleRepairRequest();
      }
      if (cause instanceof RepairRequestTimeout || cause instanceof RepairRequestCancelled || (cause instanceof DOMException && cause.name === "AbortError")) {
        throw new Error("The repair request timed out or was cancelled.");
      }
      throw cause;
    } finally {
      controllersRef.current.delete(controller);
    }
  }

  function validateProjection(payload: unknown): WorkBoardRepoRepairProjection {
    if (!payload || typeof payload !== "object") throw new Error("The repair status response is malformed.");
    const next = payload as Partial<WorkBoardRepoRepairProjection>;
    if (next.job_id !== jobId || next.capability_id !== "engineering.repo-repair.v1" || !next.workflow_run_id) {
      throw new Error("The repair status response is bound to a different workflow.");
    }
    if (!next.owner_principal_id || !next.operator_session_id || next.operator_visible !== true) {
      clearOwnerState("The repair status is not operator-visible or has no valid ownership binding. Private repair data was cleared.");
      throw new StaleRepairRequest();
    }
    if (!hasCurrentBinding
      || next.owner_principal_id !== ownerPrincipalId
      || next.operator_session_id !== ownerSessionId
      || next.owner_principal_id !== taskOwnerPrincipalId
      || next.operator_session_id !== taskOwnerSessionId) {
      clearOwnerState("The repair status belongs to a different operator session. Private repair data was cleared.");
      throw new StaleRepairRequest();
    }
    const binding = `${next.owner_principal_id}:${next.operator_session_id}`;
    if (ownerBindingRef.current && ownerBindingRef.current !== binding) {
      // A mounted cockpit can outlive an operator-session rotation.  Clear
      // the old projection and private preview before surfacing the binding
      // error so a late response cannot leave the previous owner's source on
      // screen.
      clearOwnerState("The repair status changed operator ownership. Private repair data was cleared.");
      throw new Error("The repair status response changed operator ownership.");
    }
    ownerBindingRef.current = binding;
    const executorMetadata = normalizeExecutorMetadata(next as Record<string, unknown>);
    const normalized = { ...(next as WorkBoardRepoRepairProjection), ...executorMetadata };
    if (!isRecord(normalized.execution)) throw new Error("The repair execution response is malformed.");
    return { ...normalized, execution: { ...normalized.execution, process_cleanup: normalizeProcessCleanup(normalized.execution.process_cleanup, normalized) } };
  }

  async function readProjection(generation: number): Promise<WorkBoardRepoRepairProjection> {
    return validateProjection(await requestJson(endpoint, {}, generation));
  }

  async function refresh(generation = generationRef.current): Promise<WorkBoardRepoRepairProjection | null> {
    if (!isCurrent(generation) || !hasCurrentBinding) return null;
    setLoading(true);
    setError(null);
    try {
      const next = await readProjection(generation);
      if (isCurrent(generation)) setProjection(next);
      return next;
    } catch (cause) {
      if (isCurrent(generation) && !(cause instanceof StaleRepairRequest)) {
        setError(cause instanceof Error ? cause.message : "The repair status could not be read.");
      }
    } finally {
      if (isCurrent(generation)) setLoading(false);
    }
    return null;
  }

  function persistedMutationKey(kind: "consent" | "resume", fingerprint: string): string {
    const storageKey = mutationStorageKey(jobId, kind);
    let stored: PersistedMutationContext | undefined;
    try {
      const raw = window.sessionStorage.getItem(storageKey);
      if (raw) {
        const parsed = JSON.parse(raw) as Partial<PersistedMutationContext>;
        if (typeof parsed.key === "string" && typeof parsed.fingerprint === "string") stored = parsed as PersistedMutationContext;
      }
    } catch {
      stored = undefined;
    }
    stored ??= mutationFallbackRef.current.get(storageKey);
    if (stored && stored.fingerprint !== fingerprint) {
      throw new Error("An earlier repair request has a different binding; refresh the exact current status before retrying.");
    }
    if (!stored) stored = { key: makeRequestKey(), fingerprint };
    mutationFallbackRef.current.set(storageKey, stored);
    try {
      window.sessionStorage.setItem(storageKey, JSON.stringify(stored));
    } catch {
      // The in-memory copy still preserves exact retries during this mount.
    }
    return stored.key;
  }

  function validateSourcePreview(payload: unknown, current: WorkBoardRepoRepairProjection): WorkBoardRepoRepairSourcePreview {
    const reject = (): never => {
      throw new Error("The source preview did not match the current owner-bound packet.");
    };
    if (
      !hasCurrentBinding
      || current.job_id !== jobId
      || current.owner_principal_id !== ownerPrincipalId
      || current.operator_session_id !== ownerSessionId
      || current.owner_principal_id !== taskOwnerPrincipalId
      || current.operator_session_id !== taskOwnerSessionId
      || current.operator_visible !== true
      || current.capability_id !== "engineering.repo-repair.v1"
      || !isRecord(current.source_packet)
    ) reject();

    const expected = current.source_packet as unknown as Record<string, unknown>;
    if (
      !isBoundedString(expected.packet_id, SOURCE_PREVIEW_MAX_PACKET_BYTES)
      || !isBoundedString(expected.state, SOURCE_PREVIEW_MAX_PACKET_BYTES)
      || !isBoundedString(expected.repository_ref, SOURCE_PREVIEW_MAX_PACKET_BYTES)
      || !isSha256Digest(expected.base_snapshot_sha256)
      || !isSha256Digest(expected.source_manifest_sha256)
      || !isSha256Digest(expected.artifact_sha256)
      || !isSafeNonNegativeInteger(expected.revision, Number.MAX_SAFE_INTEGER)
      || expected.revision < 1
    ) reject();

    if (!isRecord(payload)) reject();
    const next = payload as Record<string, unknown>;
    if (
      next.job_id !== jobId
      || !isBoundedString(next.status, SOURCE_PREVIEW_MAX_PACKET_BYTES)
      || !isBoundedString(next.recovery_action, SOURCE_PREVIEW_MAX_PACKET_BYTES)
      || next.operator_visible !== true
      || typeof next.provider_contacted !== "boolean"
      || !isRecord(next.source_packet)
      || !isRecord(next.egress)
    ) reject();

    const packet = next.source_packet as Record<string, unknown>;
    if (
      !isBoundedString(packet.packet_id, SOURCE_PREVIEW_MAX_PACKET_BYTES)
      || !isBoundedString(packet.state, SOURCE_PREVIEW_MAX_PACKET_BYTES)
      || !isBoundedString(packet.repository_ref, SOURCE_PREVIEW_MAX_PACKET_BYTES)
      || !isSha256Digest(packet.base_snapshot_sha256)
      || !isSha256Digest(packet.source_manifest_sha256)
      || !isSha256Digest(packet.artifact_sha256)
      || !isSafeNonNegativeInteger(packet.revision, Number.MAX_SAFE_INTEGER)
      || packet.revision < 1
      || packet.packet_id !== expected.packet_id
      || packet.state !== expected.state
      || packet.repository_ref !== expected.repository_ref
      || packet.base_snapshot_sha256 !== expected.base_snapshot_sha256
      || packet.source_manifest_sha256 !== expected.source_manifest_sha256
      || packet.artifact_sha256 !== expected.artifact_sha256
      || packet.revision !== expected.revision
      || !Array.isArray(packet.selected_files)
      || packet.selected_files.length < 1
      || packet.selected_files.length > SOURCE_PREVIEW_MAX_FILES
      || !Array.isArray(packet.omissions)
      || packet.omissions.some((omission: unknown) => !isBoundedString(omission, SOURCE_PREVIEW_MAX_PACKET_BYTES))
    ) reject();

    const selectedFiles = packet.selected_files as unknown[];
    const paths = new Set<string>();
    let selectedBytes = 0;
    for (const fileValue of selectedFiles) {
      if (!isRecord(fileValue)) reject();
      const file = fileValue as Record<string, unknown>;
      if (
        !isBoundedString(file.path, SOURCE_PREVIEW_MAX_PACKET_BYTES)
        || !isBoundedString(file.text, SOURCE_PREVIEW_MAX_PACKET_BYTES, true)
        || !isSafeNonNegativeInteger(file.size_bytes, SOURCE_PREVIEW_MAX_PACKET_BYTES)
        || !isSha256Digest(file.sha256)
      ) reject();
      const path = file.path as string;
      const text = file.text as string;
      const sizeBytes = file.size_bytes as number;
      if (sizeBytes !== utf8ByteLength(text) || paths.has(path)) reject();
      paths.add(path);
      selectedBytes += sizeBytes;
      if (selectedBytes > SOURCE_PREVIEW_MAX_PACKET_BYTES) reject();
    }

    const egress = next.egress as Record<string, unknown>;
    if (
      egress.runtime_path !== "strategist_agent"
      || !isNullableBoundedString(egress.effective_profile_id, SOURCE_PREVIEW_MAX_PACKET_BYTES)
      || !isNullableBoundedString(egress.effective_upstream, SOURCE_PREVIEW_MAX_PACKET_BYTES)
      || !isSafeNonNegativeInteger(egress.maximum_input_bytes, SOURCE_PREVIEW_MAX_PACKET_BYTES)
      || egress.maximum_input_bytes < 1
      || !isSafeNonNegativeInteger(egress.maximum_output_tokens, SOURCE_PREVIEW_MAX_OUTPUT_TOKENS)
      || egress.maximum_output_tokens < 1
      || (egress.expires_at !== null
        && (!isBoundedString(egress.expires_at, SOURCE_PREVIEW_MAX_PACKET_BYTES)
          || !Number.isFinite(Date.parse(egress.expires_at))))
    ) reject();

    return next as unknown as WorkBoardRepoRepairSourcePreview;
  }

  useEffect(() => {
    const generation = invalidateRequests();
    ownerBindingRef.current = null;
    setProjection(null);
    setSourcePreview(null);
    setError(null);
    setSourceError(null);
    setNotice(null);
    setBusy(false);
    setSourceLoading(false);
    setLoading(hasCurrentBinding);
    if (!hasCurrentBinding) {
      setError("Select a repair owned by the current operator session before viewing private repair data.");
      return () => {
        invalidateRequests();
      };
    }
    void readProjection(generation)
      .then((next) => {
        if (isCurrent(generation)) setProjection(next);
      })
      .catch((cause: unknown) => {
        if (isCurrent(generation) && !(cause instanceof StaleRepairRequest)) setError(cause instanceof Error ? cause.message : "The repair status could not be read.");
      })
      .finally(() => {
        if (isCurrent(generation)) setLoading(false);
      });
    return () => {
      invalidateRequests();
    };
    // The job id is the owner-bound identity for this inspector.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bindingKey, endpoint, hasCurrentBinding]);

  async function inspectSource() {
    const generation = generationRef.current;
    const current = projection;
    if (!current) return;
    setSourceLoading(true);
    setSourceError(null);
    try {
      const next = validateSourcePreview(await requestJson(`${endpoint}/source-preview`, {}, generation), current);
      if (isCurrent(generation)) setSourcePreview(next);
    } catch (cause) {
      if (isCurrent(generation) && !(cause instanceof StaleRepairRequest)) setSourceError(cause instanceof Error ? cause.message : "The private source preview is unavailable.");
    } finally {
      if (isCurrent(generation)) setSourceLoading(false);
    }
  }

  async function grantSourceConsent() {
    const generation = generationRef.current;
    const current = projection;
    const packet = sourcePreview?.source_packet;
    const profile = sourcePreview?.egress.effective_profile_id;
    if (!current || !packet || !profile) {
      setSourceError("Inspect the current selected source packet before granting its exact consent.");
      return;
    }
    setBusy(true);
    setSourceError(null);
    setNotice(null);
    try {
      const fingerprint = [current.revision ?? "", packet.artifact_sha256, packet.source_manifest_sha256, profile].join(":");
      const payload = await requestJson(`${endpoint}/code-egress-consent`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          expected_job_revision: current.revision,
          source_packet_digest: packet.artifact_sha256,
          expected_source_manifest_digest: packet.source_manifest_sha256,
          expected_profile_id: profile,
          acknowledged_selected_source: true,
          idempotency_key: persistedMutationKey("consent", fingerprint),
        }),
      }, generation);
      if (!payload || typeof payload !== "object") throw new Error("The consent receipt is malformed; the exact request key is retained.");
      const receipt = payload as {
        job_id?: unknown;
        consent_id?: unknown;
        consent_revision?: unknown;
        expires_at?: unknown;
        operator_visible?: unknown;
      };
      if (
        receipt.job_id !== jobId
        || receipt.operator_visible !== true
        || typeof receipt.consent_id !== "string"
        || !receipt.consent_id.trim()
        || typeof receipt.consent_revision !== "number"
        || !Number.isSafeInteger(receipt.consent_revision)
        || !isFiniteFutureTimestamp(receipt.expires_at)
      ) throw new Error("The consent receipt is malformed or expired; the exact request key is retained.");
      const refreshed = await refresh(generation);
      const refreshedConsent = refreshed?.egress;
      if (
        !refreshed
        || !refreshedConsent
        || refreshedConsent.consent_id !== receipt.consent_id
        || refreshedConsent.revision !== receipt.consent_revision
        || refreshedConsent.expires_at !== receipt.expires_at
        || refreshedConsent.state !== "active"
        || !isFiniteFutureTimestamp(refreshedConsent.expires_at)
      ) throw new Error("The consent readback did not match the current owner-bound consent; the exact request key is retained.");
      if (isCurrent(generation)) {
        setNotice("Selected source consent recorded. The same durable root will continue after the next board pass.");
        setSourcePreview(null);
      }
    } catch (cause) {
      if (isCurrent(generation) && !(cause instanceof StaleRepairRequest)) setSourceError(cause instanceof Error ? cause.message : "The source consent could not be recorded.");
    } finally {
      if (isCurrent(generation)) setBusy(false);
    }
  }

  async function resumeApprovedProposal() {
    const generation = generationRef.current;
    const current = projection;
    if (!current?.proposal || !current.approval) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const approval = current.approval;
      const proposal = current.proposal;
      const fingerprint = [current.revision ?? "", proposal.proposal_id, proposal.revision, approval.approval_id].join(":");
      const payload = await requestJson(`${endpoint}/resume`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          approval_id: approval.approval_id,
          proposal_id: proposal.proposal_id,
          expected_proposal_revision: proposal.revision,
          expected_job_revision: current.revision,
          idempotency_key: persistedMutationKey("resume", fingerprint),
        }),
      }, generation);
      const next = validateProjection(payload);
      if (next.approval_id && next.approval_id !== approval.approval_id) throw new Error("The resume receipt changed approval identity.");
      if (next.proposal?.proposal_id !== proposal.proposal_id || next.authority_digest !== current.authority_digest) {
        throw new Error("The resume receipt changed the reviewed repair binding.");
      }
      if (isCurrent(generation)) {
        setNotice("The approved repair was resumed on its existing durable root. Refresh for execution/readback.");
        setProjection(next);
      }
    } catch (cause) {
      if (isCurrent(generation) && !(cause instanceof StaleRepairRequest)) setError(cause instanceof Error ? cause.message : "The approved repair could not be resumed.");
    } finally {
      if (isCurrent(generation)) setBusy(false);
    }
  }

  async function recoverProcessCleanup() {
    const generation = generationRef.current;
    const current = projection;
    if (!hasCurrentBinding || !current || current.status !== "unknown_external_effect"
      || current.executor_kind !== "local" || current.executor_posture?.profile !== "repo-node24-npm-v1"
      || current.execution.process_cleanup?.physical_capacity_released === true) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    let actionError: string | null = null;
    try {
      try {
        const receipt = await requestJson(`${API_URL}/api/workflows/repo-change/${encodeURIComponent(jobId)}/recover`, { method: "POST" }, generation);
        if (!isRecord(receipt) || receipt.status !== "unknown_external_effect") {
          actionError = "Process cleanup remains unverified. Reconcile the original execution and refresh before another explicit recovery attempt.";
        }
      } catch (cause) {
        if (cause instanceof StaleRepairRequest || !isCurrent(generation)) return;
        actionError = `Recovery response uncertain or rejected: ${cause instanceof Error ? cause.message : "request unavailable"}. Refresh durable status before another explicit recovery attempt.`;
      }
      // A lost POST response cannot establish success. Only the safe durable
      // GET can display released physical capacity; no resume or replay occurs.
      const refreshed = await refresh(generation);
      if (!isCurrent(generation)) return;
      if (refreshed?.execution.process_cleanup?.physical_capacity_released === true) {
        setNotice("Durable process cleanup verified. Physical capacity released; task, effect and cost liabilities remain Unknown.");
      } else if (refreshed) {
        setError(actionError ?? "Process cleanup has no durable verified release. Reconcile the original execution before another explicit recovery attempt.");
      }
    } finally {
      if (isCurrent(generation)) setBusy(false);
    }
  }

  const projectionForRender = projection
    && hasCurrentBinding
    && projection.job_id === jobId
    && projection.owner_principal_id === ownerPrincipalId
    && projection.operator_session_id === ownerSessionId
    && projection.owner_principal_id === taskOwnerPrincipalId
    && projection.operator_session_id === taskOwnerSessionId
    && projection.operator_visible === true
    ? projection
    : null;
  const sourcePreviewForRender = projectionForRender ? sourcePreview : null;

  if (loading && !projectionForRender) {
    return <section className="rounded border border-cyan-400/30 bg-cyan-950/10 p-3" aria-label="Repository repair execution"><div className="font-semibold">Repository repair</div><div className="mt-1 text-[11px] opacity-80">Loading owner-bound repair status…</div></section>;
  }

  if (error && !projectionForRender) {
    return <section className="rounded border border-amber-500/40 bg-amber-950/10 p-3" aria-label="Repository repair execution"><div className="font-semibold">Repository repair</div><div className="mt-1" role="alert">{error}</div><button type="button" className="cockpit-feedback-button mt-2" onClick={() => void refresh()}>Refresh repair status</button></section>;
  }

  if (!projectionForRender) return null;
  const packet = projectionForRender.source_packet;
  const proposal = projectionForRender.proposal;
  const approval = projectionForRender.approval;
  const status = projectionForRender.status;
  const executorKind = projectionForRender.executor_kind ?? projectionForRender.executor_posture?.kind ?? "docker_rootless";
  const executorProfile = projectionForRender.executor_profile ?? `${executorKind}:repo-python-pytest-v1`;
  const posture = projectionForRender.executor_posture ?? {
    kind: executorKind,
    profile: "repo-python-pytest-v1",
    isolation_claim: executorKind === "local" ? "none" : "unverified",
    network_isolation: executorKind === "local" ? "not_verified" : "unverified",
    resource_enforcement: executorKind === "local" ? "admission_and_wall_timeout_only" : "unverified",
    image_digest: null,
    limits_digest: null,
    local_host_execution_required: executorKind === "local",
  };
  const localHostExecution = executorKind === "local" || projectionForRender.local_host_execution_required === true;
  const preparationReady = projectionForRender.preparation_ready ?? projectionForRender.preflight?.status === "verified";
  const executionReady = projectionForRender.execution_ready ?? (Boolean(preparationReady) && !localHostExecution);
  const canConsent = Boolean(packet && sourcePreviewForRender && !projectionForRender.egress);
  const canResume = Boolean(
    proposal
    && approval
    && approval.status === "approved"
    && ["awaiting_approval", "queued", "running"].includes(status),
  );
  const terminal = ["succeeded", "failed", "cancelled", "blocked", "unknown_external_effect", "cost_liability"].includes(status);
  const processCleanup = projectionForRender.execution.process_cleanup;
  const nodeCleanupRecovery = status === "unknown_external_effect" && executorKind === "local" && posture.profile === "repo-node24-npm-v1";

  return (
    <section className="rounded border border-cyan-400/30 bg-cyan-950/10 p-3" aria-label="Repository repair execution">
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="font-semibold">Repository repair execution</div>
          <div className="mt-1 text-[11px] opacity-80">{statusLabel(status)} · durable revision {projectionForRender.revision ?? "unavailable"}</div>
        </div>
        <button type="button" className="cockpit-feedback-button" onClick={() => void refresh()} disabled={busy || loading}>Refresh</button>
      </div>
      <div className="mt-2 grid gap-1 text-[11px]">
        <div>Root <span className="font-mono break-all">{projectionForRender.job_id}</span> · authority <span className="font-mono">{safeDigest(projectionForRender.authority_digest)}</span></div>
        <div>Executor: <span className="font-mono">{executorProfile}</span> · posture <span className="font-mono">{safeDigest(projectionForRender.executor_posture_digest)}</span></div>
        {posture.profile === "repo-node24-npm-v1" ? <div>Recorded job preflight: {typeof projectionForRender.preflight?.status === "string" ? projectionForRender.preflight.status : "blocked"}</div> : <div>Preflight: {projectionForRender.preflight?.status === "verified" ? "verified" : `blocked or unknown${projectionForRender.preflight && typeof projectionForRender.preflight.reason === "string" ? ` · ${projectionForRender.preflight.reason}` : ""}`}</div>}
        <div>Preparation: {preparationReady ? "ready" : "blocked"} · execution: {executionReady ? "ready" : localHostExecution && preparationReady ? "awaiting exact host approval" : "blocked"}</div>
        <div>Posture: isolation {posture.isolation_claim ?? "unknown"} · network {posture.network_isolation ?? "unknown"} · resources {posture.resource_enforcement ?? "unknown"}</div>
        {localHostExecution && <div className="text-amber-200">Trusted host execution: no isolation guarantee. This job may access the host filesystem and network as the Seraph user after the exact approval.</div>}
        <div>Memory: {projectionForRender.memory_status} · provider contact: {projectionForRender.execution.provider_contacted ? "recorded" : "not recorded"}</div>
      </div>

      {packet && (
        <div className="mt-3 rounded border border-white/10 p-2">
          <div className="font-semibold">Selected source packet</div>
          <div className="mt-1 break-all">{packet.repository_ref} · packet {safeDigest(packet.artifact_sha256)} · source manifest {safeDigest(packet.source_manifest_sha256)}</div>
          {!projectionForRender.egress && <div className="mt-1 text-amber-200">Private source stays local until you explicitly inspect and acknowledge this packet.</div>}
          <div className="mt-2 flex flex-wrap gap-2">
            <button type="button" className="cockpit-feedback-button" onClick={() => void inspectSource()} disabled={sourceLoading || busy}>{sourceLoading ? "Loading selected source…" : "Inspect selected source"}</button>
            {canConsent && <button type="button" className="cockpit-feedback-button" onClick={() => void grantSourceConsent()} disabled={busy}>Allow exact source packet</button>}
          </div>
          {sourceError && <div className="mt-2 rounded border border-amber-500/40 p-2" role="alert">{sourceError}</div>}
          {sourcePreviewForRender && (
            <div className="mt-2 grid gap-2" aria-label="Private source preview">
              <div className="text-[10px] opacity-75">Explicit owner preview only · {sourcePreviewForRender.source_packet.selected_files.length} selected file(s) · provider contact {sourcePreviewForRender.provider_contacted ? "recorded" : "not recorded"}.</div>
              {sourcePreviewForRender.source_packet.selected_files.map((file) => (
                <article key={`${file.path}:${file.sha256}`} className="rounded bg-black/20 p-2">
                  <div className="font-mono text-[10px]">{file.path} · {file.size_bytes} bytes · {safeDigest(file.sha256)}</div>
                  <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-words text-[10px]">{file.text}</pre>
                </article>
              ))}
            </div>
          )}
        </div>
      )}

      {projectionForRender.egress && (
        <div className="mt-3 rounded border border-emerald-500/30 p-2">
          <div className="font-semibold">Governed model consent</div>
          <div className="mt-1">{projectionForRender.egress.effective_profile_id} via {projectionForRender.egress.effective_upstream} · expires {new Date(projectionForRender.egress.expires_at).toLocaleString()}</div>
          <div className="text-[10px] opacity-75">Input bound to {projectionForRender.egress.maximum_input_bytes} bytes and {projectionForRender.egress.maximum_output_tokens} output tokens.</div>
        </div>
      )}

      {proposal && (
        <div className="mt-3 rounded border border-white/10 p-2">
          <div className="flex items-center justify-between gap-2"><div className="font-semibold">Reviewed repair proposal</div><span>{statusLabel(proposal.status)} · revision {proposal.revision}</span></div>
          <div className="mt-1">Patch digest <span className="font-mono">{safeDigest(proposal.patch_sha256)}</span> · model profile {proposal.model_profile_id}</div>
          {approval && <div className="mt-1">Exact approval <span className="font-mono break-all">{approval.approval_id}</span> · {statusLabel(approval.status)}{approval.expires_at ? ` · expires ${new Date(approval.expires_at).toLocaleString()}` : ""}</div>}
          {localHostExecution && <div className="mt-1 text-amber-200">Host permission required: local filesystem and network access, host-user resource consumption, and bounded process execution are visible in the exact approval.</div>}
          {posture.profile === "repo-node24-npm-v1" && <div className="mt-2" aria-label="Reviewed Node execution inputs">
            <div>Node {String(posture.node_version ?? "unavailable")} · Linux process supervision · CPU, memory and PID ceilings unenforced.</div>
            {isRecord(posture.execution_plan) && Array.isArray(posture.execution_plan.commands) && posture.execution_plan.commands.map((command) => isRecord(command) && <div key={String(command.script)} className="mt-1">
              <div>{String(command.script)}: <code>{String(command.body)}</code></div>
              <div className="font-mono break-all">Direct argv: {Array.isArray(command.argv) ? command.argv.join(" ") : "unavailable"}</div>
            </div>)}
            {isRecord(posture.execution_plan) && <div>Package {safeDigest(String(posture.execution_plan.package_sha256 ?? ""))} · lockfile {safeDigest(String(posture.execution_plan.lockfile_sha256 ?? ""))} · dependencies {safeDigest(String(posture.execution_plan.dependency_manifest_sha256 ?? ""))}</div>}
            <div>npm and pre/post hooks are not executed. Existing dependencies are privately copied; no downloads.</div>
          </div>}
          <div className="mt-2 flex flex-wrap gap-2">
            {approval?.status === "pending" && onOpenApprovals && <button type="button" className="cockpit-feedback-button" onClick={onOpenApprovals}>{localHostExecution ? "Approve local tests on this host" : "Review exact approval"}</button>}
            {approval?.status === "approved" && canResume && <button type="button" className="cockpit-feedback-button" onClick={() => void resumeApprovedProposal()} disabled={busy}>Resume approved repair</button>}
            {approval?.status === "denied" && <span className="text-amber-200">Approval denied. Prepare a fresh review after checking the current source and goal.</span>}
          </div>
        </div>
      )}

      <div className="mt-3 rounded border border-white/10 p-2">
        {nodeCleanupRecovery && <div className="mb-3 rounded border border-amber-500/40 p-2" aria-label="Process cleanup recovery">
          <div className="font-semibold">Process cleanup only</div>
          <div className="mt-1">{processCleanup?.physical_capacity_released === true ? "Physical capacity released · durable cleanup receipt verified." : processCleanup?.status === "held" ? "Physical capacity held · process cleanup unverified." : "Physical capacity unverified · no verified process cleanup release."}</div>
          <div className="mt-1">Task, effect and cost liabilities remain Unknown. Reconcile the exact sandbox effect before any retry.</div>
          {processCleanup?.status === "released" && <div className="mt-1 text-[10px]">Original attempt {processCleanup.attempt_id} · fence {processCleanup.fencing_token} · cleanup digest {safeDigest(processCleanup.process_cleanup_readback_sha256)}</div>}
          {processCleanup?.physical_capacity_released !== true && <button type="button" className="cockpit-feedback-button mt-2" onClick={() => void recoverProcessCleanup()} disabled={busy || loading}>Recover process cleanup</button>}
        </div>}
        <div className="font-semibold">Sandbox readback</div>
        {projectionForRender.execution.readback ? (
          <div className="mt-1">{statusLabel(projectionForRender.execution.readback.status)} · {projectionForRender.execution.readback.verified ? "independently verified" : "verification unavailable"} · {projectionForRender.execution.readback.target_path ?? "target unavailable"}</div>
        ) : <div className="mt-1 text-amber-200">No verified readback receipt is available.</div>}
        {projectionForRender.execution.artifacts.length > 0 && <div className="mt-1 text-[10px]">{projectionForRender.execution.artifacts.length} bounded execution artifact(s) are recorded by digest.</div>}
        {terminal && status !== "succeeded" && <div className="mt-2 rounded border border-amber-500/40 p-2" role="status">Recovery: {status === "unknown_external_effect" || status === "cost_liability" ? "reconcile the exact sandbox effect before any retry" : projectionForRender.recovery_action.replace(/_/g, " ")}.</div>}
      </div>
      {notice && <div className="mt-2 rounded border border-emerald-500/40 p-2" role="status">{notice}</div>}
      {error && <div className="mt-2 rounded border border-amber-500/40 p-2" role="alert">{error}</div>}
    </section>
  );
}
