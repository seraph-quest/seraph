import { useEffect, useRef, useState } from "react";

import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";

type RepoSandboxExecutorKind = "local" | "docker_rootless" | "docker_rootful";

interface RepoSandboxPosture {
  kind: RepoSandboxExecutorKind;
  profile: string;
  isolation_claim: string;
  network_isolation: string;
  resource_enforcement: string;
  image_digest: string | null;
  limits_digest: string | null;
  host_access?: string;
  local_host_execution_required: boolean;
}

interface RepoSandboxPreflight {
  ok?: boolean;
  status?: string;
  reason?: string;
  [key: string]: unknown;
}

interface RepoSandboxPayload {
  node_runtime_path?: string;
  metadata_available: boolean;
  executor_kind: RepoSandboxExecutorKind;
  executor_profile: string;
  executor_posture: RepoSandboxPosture;
  executor_posture_digest: string | null;
  local_host_approval_required: boolean;
  enabled: boolean;
  docker_socket: string;
  worker_image_digest: string;
  profile: string;
  limits: Record<string, number>;
  limits_digest: string | null;
  limits_editable: boolean;
  preflight: RepoSandboxPreflight;
  preparation_ready: boolean;
  execution_ready: boolean;
  legacy_repo_change_preflight: RepoSandboxPreflight | null;
  status: string;
  configuration_error?: string | null;
  operator_visible: boolean;
}

const FALLBACK: RepoSandboxPayload = {
  metadata_available: false,
  executor_kind: "docker_rootless",
  executor_profile: "unavailable",
  executor_posture: {
    kind: "docker_rootless",
    profile: "repo-python-pytest-v1",
    isolation_claim: "unknown",
    network_isolation: "unknown",
    resource_enforcement: "unknown",
    image_digest: null,
    limits_digest: null,
    local_host_execution_required: false,
  },
  executor_posture_digest: null,
  local_host_approval_required: false,
  enabled: false,
  docker_socket: "",
  worker_image_digest: "",
  profile: "repo-python-pytest-v1",
  limits: {},
  limits_digest: null,
  limits_editable: false,
  preflight: { ok: false, status: "unknown", reason: "metadata_unavailable" },
  preparation_ready: false,
  execution_ready: false,
  legacy_repo_change_preflight: null,
  status: "unknown",
  configuration_error: "Settings metadata is unavailable; execution remains blocked.",
  operator_visible: true,
};

const REQUIRED_LIMITS = ["max_cpu_seconds", "max_memory_bytes", "max_pids", "max_wall_seconds"] as const;
const REPO_SANDBOX_PROFILE = "repo-python-pytest-v1";
const PREFLIGHT_STATUSES = new Set(["ready", "blocked", "unknown", "degraded", "not_selected"]);
const LOCAL_HOST_ACCESS = "explicit_job_approval_required";

