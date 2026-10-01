import { useEffect, useRef, useState } from "react";

import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";

interface RepoSandboxPayload {
  enabled: boolean;
  docker_socket: string;
  worker_image_digest: string;
  profile: string;
  limits: Record<string, number>;
  limits_digest: string | null;
  limits_editable: boolean;
  preflight: { ok?: boolean; status?: string; reason?: string; [key: string]: unknown };
  status: string;
  configuration_error?: string | null;
  operator_visible: boolean;
}

const FALLBACK: RepoSandboxPayload = {
  enabled: false,
  docker_socket: "",
  worker_image_digest: "",
  profile: "repo-python-pytest-v1",
  limits: {},
  limits_digest: null,
  limits_editable: false,
  preflight: { ok: false, status: "blocked", reason: "metadata_unavailable" },
  status: "blocked",
  configuration_error: "Settings metadata is unavailable; execution remains blocked.",
  operator_visible: true,
};

const REQUIRED_LIMITS = ["max_cpu_seconds", "max_memory_bytes", "max_pids", "max_wall_seconds"] as const;

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function normalizeRepoSandboxPayload(payload: unknown): RepoSandboxPayload | null {
  if (!isRecord(payload)) return null;
  if (
    typeof payload.enabled !== "boolean"
    || typeof payload.docker_socket !== "string"
    || typeof payload.worker_image_digest !== "string"
    || payload.profile !== "repo-python-pytest-v1"
    || payload.limits_editable !== false
    || typeof payload.status !== "string"
    || payload.operator_visible !== true
    || !(payload.limits_digest === null || typeof payload.limits_digest === "string")
    || !(payload.configuration_error === null || typeof payload.configuration_error === "string" || typeof payload.configuration_error === "undefined")
    || !isRecord(payload.limits)
    || !isRecord(payload.preflight)
  ) return null;
  const limits = payload.limits;
  if (REQUIRED_LIMITS.some((key) => typeof limits[key] !== "number" || !Number.isSafeInteger(limits[key]) || limits[key] < 1)) return null;
  const preflight = payload.preflight;
  if (typeof preflight.ok !== "undefined" && typeof preflight.ok !== "boolean") return null;
  if (typeof preflight.status !== "undefined" && typeof preflight.status !== "string") return null;
  if (typeof preflight.reason !== "undefined" && typeof preflight.reason !== "string") return null;
  return {
    enabled: payload.enabled,
    docker_socket: payload.docker_socket,
    worker_image_digest: payload.worker_image_digest,
    profile: payload.profile,
    limits: Object.fromEntries(Object.entries(limits).filter(([, value]) => typeof value === "number")) as Record<string, number>,
    limits_digest: payload.limits_digest,
    limits_editable: false,
    preflight: preflight as RepoSandboxPayload["preflight"],
    status: payload.status,
    configuration_error: typeof payload.configuration_error === "string" ? payload.configuration_error : null,
    operator_visible: true,
  };
}

const MALFORMED_METADATA_MESSAGE = "Sandbox metadata was malformed; the last known controls are retained and execution remains blocked until refreshed.";

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
          setValue((current) => ({
            ...current,
            status: "degraded",
            preflight: { ...current.preflight, ok: false, status: "degraded", reason: "metadata_malformed" },
            configuration_error: MALFORMED_METADATA_MESSAGE,
          }));
        }
      }
    } catch (cause) {
      if (generationRef.current === generation) setError(cause instanceof Error ? cause.message : "Repository sandbox metadata is unavailable.");
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
          enabled: value.enabled,
          docker_socket: value.docker_socket,
          worker_image_digest: value.worker_image_digest,
          profile: value.profile,
        }),
      }, generation);
      if (generationRef.current === generation) {
        const normalized = normalizeRepoSandboxPayload(payload);
        if (!normalized) {
          setValue((current) => ({
            ...current,
            status: "degraded",
            preflight: { ...current.preflight, ok: false, status: "degraded", reason: "metadata_malformed" },
            configuration_error: MALFORMED_METADATA_MESSAGE,
          }));
          return;
        }
        setValue(normalized);
        setNotice("Repository sandbox settings saved. Saving never starts Docker or changes host limits.");
      }
    } catch (cause) {
      if (generationRef.current === generation) setError(cause instanceof Error ? cause.message : "Repository sandbox settings could not be saved.");
    } finally {
      if (generationRef.current === generation) setSaving(false);
    }
  }

  const preflightReason = typeof value.preflight?.reason === "string" ? value.preflight.reason : "unavailable";

  return (
    <section className="px-1" aria-label="Repository sandbox settings">
      <div className="text-[10px] uppercase tracking-wider text-retro-border font-bold mb-2">Repository sandbox</div>
      <div className="text-[9px] text-retro-text/60 mb-3">Fixed rootless worker profile for approved repository repairs. Host provisioning and CPU enforcement are separate administrative prerequisites.</div>
      <div className="grid gap-2">
        <label className="flex items-center gap-2 text-[10px] text-retro-text">
          <input type="checkbox" checked={value.enabled} onChange={(event) => setValue((current) => ({ ...current, enabled: event.currentTarget.checked }))} />
          Enable the fixed profile when its preflight is verified
        </label>
        <label className="text-[10px] text-retro-text">Docker socket
          <input className="mt-1 w-full bg-transparent text-[10px] text-retro-text border-b border-retro-text/20 px-0.5 py-1 outline-none focus:border-retro-highlight" value={value.docker_socket} onChange={(event) => setValue((current) => ({ ...current, docker_socket: event.currentTarget.value }))} placeholder="unix:///run/user/.../docker.sock" maxLength={512} />
        </label>
        <label className="text-[10px] text-retro-text">Pinned worker image digest
          <input className="mt-1 w-full bg-transparent text-[10px] text-retro-text border-b border-retro-text/20 px-0.5 py-1 font-mono outline-none focus:border-retro-highlight" value={value.worker_image_digest} onChange={(event) => setValue((current) => ({ ...current, worker_image_digest: event.currentTarget.value }))} placeholder="registry.example/seraph-worker@sha256:…" maxLength={512} />
        </label>
        <label className="text-[10px] text-retro-text">Profile
          <input className="mt-1 w-full bg-transparent text-[10px] text-retro-text border-b border-retro-text/20 px-0.5 py-1 font-mono outline-none focus:border-retro-highlight" value={value.profile} readOnly aria-readonly="true" />
        </label>
      </div>
      <div className={`mt-3 rounded border p-2 text-[10px] ${value.preflight?.ok ? "border-green-500/40" : "border-yellow-500/40"}`} role="status">
        <div className="font-bold">Effective status: {value.status}</div>
        <div className="mt-1">Preflight: {value.preflight?.ok ? "verified" : "blocked"} · {preflightReason}</div>
        <div>Limits are fixed and non-editable · digest {digest(value.limits_digest)}</div>
        {Object.keys(value.limits).length > 0 && <div className="mt-1">CPU {value.limits.max_cpu_seconds}s · memory {Math.round(value.limits.max_memory_bytes / (1024 * 1024))} MiB · processes {value.limits.max_pids} · wall {value.limits.max_wall_seconds}s</div>}
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
