import { useEffect, useMemo, useRef, useState } from "react";

import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import type {
  WorkBoardRepoRepairProjection,
  WorkBoardRepoRepairSourcePreview,
} from "../../types";

interface RepoRepairInspectorProps {
  jobId: string;
  onOpenApprovals?: () => void;
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

function makeRequestKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return `repo-repair-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

const REPAIR_REQUEST_TIMEOUT_MS = 15_000;

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

export function RepoRepairInspector({ jobId, onOpenApprovals }: RepoRepairInspectorProps) {
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

  function isCurrent(generation: number): boolean {
    return generationRef.current === generation;
  }

  function invalidateRequests(): number {
    generationRef.current += 1;
    for (const controller of controllersRef.current) controller.abort();
    controllersRef.current.clear();
    return generationRef.current;
  }

  async function requestJson(path: string, init: RequestInit = {}, generation: number): Promise<unknown> {
    if (!isCurrent(generation)) throw new StaleRepairRequest();
    const controller = new AbortController();
    controllersRef.current.add(controller);
    try {
      const { response, payload } = await boundedJsonRequest(
        () => apiFetch(path, { ...init, signal: controller.signal }),
        controller,
      );
      if (!isCurrent(generation)) throw new StaleRepairRequest();
      if (!response.ok) throw new Error(errorMessage(payload, "The repair request failed."));
      return payload;
    } catch (cause) {
      if (cause instanceof StaleRepairRequest) throw cause;
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
    if (!next.owner_principal_id || !next.operator_session_id) {
      throw new Error("The repair status response has no operator ownership binding.");
    }
    const binding = `${next.owner_principal_id}:${next.operator_session_id}`;
    if (ownerBindingRef.current && ownerBindingRef.current !== binding) {
      // A mounted cockpit can outlive an operator-session rotation.  Clear
      // the old projection and private preview before surfacing the binding
      // error so a late response cannot leave the previous owner's source on
      // screen.
      setProjection(null);
      setSourcePreview(null);
      throw new Error("The repair status response changed operator ownership.");
    }
    ownerBindingRef.current = binding;
    return next as WorkBoardRepoRepairProjection;
  }

  async function readProjection(generation: number): Promise<WorkBoardRepoRepairProjection> {
    return validateProjection(await requestJson(endpoint, {}, generation));
  }

  async function refresh(generation = generationRef.current) {
    if (!isCurrent(generation)) return;
    setLoading(true);
    setError(null);
    try {
      const next = await readProjection(generation);
      if (isCurrent(generation)) setProjection(next);
    } catch (cause) {
      if (isCurrent(generation) && !(cause instanceof StaleRepairRequest)) {
        setError(cause instanceof Error ? cause.message : "The repair status could not be read.");
      }
    } finally {
      if (isCurrent(generation)) setLoading(false);
    }
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
    if (!payload || typeof payload !== "object") throw new Error("The private source preview is malformed.");
    const next = payload as WorkBoardRepoRepairSourcePreview;
    const expected = current.source_packet;
    if (
      !next.operator_visible
      || next.job_id !== jobId
      || !next.source_packet
      || !Array.isArray(next.source_packet.selected_files)
      || !expected
      || next.source_packet.packet_id !== expected.packet_id
      || next.source_packet.artifact_sha256 !== expected.artifact_sha256
      || next.source_packet.source_manifest_sha256 !== expected.source_manifest_sha256
    ) {
      throw new Error("The source preview did not match the current owner-bound packet.");
    }
    return next;
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
    setLoading(true);
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
  }, [endpoint]);

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
      if (!payload || typeof payload !== "object" || (payload as { job_id?: unknown }).job_id !== jobId) throw new Error("The consent receipt is bound to a different repair.");
      if (isCurrent(generation)) {
        setNotice("Selected source consent recorded. The same durable root will continue after the next board pass.");
        setSourcePreview(null);
      }
      await refresh(generation);
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

  if (loading && !projection) {
    return <section className="rounded border border-cyan-400/30 bg-cyan-950/10 p-3" aria-label="Repository repair execution"><div className="font-semibold">Repository repair</div><div className="mt-1 text-[11px] opacity-80">Loading owner-bound repair status…</div></section>;
  }

  if (error && !projection) {
    return <section className="rounded border border-amber-500/40 bg-amber-950/10 p-3" aria-label="Repository repair execution"><div className="font-semibold">Repository repair</div><div className="mt-1" role="alert">{error}</div><button type="button" className="cockpit-feedback-button mt-2" onClick={() => void refresh()}>Refresh repair status</button></section>;
  }

  if (!projection) return null;
  const packet = projection.source_packet;
  const proposal = projection.proposal;
  const approval = projection.approval;
  const status = projection.status;
  const canConsent = Boolean(packet && sourcePreview && !projection.egress);
  const canResume = Boolean(
    proposal
    && approval
    && approval.status === "approved"
    && ["awaiting_approval", "queued", "running"].includes(status),
  );
  const terminal = ["succeeded", "failed", "cancelled", "blocked", "unknown_external_effect", "cost_liability"].includes(status);

  return (
    <section className="rounded border border-cyan-400/30 bg-cyan-950/10 p-3" aria-label="Repository repair execution">
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="font-semibold">Repository repair execution</div>
          <div className="mt-1 text-[11px] opacity-80">{statusLabel(status)} · durable revision {projection.revision ?? "unavailable"}</div>
        </div>
        <button type="button" className="cockpit-feedback-button" onClick={() => void refresh()} disabled={busy || loading}>Refresh</button>
      </div>
      <div className="mt-2 grid gap-1 text-[11px]">
        <div>Root <span className="font-mono break-all">{projection.job_id}</span> · authority <span className="font-mono">{safeDigest(projection.authority_digest)}</span></div>
        <div>Preflight: {projection.preflight?.status === "verified" ? "verified" : `blocked or unknown${projection.preflight && typeof projection.preflight.reason === "string" ? ` · ${projection.preflight.reason}` : ""}`}</div>
        <div>Memory: {projection.memory_status} · provider contact: {projection.execution.provider_contacted ? "recorded" : "not recorded"}</div>
      </div>

      {packet && (
        <div className="mt-3 rounded border border-white/10 p-2">
          <div className="font-semibold">Selected source packet</div>
          <div className="mt-1 break-all">{packet.repository_ref} · packet {safeDigest(packet.artifact_sha256)} · source manifest {safeDigest(packet.source_manifest_sha256)}</div>
          {!projection.egress && <div className="mt-1 text-amber-200">Private source stays local until you explicitly inspect and acknowledge this packet.</div>}
          <div className="mt-2 flex flex-wrap gap-2">
            <button type="button" className="cockpit-feedback-button" onClick={() => void inspectSource()} disabled={sourceLoading || busy}>{sourceLoading ? "Loading selected source…" : "Inspect selected source"}</button>
            {canConsent && <button type="button" className="cockpit-feedback-button" onClick={() => void grantSourceConsent()} disabled={busy}>Allow exact source packet</button>}
          </div>
          {sourceError && <div className="mt-2 rounded border border-amber-500/40 p-2" role="alert">{sourceError}</div>}
          {sourcePreview && (
            <div className="mt-2 grid gap-2" aria-label="Private source preview">
              <div className="text-[10px] opacity-75">Explicit owner preview only · {sourcePreview.source_packet.selected_files.length} selected file(s) · no provider contact recorded.</div>
              {sourcePreview.source_packet.selected_files.map((file) => (
                <article key={`${file.path}:${file.sha256}`} className="rounded bg-black/20 p-2">
                  <div className="font-mono text-[10px]">{file.path} · {file.size_bytes} bytes · {safeDigest(file.sha256)}</div>
                  <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-words text-[10px]">{file.text}</pre>
                </article>
              ))}
            </div>
          )}
        </div>
      )}

      {projection.egress && (
        <div className="mt-3 rounded border border-emerald-500/30 p-2">
          <div className="font-semibold">Governed model consent</div>
          <div className="mt-1">{projection.egress.effective_profile_id} via {projection.egress.effective_upstream} · expires {new Date(projection.egress.expires_at).toLocaleString()}</div>
          <div className="text-[10px] opacity-75">Input bound to {projection.egress.maximum_input_bytes} bytes and {projection.egress.maximum_output_tokens} output tokens.</div>
        </div>
      )}

      {proposal && (
        <div className="mt-3 rounded border border-white/10 p-2">
          <div className="flex items-center justify-between gap-2"><div className="font-semibold">Reviewed repair proposal</div><span>{statusLabel(proposal.status)} · revision {proposal.revision}</span></div>
          <div className="mt-1">Patch digest <span className="font-mono">{safeDigest(proposal.patch_sha256)}</span> · model profile {proposal.model_profile_id}</div>
          {approval && <div className="mt-1">Exact approval <span className="font-mono break-all">{approval.approval_id}</span> · {statusLabel(approval.status)}{approval.expires_at ? ` · expires ${new Date(approval.expires_at).toLocaleString()}` : ""}</div>}
          <div className="mt-2 flex flex-wrap gap-2">
            {approval?.status === "pending" && onOpenApprovals && <button type="button" className="cockpit-feedback-button" onClick={onOpenApprovals}>Review exact approval</button>}
            {approval?.status === "approved" && canResume && <button type="button" className="cockpit-feedback-button" onClick={() => void resumeApprovedProposal()} disabled={busy}>Resume approved repair</button>}
            {approval?.status === "denied" && <span className="text-amber-200">Approval denied. Prepare a fresh review after checking the current source and goal.</span>}
          </div>
        </div>
      )}

      <div className="mt-3 rounded border border-white/10 p-2">
        <div className="font-semibold">Sandbox readback</div>
        {projection.execution.readback ? (
          <div className="mt-1">{statusLabel(projection.execution.readback.status)} · {projection.execution.readback.verified ? "independently verified" : "verification unavailable"} · {projection.execution.readback.target_path ?? "target unavailable"}</div>
        ) : <div className="mt-1 text-amber-200">No verified readback receipt is available.</div>}
        {projection.execution.artifacts.length > 0 && <div className="mt-1 text-[10px]">{projection.execution.artifacts.length} bounded execution artifact(s) are recorded by digest.</div>}
        {terminal && status !== "succeeded" && <div className="mt-2 rounded border border-amber-500/40 p-2" role="status">Recovery: {status === "unknown_external_effect" || status === "cost_liability" ? "reconcile the exact sandbox effect before any retry" : projection.recovery_action.replace(/_/g, " ")}.</div>}
      </div>
      {notice && <div className="mt-2 rounded border border-emerald-500/40 p-2" role="status">{notice}</div>}
      {error && <div className="mt-2 rounded border border-amber-500/40 p-2" role="alert">{error}</div>}
    </section>
  );
}