const POSTURE_VALUES: Record<RepoSandboxExecutorKind, {
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

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function isExecutorKind(value: unknown): value is RepoSandboxExecutorKind {
  return value === "local" || value === "docker_rootless" || value === "docker_rootful";
}

function boundedMetadataString(value: unknown, fallback: string): string {
  return typeof value === "string" && value.length <= 512 && !value.includes("\u0000") ? value : fallback;
}

function isSafeDigest(value: unknown): value is string | null | undefined {
  return value === undefined || value === null || (typeof value === "string" && /^[0-9a-f]{64}$/.test(value));
}

function normalizePosture(
  value: unknown,
  kind: RepoSandboxExecutorKind,
  limitsDigest: string | null,
  requireComplete = false,
): RepoSandboxPosture | null {
  if (!isRecord(value)) return null;
  if (requireComplete && (value.kind !== kind || ![REPO_SANDBOX_PROFILE, "repo-node24-npm-v1"].includes(String(value.profile)))) return null;
  if (value.kind !== undefined && value.kind !== kind) return null;
  if (value.profile !== undefined && ![REPO_SANDBOX_PROFILE, "repo-node24-npm-v1"].includes(String(value.profile))) return null;
  const imageDigest = value.image_digest;
  if (imageDigest !== null && imageDigest !== undefined && (typeof imageDigest !== "string" || imageDigest.length > 512 || imageDigest.includes("\u0000"))) return null;
  if (kind === "local" && imageDigest !== null && imageDigest !== undefined) return null;
  const rawIsolation = value.isolation_claim;
  const rawNetwork = value.network_isolation;
  const rawResources = value.resource_enforcement;
  if (requireComplete && [rawIsolation, rawNetwork, rawResources].some((item) => item === undefined)) return null;
  for (const item of [rawIsolation, rawNetwork, rawResources]) {
    if (item !== undefined && (typeof item !== "string" || item.length > 128 || item.includes("\u0000"))) return null;
  }
  if (rawIsolation !== undefined && !POSTURE_VALUES[kind].isolation.includes(rawIsolation as string)) return null;
  if (rawNetwork !== undefined && !POSTURE_VALUES[kind].network.includes(rawNetwork as string)) return null;
  if (rawResources !== undefined && !POSTURE_VALUES[kind].resources.includes(rawResources as string)) return null;
  const hostAccess = value.host_access;
  if (hostAccess !== undefined && (typeof hostAccess !== "string" || hostAccess.length > 128 || hostAccess.includes("\u0000"))) return null;
  if (hostAccess !== undefined && (kind !== "local" || hostAccess !== LOCAL_HOST_ACCESS)) return null;
  if (requireComplete && !Object.prototype.hasOwnProperty.call(value, "limits_digest")) return null;
  if (!isSafeDigest(value.limits_digest) || !isSafeDigest(limitsDigest)) return null;
  const localHost = value.local_host_execution_required;
  const inferredLocalHost = localHost === undefined && hostAccess === LOCAL_HOST_ACCESS ? true : localHost;
  if (localHost !== undefined && typeof localHost !== "boolean") return null;
  if (requireComplete && typeof inferredLocalHost !== "boolean") return null;
  const effectiveLocalHost: boolean = typeof inferredLocalHost === "boolean"
    ? inferredLocalHost
    : kind === "local";
  if (kind === "local" && effectiveLocalHost === false) return null;
  if (kind !== "local" && effectiveLocalHost === true) return null;
  if (hostAccess === LOCAL_HOST_ACCESS && effectiveLocalHost !== true) return null;
  return {
    kind,
    profile: boundedMetadataString(value.profile, REPO_SANDBOX_PROFILE),
    isolation_claim: boundedMetadataString(value.isolation_claim, kind === "local" ? "none" : "unverified"),
    network_isolation: boundedMetadataString(value.network_isolation, kind === "local" ? "not_verified" : "unverified"),
    resource_enforcement: boundedMetadataString(value.resource_enforcement, kind === "local" ? "admission_and_wall_timeout_only" : "unverified"),
    image_digest: typeof imageDigest === "string" ? imageDigest : null,
    limits_digest: typeof value.limits_digest === "string" ? value.limits_digest : limitsDigest,
    ...(hostAccess === undefined ? {} : { host_access: hostAccess }),
    local_host_execution_required: effectiveLocalHost ?? kind === "local",
  };
}

function normalizePreflight(value: unknown): RepoSandboxPreflight | null {
  if (!isRecord(value)) return null;
  if (value.ok !== undefined && typeof value.ok !== "boolean") return null;
  if (value.status !== undefined && (typeof value.status !== "string" || value.status.length > 128 || !PREFLIGHT_STATUSES.has(value.status))) return null;
  if (value.ok === true && value.status !== undefined && value.status !== "ready") return null;
  if (value.ok === false && value.status === "ready") return null;
  if (value.reason !== undefined && (typeof value.reason !== "string" || value.reason.length > 512 || value.reason.includes("\u0000"))) return null;
  return {
    ok: value.ok as boolean | undefined,
    status: value.status as string | undefined,
    reason: value.reason as string | undefined,
  };
}

function normalizeRepoSandboxPayload(payload: unknown): RepoSandboxPayload | null {
  if (!isRecord(payload)) return null;
  // A response without executor_kind is the pre-M4 persisted rootless shape.
  // Keep it visible as rootless until the operator explicitly selects another
  // backend; never reinterpret an old enabled document as local execution.
  if (payload.executor_kind !== undefined && !isExecutorKind(payload.executor_kind)) return null;
  const hasExplicitExecutor = payload.executor_kind !== undefined;
  const executorKind: RepoSandboxExecutorKind = !hasExplicitExecutor
    ? "docker_rootless"
    : payload.executor_kind as RepoSandboxExecutorKind;
  if (
    typeof payload.enabled !== "boolean"
    || typeof payload.docker_socket !== "string"
    || payload.docker_socket.length > 512
    || payload.docker_socket.includes("\u0000")
    || typeof payload.worker_image_digest !== "string"
    || payload.worker_image_digest.length > 512
    || payload.worker_image_digest.includes("\u0000")
    || ![REPO_SANDBOX_PROFILE, "repo-node24-npm-v1"].includes(String(payload.profile))
    || payload.limits_editable !== false
    || typeof payload.status !== "string"
    || payload.status.length > 128
    || payload.status.includes("\u0000")
    || payload.operator_visible !== true
    || !(payload.limits_digest === null || typeof payload.limits_digest === "string")
    || !(payload.configuration_error === null || typeof payload.configuration_error === "undefined" || (typeof payload.configuration_error === "string" && payload.configuration_error.length <= 512 && !payload.configuration_error.includes("\u0000")))
    || !isRecord(payload.limits)
    || !isRecord(payload.preflight)
  ) return null;
  if (!isSafeDigest(payload.limits_digest)) return null;
  const limits = payload.limits;
  if (REQUIRED_LIMITS.some((key) => typeof limits[key] !== "number" || !Number.isSafeInteger(limits[key]) || limits[key] < 1)) return null;
  const preflight = normalizePreflight(payload.preflight);
  if (!preflight) return null;
  const posture = !hasExplicitExecutor && payload.executor_posture === undefined
    ? normalizePosture({
      kind: executorKind,
      profile: "repo-python-pytest-v1",
      isolation_claim: executorKind === "local" ? "none" : "unverified",
      network_isolation: executorKind === "local" ? "not_verified" : "unverified",
      resource_enforcement: executorKind === "local" ? "admission_and_wall_timeout_only" : "unverified",
      limits_digest: payload.limits_digest,
      local_host_execution_required: executorKind === "local",
    }, executorKind, payload.limits_digest)
    : normalizePosture(payload.executor_posture, executorKind, payload.limits_digest, hasExplicitExecutor);
  if (!posture || posture.profile !== payload.profile) return null;
  const legacyPreflight = payload.legacy_repo_change_preflight === undefined || payload.legacy_repo_change_preflight === null
    ? null
    : normalizePreflight(payload.legacy_repo_change_preflight);
  if (payload.legacy_repo_change_preflight !== undefined && payload.legacy_repo_change_preflight !== null && !legacyPreflight) return null;
  const expectedExecutorProfile = `${executorKind}:${payload.profile}`;
  if (hasExplicitExecutor) {
    if (payload.executor_profile !== expectedExecutorProfile) return null;
    if (typeof payload.executor_posture_digest !== "string" || !/^[0-9a-f]{64}$/.test(payload.executor_posture_digest)) return null;
    if (typeof payload.local_host_approval_required !== "boolean") return null;
    if (typeof payload.preparation_ready !== "boolean" || typeof payload.execution_ready !== "boolean") return null;
    if (payload.local_host_approval_required !== posture.local_host_execution_required) return null;
    if (executorKind === "local" && payload.execution_ready === true) return null;
    if (typeof preflight.ok !== "boolean" || typeof preflight.status !== "string") return null;
    if (payload.execution_ready === true && preflight.ok !== true) return null;
  } else {
    if (payload.executor_profile !== undefined && payload.executor_profile !== expectedExecutorProfile) return null;
    if (payload.executor_posture !== undefined && posture.local_host_execution_required) return null;
    if (!isSafeDigest(payload.executor_posture_digest ?? null)) return null;
    if (payload.local_host_approval_required !== undefined && typeof payload.local_host_approval_required !== "boolean") return null;
    if (payload.preparation_ready !== undefined && typeof payload.preparation_ready !== "boolean") return null;
    if (payload.execution_ready !== undefined && typeof payload.execution_ready !== "boolean") return null;
  }
  return {
    metadata_available: true,
    ...(typeof payload.node_runtime_path === "string" && payload.node_runtime_path.length <= 512 ? { node_runtime_path: payload.node_runtime_path } : {}),
    executor_kind: executorKind,
    executor_profile: typeof payload.executor_profile === "string" ? payload.executor_profile : expectedExecutorProfile,
    executor_posture: posture,
    executor_posture_digest: typeof payload.executor_posture_digest === "string" ? payload.executor_posture_digest : null,
    local_host_approval_required: payload.local_host_approval_required ?? executorKind === "local",
    enabled: payload.enabled,
    docker_socket: payload.docker_socket,
    worker_image_digest: payload.worker_image_digest,
    profile: payload.profile,
    limits: Object.fromEntries(Object.entries(limits).filter(([, value]) => typeof value === "number")) as Record<string, number>,
    limits_digest: payload.limits_digest,
    limits_editable: false,
    preflight,
    preparation_ready: payload.preparation_ready ?? Boolean(preflight.ok),
    execution_ready: payload.execution_ready ?? (Boolean(preflight.ok) && executorKind !== "local"),
    legacy_repo_change_preflight: legacyPreflight,
    status: payload.status,
    configuration_error: typeof payload.configuration_error === "string" ? payload.configuration_error : null,
    operator_visible: true,
  };
}

const MALFORMED_METADATA_MESSAGE = "Sandbox metadata was malformed; the last known controls are retained and execution remains blocked until refreshed.";
const UNAVAILABLE_METADATA_MESSAGE = "Sandbox metadata is unavailable; the last known selectors are retained and effective posture/readiness is unknown.";

function markMetadataUnavailable(current: RepoSandboxPayload, message = UNAVAILABLE_METADATA_MESSAGE): RepoSandboxPayload {
  return {
    ...current,
    metadata_available: false,
    status: "degraded",
    preflight: { ...current.preflight, ok: false, status: "unknown", reason: "metadata_unavailable" },
    preparation_ready: false,
    execution_ready: false,
    executor_posture: {
      ...current.executor_posture,
      isolation_claim: "unknown",
      network_isolation: "unknown",
      resource_enforcement: "unknown",
      local_host_execution_required: false,
    },
    local_host_approval_required: false,
    configuration_error: message,
  };
}

function digest(value: string | null): string {
  return value ? `${value.slice(0, 12)}…${value.slice(-8)}` : "unavailable";
}

function displayError(value: unknown): string {
  if (!value || typeof value !== "object") return "The repository sandbox settings request failed.";
  const detail = (value as { detail?: unknown }).detail;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (detail && typeof detail === "object") {
    const candidate = detail as { code?: unknown; reason?: unknown };
    if (typeof candidate.reason === "string" && candidate.reason) return candidate.reason;
    if (typeof candidate.code === "string" && candidate.code) return candidate.code;
  }
  return "The repository sandbox settings request failed.";
}

const SETTINGS_REQUEST_TIMEOUT_MS = 15_000;

class SettingsRequestTimeout extends Error {
  constructor() {
    super("The repository sandbox request exceeded its deadline.");
    this.name = "SettingsRequestTimeout";
  }
}

class SettingsRequestCancelled extends Error {
  constructor() {
    super("The repository sandbox request was cancelled.");
    this.name = "SettingsRequestCancelled";
  }
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
      reject(new SettingsRequestTimeout());
    }, SETTINGS_REQUEST_TIMEOUT_MS);
  });
  const cancellationPromise = new Promise<never>((_, reject) => {
    onAbort = () => {
      if (!deadlineExpired) reject(new SettingsRequestCancelled());
    };
    controller.signal.addEventListener("abort", onAbort, { once: true });
  });
  try {
    return await Promise.race([operationPromise, deadlinePromise, cancellationPromise]);
  } finally {
    if (deadlineTimer !== undefined) window.clearTimeout(deadlineTimer);
    if (onAbort) controller.signal.removeEventListener("abort", onAbort);
    // A timed-out fetch/body may settle later. Keep that late rejection
    // handled without allowing it to update the panel.
    void operationPromise.catch(() => undefined);
  }
}

export function RepoSandboxPanel() {
  const [value, setValue] = useState<RepoSandboxPayload>(FALLBACK);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const generationRef = useRef(0);
  const controllersRef = useRef<Set<AbortController>>(new Set());

  function invalidateRequests(): number {
    generationRef.current += 1;
    for (const controller of controllersRef.current) controller.abort();
    controllersRef.current.clear();
    return generationRef.current;
  }

  async function requestJson(init: RequestInit = {}, generation: number): Promise<unknown> {
    if (generationRef.current !== generation) return null;
    const controller = new AbortController();
    controllersRef.current.add(controller);
    try {
      const { response, payload } = await boundedJsonRequest(
        () => apiFetch(`${API_URL}/api/settings/repo-sandbox`, { ...init, signal: controller.signal }),
        controller,
      );
      if (generationRef.current !== generation) return null;
      if (!response.ok) throw new Error(displayError(payload));
      return payload;
    } catch (cause) {
      if (cause instanceof SettingsRequestTimeout || cause instanceof SettingsRequestCancelled || (cause instanceof DOMException && cause.name === "AbortError")) {
        throw new Error("The repository sandbox request timed out or was cancelled.");
      }
      throw cause;
    } finally {
      controllersRef.current.delete(controller);
    }
  }

  async function load(generation = generationRef.current) {
    if (generationRef.current !== generation) return;
    setLoading(true);
    setError(null);
    try {
      const payload = await requestJson({}, generation);
      if (generationRef.current === generation) {
        const normalized = normalizeRepoSandboxPayload(payload);
        if (normalized) {
          setValue(normalized);
        } else {
          setValue((current) => markMetadataUnavailable(current, MALFORMED_METADATA_MESSAGE));
        }
      }
    } catch (cause) {
      if (generationRef.current === generation) {
        setValue((current) => markMetadataUnavailable(current));
        setError(cause instanceof Error ? cause.message : "Repository sandbox metadata is unavailable.");
      }
    } finally {
      if (generationRef.current === generation) setLoading(false);
    }
  }

  useEffect(() => {
    const generation = invalidateRequests();
    void load(generation).catch(() => undefined);
    return () => {
      invalidateRequests();
    };
    // This panel has one fixed endpoint; the generation fence also protects
    // late responses after an unmount or an explicit refresh.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function save() {
    const generation = generationRef.current;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const payload = await requestJson({
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          executor_kind: value.executor_kind,
          enabled: value.enabled,
          docker_socket: value.docker_socket,
          worker_image_digest: value.worker_image_digest,
          profile: value.profile,
          ...(value.node_runtime_path === undefined ? {} : { node_runtime_path: value.node_runtime_path }),
        }),
      }, generation);
      if (generationRef.current === generation) {
        const normalized = normalizeRepoSandboxPayload(payload);
        if (!normalized) {
          setValue((current) => markMetadataUnavailable(current, MALFORMED_METADATA_MESSAGE));
          return;
        }
        setValue(normalized);
        setNotice("Repository sandbox settings saved. Saving never starts Docker or changes host limits.");
      }
    } catch (cause) {
      if (generationRef.current === generation) {
        setValue((current) => markMetadataUnavailable(current));
        setError(cause instanceof Error ? cause.message : "Repository sandbox settings could not be saved.");
      }
    } finally {
      if (generationRef.current === generation) setSaving(false);
    }
  }

  const metadataAvailable = value.metadata_available;
  const selectedIsLocal = value.executor_kind === "local";
  const isLocal = metadataAvailable && selectedIsLocal;
  const preflightReason = typeof value.preflight?.reason === "string" ? value.preflight.reason : "unavailable";
  const executorLabel = value.executor_kind === "local"
    ? "Trusted local staged runner"
    : value.executor_kind === "docker_rootless"
      ? "Docker rootless profile"
      : "Docker rootful profile";
  const resourceCopy = isLocal
    ? "Local execution reports admission and wall-time bounds only; CPU, memory, PID, network, and filesystem isolation are not verified."
    : metadataAvailable ? `Resource posture: ${value.executor_posture.resource_enforcement}.` : "Effective resource posture is unknown until server metadata is refreshed.";

  return (
    <section className="px-1" aria-label="Repository sandbox settings">
      <div className="text-[10px] uppercase tracking-wider text-retro-border font-bold mb-2">Repository sandbox</div>
      <div className="text-[9px] text-retro-text/60 mb-3">Choose the server-owned executor for approved repository repairs. Selection never starts Docker, changes host limits, or grants a job permission to run.</div>
      <div className="grid gap-2">
        <label className="text-[10px] text-retro-text">Execution backend
          <select
            aria-label="Repository execution backend"
            className="cockpit-input mt-1 w-full"
            value={value.executor_kind}
            onChange={(event) => {
              const next = event.currentTarget.value;
              if (!isExecutorKind(next)) return;
              setValue((current) => {
                const posture = normalizePosture({
                  kind: next,
                  profile: current.profile,
                  isolation_claim: next === "local" ? "none" : "unverified",
                  network_isolation: next === "local" ? "not_verified" : "unverified",
                  resource_enforcement: next === "local" ? "admission_and_wall_timeout_only" : "unverified",
                  limits_digest: current.limits_digest,
                  local_host_execution_required: next === "local",
                }, next, current.limits_digest) ?? current.executor_posture;
                return {
                  ...current,
                  metadata_available: false,
                  executor_kind: next,
                  executor_profile: `${next}:${current.profile}`,
                  executor_posture: posture,
                  executor_posture_digest: null,
                  local_host_approval_required: next === "local",
                  preflight: { ok: false, status: "blocked", reason: "selection_not_refreshed" },
                  preparation_ready: false,
                  execution_ready: false,
                  status: "degraded",
                  configuration_error: UNAVAILABLE_METADATA_MESSAGE,
                };
              });
            }}
          >
            <option value="local">Trusted local staged runner (default)</option>
            <option value="docker_rootless">Docker rootless profile</option>
            <option value="docker_rootful">Docker rootful profile</option>
          </select>
        </label>
        <div className="rounded border border-white/10 p-2 text-[10px] text-retro-text/70">
          <div className="font-semibold text-retro-text">{metadataAvailable ? executorLabel : "Executor metadata unavailable"}</div>
          <div className="mt-1">{!metadataAvailable ? `Selected ${value.executor_kind} posture is not effective until the server returns a complete receipt.` : isLocal ? "Approved tests run as the Seraph host user in a private staged directory. Every job requires explicit host permission." : value.executor_kind === "docker_rootless" ? "Requires a verified Linux rootless daemon, pinned worker image, and fixed resource controls." : "Uses an existing rootful daemon with independently verified limits and a non-root worker."}</div>
        </div>
        <label className="flex items-center gap-2 text-[10px] text-retro-text">
          <input type="checkbox" checked={value.enabled} onChange={(event) => {
            const enabled = event.currentTarget.checked;
            setValue((current) => ({ ...current, enabled }));
          }} />
          Enable this backend when its technical preflight is verified
        </label>
        {!selectedIsLocal && <label className="text-[10px] text-retro-text">Docker socket
          <input className="mt-1 w-full bg-transparent text-[10px] text-retro-text border-b border-retro-text/20 px-0.5 py-1 outline-none focus:border-retro-highlight" value={value.docker_socket} onChange={(event) => {
            const docker_socket = event.currentTarget.value;
            setValue((current) => ({ ...current, docker_socket }));
          }} placeholder="unix:///run/user/.../docker.sock" maxLength={512} />
        </label>}
        {!selectedIsLocal && <label className="text-[10px] text-retro-text">Pinned worker image digest
          <input className="mt-1 w-full bg-transparent text-[10px] text-retro-text border-b border-retro-text/20 px-0.5 py-1 font-mono outline-none focus:border-retro-highlight" value={value.worker_image_digest} onChange={(event) => {
            const worker_image_digest = event.currentTarget.value;
            setValue((current) => ({ ...current, worker_image_digest }));
          }} placeholder="registry.example/seraph-worker@sha256:…" maxLength={512} />
        </label>}
        <label className="text-[10px] text-retro-text">Profile
          <select aria-label="Repository execution profile" className="cockpit-input mt-1 w-full" value={value.profile} onChange={(event) => {
            const profile = event.currentTarget.value;
            if (![REPO_SANDBOX_PROFILE, "repo-node24-npm-v1"].includes(profile)) return;
            setValue((current) => ({ ...current, profile, metadata_available: false, preparation_ready: false, execution_ready: false }));
          }}>
            <option value={REPO_SANDBOX_PROFILE}>Python / pytest (default)</option>
            <option value="repo-node24-npm-v1">Node 24 / bounded test and build scripts</option>
          </select>
        </label>
        {value.profile === "repo-node24-npm-v1" && <label className="text-[10px] text-retro-text">Installed Node 24 executable
          <input aria-label="Installed Node 24 executable" className="cockpit-input mt-1 w-full font-mono" maxLength={512} value={value.node_runtime_path ?? ""} onChange={(event) => {
            const node_runtime_path = event.currentTarget.value;
            setValue((current) => ({ ...current, node_runtime_path, metadata_available: false, preparation_ready: false, execution_ready: false }));
          }} placeholder="/absolute/path/to/node" />
          <span>No downloads. Linux supervision required; unavailable profiles block.</span>
        </label>}
      </div>
      <div className={`mt-3 rounded border p-2 text-[10px] ${value.preflight?.ok ? "border-green-500/40" : "border-yellow-500/40"}`} role="status">
        <div className="font-bold">Effective status: {metadataAvailable ? value.status : "unknown"} · {metadataAvailable ? executorLabel : "executor unavailable"}</div>
        <div className="mt-1">Technical preflight: {metadataAvailable && value.preflight?.ok ? "verified" : "blocked or unknown"} · {metadataAvailable ? preflightReason : "metadata unavailable"}</div>
        <div>Preparation: {metadataAvailable && value.preparation_ready ? "ready" : "blocked or unknown"} · execution: {metadataAvailable && value.execution_ready ? "ready" : metadataAvailable && isLocal && value.preparation_ready ? "awaiting exact per-job host approval" : "blocked or unknown"}</div>
        <div>Posture: isolation {metadataAvailable ? value.executor_posture.isolation_claim : "unknown"} · network {metadataAvailable ? value.executor_posture.network_isolation : "unknown"} · {resourceCopy}</div>
        {isLocal && <div className="mt-1 text-amber-200">No isolation guarantee. Host-user filesystem and network access is visible at the job approval boundary.</div>}
        <div>{isLocal ? "Limits are fixed and non-editable metadata; host CPU, memory, and PID enforcement is not claimed" : "Limits are fixed and non-editable"} · digest {digest(value.limits_digest)} · posture digest {digest(value.executor_posture_digest)}</div>
        {metadataAvailable && !isLocal && Object.keys(value.limits).length > 0 && <div className="mt-1">Configured ceilings: CPU {value.limits.max_cpu_seconds}s · memory {Math.round(value.limits.max_memory_bytes / (1024 * 1024))} MiB · processes {value.limits.max_pids} · wall {value.limits.max_wall_seconds}s</div>}
        {value.legacy_repo_change_preflight && <div className="mt-1 text-amber-200">Legacy engineering.repo-change.v1 remains strict rootless-only: {value.legacy_repo_change_preflight.ok ? "verified" : "blocked"}.</div>}
      </div>
      {value.configuration_error && <div className="mt-2 text-[10px] text-yellow-300" role="alert">{value.configuration_error}</div>}
      {error && <div className="mt-2 text-[10px] text-red-400" role="alert">{error}</div>}
      {notice && <div className="mt-2 text-[10px] text-green-400" role="status">{notice}</div>}
      <div className="mt-3 flex gap-2">
        <button type="button" className="text-[9px] text-retro-highlight hover:text-retro-text uppercase tracking-wider" onClick={() => void save()} disabled={saving || loading}>{saving ? "saving…" : "save selectors"}</button>
        <button type="button" className="text-[9px] text-retro-text/60 hover:text-retro-highlight uppercase tracking-wider" onClick={() => void load()} disabled={loading || saving}>{loading ? "checking…" : "refresh status"}</button>
      </div>
    </section>
  );
}
