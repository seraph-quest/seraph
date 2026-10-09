import { useEffect, useMemo, useRef, useState } from "react";

import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import { RepoPublicationPanel } from "./RepoPublicationPanel";
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
const LOCAL_REPAIR_PROFILES = new Set([REPO_SANDBOX_PROFILE, "repo-node24-npm-v1", "repo-python-pytest-publication-v1"]);
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
  const selectedProfile = posture.profile ?? REPO_SANDBOX_PROFILE;
  if (typeof selectedProfile !== "string"
    || !(executorKind === "local" ? LOCAL_REPAIR_PROFILES.has(selectedProfile) : selectedProfile === REPO_SANDBOX_PROFILE)) {
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
  const expectedExecutorProfile = `${executorKind}:${selectedProfile}`;
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
  if (selectedProfile === "repo-python-pytest-publication-v1" && preparationReady === true
    && (posture.runtime_proof_available !== true
      || typeof posture.publication_runtime_proof_sha256 !== "string"
      || !/^[0-9a-f]{64}$/.test(posture.publication_runtime_proof_sha256))) {
    throw new Error("The selected publication profile has no verified runtime proof.");
  }
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

interface PreparedRepositoryReview {
  native_child_id: string;
  repository_job_id: string;
  iteration_index: number;
  iteration_id: string;
  preparation_digest: string;
  contact_state: "not_started" | "started" | "unknown" | "closed";
  source_preview_path: string;
}

type RepositoryReview = PreparedRepositoryReview | {
  native_child_id: string;
  repository_job_id: string;
  iteration_index: null;
  iteration_id: null;
  preparation_digest: null;
  contact_state: "not_prepared";
  source_preview_path: null;
};

interface RepositoryLimitEvidence {
  schema_version: "repository.native_limit_evidence.v1";
  original_limits_digest: string;
  original_deadline_at: string;
  original_server_bound_microusd: number;
  root_liability_microusd: number;
  group_liability_microusd: number;
  group_calls: number;
  original_root_max_cost_microusd: number;
  original_group_max_cost_microusd: number;
  original_group_max_calls: number;
  goal_cutoff_at: string | null;
  cause: string;
}

interface RepositoryStop {
  reason: string;
  pending: boolean;
  limit_evidence: RepositoryLimitEvidence | null;
  limit_evidence_digest: string | null;
}

interface RepositorySourceStatus {
  job_id: string;
  status: string;
  revision: number;
  repository_review: RepositoryReview;
  repository_stop?: RepositoryStop;
  source_recovery: RepositorySourceRecovery | null;
  patch_proposal: null | { proposal_id: string; revision: number; approval_id: string; status: string; summary: string; patch_artifact_ref: string; patch_sha256: string; expires_at: string; allowed_paths: string[]; test_args: string[] };
  approval: null | { id: string; status: string; fingerprint: string; expires_at: string };
  iterations: { index: number; input_tree_digest: string; patch_digest: string; command_refs: string[]; result_artifacts: string[] }[];
  iteration_states: { index: number; iteration_id: string; status: string; manifest_artifact_ref: string; manifest_artifact_digest: string; cleanup_proven: boolean; command_results_status: "recorded" | "unknown"; command_results: null | { check: "test" | "build"; status: "succeeded" | "failed" | "timed_out" | "cancelled" | "unknown"; exit_code: number | null }[] }[];
  recovery_action: string;
  provider_contacted: boolean;
  no_learning: true;
  operator_visible: true;
}

const SOURCE_RECOVERY_STATES = ["pending_original_producer", "held_unknown", "held_partial", "continuation_ready", "original_cleanup_committed", "original_stop_committed", "physical_cleanup_only"] as const;

interface RepositorySourceRecovery {
  state: typeof SOURCE_RECOVERY_STATES[number];
  reason: string;
  physical_hold: boolean | null;
  original_result: "succeeded" | "failed" | "held_partial" | null;
  public_actions: "unavailable";
}

function validateSourceRecovery(value: unknown): RepositorySourceRecovery | null {
  // Null is an explicit authenticated Source-owner projection, never a client
  // classification based on missing proof or a missing response field.
  if (value === null) return null;
  if (!isRecord(value) || !exactKeys(value, ["state", "reason", "physical_hold", "original_result", "public_actions"])
    || !SOURCE_RECOVERY_STATES.includes(value.state as RepositorySourceRecovery["state"])
    || typeof value.reason !== "string" || !/^[a-z][a-z0-9_]{0,127}$/.test(value.reason)
    || (value.physical_hold !== null && typeof value.physical_hold !== "boolean")
    || (value.original_result !== null && (typeof value.original_result !== "string" || !["succeeded", "failed", "held_partial"].includes(value.original_result)))
    || value.public_actions !== "unavailable") throw new Error("The repository Source recovery readback is malformed.");
  return value as unknown as RepositorySourceRecovery;
}

interface RepositorySourcePreview {
  job_id: string;
  revision: number;
  repository_review: RepositoryReview;
  source_packet: { packet_id: string; state: string; repository_ref: string; source_manifest_sha256: string; artifact_sha256: string; selected_files: { path: string; sha256: string; size_bytes: number; text: string }[]; omissions: unknown[] };
  egress: { runtime_path: string; effective_profile_id: string; effective_upstream: string; maximum_input_bytes: number; maximum_output_tokens: number; request_body: Record<string, unknown>; request_body_digest: string; request_route_digest: string; egress_envelope_digest: string; diagnostics_digest: string; diagnostics: { redaction_version: string; stdout: string; stderr: string }; redaction_version: string; combined_input_bytes: number; original_deadline_at: string; remaining_inference_calls: number; remaining_cost_microusd: number };
  provider_contacted: false;
  operator_visible: true;
}

function exactKeys(value: Record<string, unknown>, keys: string[]): boolean {
  return Object.keys(value).length === keys.length && keys.every((key) => Object.prototype.hasOwnProperty.call(value, key));
}

function safeArtifactRef(value: unknown): value is string {
  return typeof value === "string" && /^workspace-json:artifacts\/[A-Za-z0-9._/-]+$/.test(value)
    && value.length <= 512 && !value.split("/").includes("..");
}

function validateRepositoryReview(value: unknown, jobId: string): RepositoryReview {
  if (!isRecord(value) || !exactKeys(value, ["native_child_id", "repository_job_id", "iteration_index", "iteration_id", "preparation_digest", "contact_state", "source_preview_path"])
    || !isBoundedString(value.native_child_id, 128) || value.repository_job_id !== jobId) throw new Error("The repository iteration binding is malformed.");
  if (value.contact_state === "not_prepared") {
    if (["iteration_index", "iteration_id", "preparation_digest", "source_preview_path"].some((key) => value[key] !== null)) throw new Error("The unprepared repository binding is malformed.");
    return value as unknown as RepositoryReview;
  }
  if (!Number.isSafeInteger(value.iteration_index) || Number(value.iteration_index) < 1 || Number(value.iteration_index) > 3
    || !isSha256Digest(value.iteration_id) || !isSha256Digest(value.preparation_digest)
    || !["not_started", "started", "unknown", "closed"].includes(String(value.contact_state))
    || value.source_preview_path !== `/api/workflows/repo-repair/${jobId}/source-preview`) throw new Error("The repository iteration binding is malformed.");
  return value as unknown as RepositoryReview;
}

function validateRepositoryStatus(value: unknown, jobId: string): RepositorySourceStatus {
  const reject = (): never => { throw new Error("The repository Source status is malformed or belongs to another job."); };
  const keys = ["job_id", "status", "revision", "repository_review", "source_recovery", "patch_proposal", "approval", "iterations", "iteration_states", "recovery_action", "provider_contacted", "no_learning", "operator_visible"];
  if (isRecord(value) && Object.prototype.hasOwnProperty.call(value, "repository_stop")) keys.push("repository_stop");
  if (!isRecord(value) || !exactKeys(value, keys)
    || value.job_id !== jobId || !isBoundedString(value.status, 64) || !isSafeNonNegativeInteger(value.revision, Number.MAX_SAFE_INTEGER)
    || !isBoundedString(value.recovery_action, 128) || typeof value.provider_contacted !== "boolean" || value.no_learning !== true || value.operator_visible !== true
    || !Array.isArray(value.iterations) || value.iterations.length > 3 || !Array.isArray(value.iteration_states) || value.iteration_states.length !== value.iterations.length) return reject();
  const review = validateRepositoryReview(value.repository_review, jobId);
  validateSourceRecovery(value.source_recovery);
  const stop = value.repository_stop;
  const automaticReasons = ["deadline_exhausted", "cost_exhausted", "shared_group_exhausted", "goal_limit_exhausted"];
  if (Object.prototype.hasOwnProperty.call(value, "repository_stop")) {
    if (!isRecord(stop) || !exactKeys(stop, ["reason", "pending", "limit_evidence", "limit_evidence_digest"])
      || !["operator_cancelled", "iterations_exhausted", ...automaticReasons].includes(String(stop.reason)) || typeof stop.pending !== "boolean"
      || (stop.pending ? !["running", "unknown_external_effect"].includes(String(value.status)) || value.recovery_action !== "repository_stop_pending"
        : value.status !== (stop.reason === "operator_cancelled" ? "cancelled" : "failed")
          || value.recovery_action !== (stop.reason === "operator_cancelled" ? "repository_stopped" : `original_${stop.reason}`))) return reject();
    const evidence = stop.limit_evidence;
    if (automaticReasons.includes(String(stop.reason))) {
      const timestamps = (item: unknown): item is string => typeof item === "string" && item.length <= 64
        && /(?:Z|\+00:00)$/.test(item) && Number.isFinite(Date.parse(item));
      if (!isRecord(evidence) || !exactKeys(evidence, ["schema_version", "original_limits_digest", "original_deadline_at", "original_server_bound_microusd", "root_liability_microusd", "group_liability_microusd", "group_calls", "original_root_max_cost_microusd", "original_group_max_cost_microusd", "original_group_max_calls", "goal_cutoff_at", "cause"])
        || evidence.schema_version !== "repository.native_limit_evidence.v1" || !isSha256Digest(evidence.original_limits_digest)
        || !isSha256Digest(stop.limit_evidence_digest) || evidence.cause !== stop.reason || !timestamps(evidence.original_deadline_at)
        || (evidence.goal_cutoff_at !== null && !timestamps(evidence.goal_cutoff_at))
        || ["original_server_bound_microusd", "root_liability_microusd", "group_liability_microusd", "group_calls", "original_root_max_cost_microusd", "original_group_max_cost_microusd", "original_group_max_calls"].some((key) => !isSafeNonNegativeInteger(evidence[key], Number.MAX_SAFE_INTEGER))
        || Number(evidence.original_server_bound_microusd) === 0 || Number(evidence.original_group_max_calls) > 12
        || (stop.reason === "goal_limit_exhausted" && (evidence.goal_cutoff_at === null || Date.parse(String(evidence.goal_cutoff_at)) !== Date.parse(String(evidence.original_deadline_at))))) return reject();
    } else if (evidence !== null || stop.limit_evidence_digest !== null) return reject();
  }
  if (review.contact_state === "not_prepared" && (!isRecord(stop) || !automaticReasons.includes(String(stop.reason))
    || value.patch_proposal !== null || value.approval !== null || value.provider_contacted !== false
    || value.iterations.length !== 0 || value.iteration_states.length !== 0)) return reject();
  const opaqueRefs = (refs: unknown, max: number): refs is string[] => Array.isArray(refs) && refs.length >= 1 && refs.length <= max
    && new Set(refs).size === refs.length && refs.every((ref) => typeof ref === "string" && /^[A-Za-z0-9_.:-]{1,256}$/.test(ref));
  for (const [position, item] of value.iterations.entries()) {
    if (!isRecord(item) || !exactKeys(item, ["index", "input_tree_digest", "patch_digest", "command_refs", "result_artifacts"])
      || item.index !== position + 1 || !isSha256Digest(item.input_tree_digest) || !isSha256Digest(item.patch_digest)
      || !opaqueRefs(item.command_refs, 8) || !opaqueRefs(item.result_artifacts, 16)) return reject();
  }
  for (const [position, item] of value.iteration_states.entries()) {
    if (!isRecord(item) || !exactKeys(item, ["index", "iteration_id", "status", "manifest_artifact_ref", "manifest_artifact_digest", "cleanup_proven", "command_results_status", "command_results"])
      || item.index !== position + 1 || !isSha256Digest(item.iteration_id) || !["succeeded", "failed"].includes(String(item.status))
      || !safeArtifactRef(item.manifest_artifact_ref) || !isSha256Digest(item.manifest_artifact_digest) || item.cleanup_proven !== true) return reject();
    if (item.command_results_status === "unknown") {
      if (item.command_results !== null) return reject();
    } else if (item.command_results_status === "recorded") {
      if (!Array.isArray(item.command_results) || item.command_results.length < 1 || item.command_results.length > 2) return reject();
      const checks = new Set<string>();
      for (const command of item.command_results) {
        if (!isRecord(command) || !exactKeys(command, ["check", "status", "exit_code"]) || !["test", "build"].includes(String(command.check))
          || checks.has(String(command.check)) || !["succeeded", "failed", "timed_out", "cancelled", "unknown"].includes(String(command.status))
          || (command.exit_code !== null && (!Number.isSafeInteger(command.exit_code) || Number(command.exit_code) < -(2 ** 31) || Number(command.exit_code) >= 2 ** 31))
          || (command.status === "succeeded" && command.exit_code !== 0)) return reject();
        checks.add(String(command.check));
      }
    } else return reject();
  }
  const proposal = value.patch_proposal;
  const approval = value.approval;
  if (proposal !== null && (!isRecord(proposal) || !exactKeys(proposal, ["proposal_id", "revision", "approval_id", "status", "summary", "patch_artifact_ref", "patch_sha256", "expires_at", "allowed_paths", "test_args"])
    || proposal.proposal_id !== `repository-proposal:${review.iteration_id}` || proposal.approval_id !== `repository-approval:${review.iteration_id}` || !isSafeNonNegativeInteger(proposal.revision, Number.MAX_SAFE_INTEGER)
    || !isBoundedString(proposal.status, 64) || typeof proposal.summary !== "string" || proposal.summary.length > 2000
    || !safeArtifactRef(proposal.patch_artifact_ref) || !isSha256Digest(proposal.patch_sha256) || typeof proposal.expires_at !== "string" || !Number.isFinite(Date.parse(proposal.expires_at))
    || !Array.isArray(proposal.allowed_paths) || proposal.allowed_paths.length > 32 || !proposal.allowed_paths.every((x) => isBoundedString(x, 512))
    || !Array.isArray(proposal.test_args) || proposal.test_args.length > 16 || !proposal.test_args.every((x) => isBoundedString(x, 512)))) return reject();
  if (approval !== null && (!isRecord(approval) || !exactKeys(approval, ["id", "status", "fingerprint", "expires_at"])
    || !isBoundedString(approval.id, 128) || !isBoundedString(approval.status, 64) || !isSha256Digest(approval.fingerprint)
    || typeof approval.expires_at !== "string" || !Number.isFinite(Date.parse(approval.expires_at)) || !isRecord(proposal) || proposal.approval_id !== approval.id)) return reject();
  return value as unknown as RepositorySourceStatus;
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
  const [repositoryStatus, setRepositoryStatus] = useState<RepositorySourceStatus | null>(null);
  const [repositoryPreview, setRepositoryPreview] = useState<RepositorySourcePreview | null>(null);
  const [sourceMutationUncertain, setSourceMutationUncertain] = useState(false);
  const [acknowledgedSource, setAcknowledgedSource] = useState(false);
  const [acknowledgedDiagnostics, setAcknowledgedDiagnostics] = useState(false);
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
    setRepositoryStatus(null);
    setSourceMutationUncertain(false);
    setRepositoryPreview(null);
    setAcknowledgedSource(false);
    setAcknowledgedDiagnostics(false);
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

  async function readProjection(generation: number): Promise<WorkBoardRepoRepairProjection | null> {
    const payload = await requestJson(endpoint, {}, generation);
    if (isRecord(payload) && Object.prototype.hasOwnProperty.call(payload, "repository_review")) {
      const next = validateRepositoryStatus(payload, jobId);
      if (repositoryStatus && (next.repository_review.native_child_id !== repositoryStatus.repository_review.native_child_id
        || next.revision < repositoryStatus.revision)) throw new Error("The repository Source status changed the original binding.");
      if (isCurrent(generation)) {
        setRepositoryStatus(next);
        setSourceMutationUncertain(false);
        setProjection(null);
        setRepositoryPreview(null);
        setAcknowledgedSource(false);
        setAcknowledgedDiagnostics(false);
      }
      return null;
    }
    const next = validateProjection(payload);
    if (isCurrent(generation)) setRepositoryStatus(null);
    return next;
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
        setRepositoryStatus(null);
        setRepositoryPreview(null);
        setAcknowledgedSource(false);
        setAcknowledgedDiagnostics(false);
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
    setRepositoryStatus(null);
    setSourceMutationUncertain(false);
    setRepositoryPreview(null);
    setAcknowledgedSource(false);
    setAcknowledgedDiagnostics(false);
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

  function sourceMutationKey(kind: "consent" | "resume", fingerprint: string): string {
    // Each original iteration retains its own key, including uncertain replies.
    const storageKey = `${mutationStorageKey(jobId, kind)}:${bindingKey}:${fingerprint}`;
    let key = mutationFallbackRef.current.get(storageKey)?.key;
    try { key ??= window.sessionStorage.getItem(storageKey) ?? undefined; } catch { /* memory fallback */ }
    key ??= makeRequestKey();
    mutationFallbackRef.current.set(storageKey, { key, fingerprint });
    try { window.sessionStorage.setItem(storageKey, key); } catch { /* memory fallback */ }
    return key;
  }

  async function inspectRepositorySource() {
    const current = repositoryStatus;
    const generation = generationRef.current;
    if (loading || busy || sourceMutationUncertain || !current || current.repository_stop || current.status !== "running" || current.recovery_action !== "review_code_egress" || current.repository_review.contact_state !== "not_started") return;
    setSourceLoading(true);
    setSourceError(null);
    setRepositoryPreview(null);
    setAcknowledgedSource(false);
    setAcknowledgedDiagnostics(false);
    try {
      const payload = await requestJson(`${endpoint}/source-preview`, {}, generation);
      const reject = (): never => { throw new Error("The exact source and diagnostics preview is unavailable or stale."); };
      if (!isRecord(payload) || !exactKeys(payload, ["job_id", "status", "revision", "recovery_action", "repository_review", "source_packet", "egress", "provider_contacted", "operator_visible"])
        || payload.job_id !== jobId || payload.status !== "running" || payload.revision !== current.revision || payload.recovery_action !== "review_code_egress"
        || payload.provider_contacted !== false || payload.operator_visible !== true) return reject();
      const review = validateRepositoryReview(payload.repository_review, jobId);
      if (Object.keys(review).some((key) => review[key as keyof RepositoryReview] !== current.repository_review[key as keyof RepositoryReview])) return reject();
      const packet = payload.source_packet;
      const egress = payload.egress;
      if (!isRecord(packet) || !exactKeys(packet, ["packet_id", "state", "repository_ref", "source_manifest_sha256", "artifact_sha256", "selected_files", "omissions"]) || !isSha256Digest(packet.artifact_sha256) || !isSha256Digest(packet.source_manifest_sha256)
        || !isBoundedString(packet.packet_id, 128) || packet.state !== "verified" || !isBoundedString(packet.repository_ref, 512)
        || !Array.isArray(packet.selected_files) || packet.selected_files.length < 1 || packet.selected_files.length > SOURCE_PREVIEW_MAX_FILES
        || !Array.isArray(packet.omissions) || packet.omissions.length !== 0 || !isRecord(egress)
        || !exactKeys(egress, ["runtime_path", "effective_profile_id", "effective_upstream", "maximum_input_bytes", "maximum_output_tokens", "request_body", "request_body_digest", "request_route_digest", "egress_envelope_digest", "diagnostics_digest", "diagnostics", "redaction_version", "combined_input_bytes", "original_deadline_at", "remaining_inference_calls", "remaining_cost_microusd"])) return reject();
      for (const file of packet.selected_files) {
        if (!isRecord(file) || !exactKeys(file, ["path", "sha256", "size_bytes", "text"]) || !isBoundedString(file.path, 512) || file.path.startsWith("/") || file.path.split("/").includes("..")
          || !isSha256Digest(file.sha256) || typeof file.text !== "string" || !isSafeNonNegativeInteger(file.size_bytes, SOURCE_PREVIEW_MAX_PACKET_BYTES)
          || new TextEncoder().encode(file.text).length !== file.size_bytes) return reject();
      }
      for (const key of ["request_body_digest", "request_route_digest", "egress_envelope_digest", "diagnostics_digest"]) if (!isSha256Digest(egress[key])) return reject();
      if (egress.runtime_path !== "strategist_agent" || !isBoundedString(egress.effective_profile_id, 128) || !isBoundedString(egress.effective_upstream, 128)
        || !isSafeNonNegativeInteger(egress.maximum_input_bytes, SOURCE_PREVIEW_MAX_PACKET_BYTES) || Number(egress.maximum_input_bytes) < 1
        || !isSafeNonNegativeInteger(egress.maximum_output_tokens, SOURCE_PREVIEW_MAX_OUTPUT_TOKENS) || Number(egress.maximum_output_tokens) < 1
        || !isSafeNonNegativeInteger(egress.combined_input_bytes, Number(egress.maximum_input_bytes))
        || !isRecord(egress.request_body) || egress.request_body.max_tokens !== egress.maximum_output_tokens
        || !isBoundedString(egress.request_body.model, 256) || !isFiniteFutureTimestamp(egress.original_deadline_at)
        || !isSafeNonNegativeInteger(egress.remaining_inference_calls, Number.MAX_SAFE_INTEGER) || Number(egress.remaining_inference_calls) < 1
        || !isSafeNonNegativeInteger(egress.remaining_cost_microusd, Number.MAX_SAFE_INTEGER)
        || !isBoundedString(egress.redaction_version, 256) || !isRecord(egress.diagnostics)
        || !exactKeys(egress.diagnostics, ["redaction_version", "stdout", "stderr"])
        || egress.diagnostics.redaction_version !== egress.redaction_version || typeof egress.diagnostics.stdout !== "string" || typeof egress.diagnostics.stderr !== "string"
        || new TextEncoder().encode(JSON.stringify(egress.request_body)).length > Number(egress.maximum_input_bytes)) return reject();
      const messages = egress.request_body.messages;
      if (!Array.isArray(messages)) return reject();
      const user = messages.find((message) => isRecord(message) && message.role === "user");
      if (!isRecord(user) || typeof user.content !== "string") return reject();
      const envelope: unknown = JSON.parse(user.content);
      const selectedFiles = packet.selected_files as Record<string, unknown>[];
      const diagnostics = egress.diagnostics as Record<string, unknown>;
      if (!isRecord(envelope) || envelope.iteration_id !== review.iteration_id || !isRecord(envelope.source_packet)
        || envelope.source_packet.owner_principal_id !== ownerPrincipalId || envelope.source_packet.owner_session_id !== ownerSessionId
        || envelope.source_packet.workflow_run_id !== jobId || envelope.source_packet.packet_id !== packet.packet_id
        || envelope.source_packet.source_manifest_sha256 !== packet.source_manifest_sha256
        || !Array.isArray(envelope.source_packet.files) || envelope.source_packet.files.length !== packet.selected_files.length
        || envelope.source_packet.files.some((file, index) => !isRecord(file) || !["path", "sha256", "size_bytes", "text"].every((key) => file[key] === selectedFiles[index][key]))
        || !isRecord(envelope.diagnostics) || !["stdout", "stderr", "redaction_version"].every((key) => (envelope.diagnostics as Record<string, unknown>)[key] === diagnostics[key])) return reject();
      if (isCurrent(generation)) setRepositoryPreview(payload as unknown as RepositorySourcePreview);
    } catch (cause) {
      if (isCurrent(generation) && !(cause instanceof StaleRepairRequest)) setSourceError(cause instanceof Error ? cause.message : "The private source preview is unavailable.");
    } finally { if (isCurrent(generation)) setSourceLoading(false); }
  }

  async function continueRepository(kind: "consent" | "resume") {
    const current = repositoryStatus;
    const preview = repositoryPreview;
    const generation = generationRef.current;
    if (loading || busy || sourceMutationUncertain || !current || current.repository_stop || current.repository_review.contact_state === "not_prepared" || current.status !== "running") return;
    if (kind === "consent" && (!preview || !acknowledgedSource || !acknowledgedDiagnostics || !isFiniteFutureTimestamp(preview.egress.original_deadline_at)
      || current.recovery_action !== "review_code_egress" || current.repository_review.contact_state !== "not_started")) return;
    const proposal = current.patch_proposal;
    const approval = current.approval;
    if (kind === "resume" && (!proposal || !approval || approval.status !== "approved" || proposal.status !== "awaiting_approval"
      || current.recovery_action !== "execute_approved_patch" || !isFiniteFutureTimestamp(approval.expires_at) || !isFiniteFutureTimestamp(proposal.expires_at))) return;
    setBusy(true);
    setError(null);
    setSourceError(null);
    setNotice(null);
    try {
      const iteration = current.repository_review;
      const fingerprint = [iteration.iteration_id, current.revision, preview?.egress.request_body_digest ?? proposal?.proposal_id, proposal?.revision ?? ""].join(":");
      const body = kind === "consent" && preview ? {
        expected_job_revision: current.revision, source_packet_digest: preview.source_packet.artifact_sha256,
        expected_source_manifest_digest: preview.source_packet.source_manifest_sha256, expected_profile_id: preview.egress.effective_profile_id,
        acknowledged_selected_source: true, expected_iteration_index: iteration.iteration_index, expected_iteration_id: iteration.iteration_id,
        expected_preparation_digest: iteration.preparation_digest, expected_request_body_digest: preview.egress.request_body_digest,
        expected_request_route_digest: preview.egress.request_route_digest, expected_egress_envelope_digest: preview.egress.egress_envelope_digest,
        expected_diagnostics_digest: preview.egress.diagnostics_digest, expected_redaction_version: preview.egress.redaction_version, acknowledged_diagnostics: true,
        idempotency_key: sourceMutationKey(kind, fingerprint),
      } : { approval_id: approval!.id, proposal_id: proposal!.proposal_id, expected_proposal_revision: proposal!.revision,
        expected_job_revision: current.revision, idempotency_key: sourceMutationKey(kind, fingerprint) };
      const receipt = await requestJson(`${endpoint}/${kind === "consent" ? "code-egress-consent" : "resume"}`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      }, generation);
      if (!isRecord(receipt) || receipt.job_id !== jobId || (kind === "consent"
        ? !isBoundedString(receipt.consent_id, 128) || receipt.operator_visible !== true
        : receipt.iteration_id !== iteration.iteration_id || receipt.no_learning !== true || typeof receipt.cleanup_proven !== "boolean")) throw new Error("The continuation receipt is malformed; the exact request key is retained.");
      if (kind === "consent") {
        const outcome = receipt.repository_outcome;
        const wait = isRecord(outcome) ? outcome.wait : null;
        const witness = isRecord(wait) ? wait.witness : null;
        const native = isRecord(witness) ? witness.native_binding : null;
        if (!isRecord(outcome) || outcome.child_id !== iteration.native_child_id || !isRecord(witness)
          || witness.repository_job_id !== jobId || witness.iteration_id !== iteration.iteration_id
          || witness.request_body_digest !== preview!.egress.request_body_digest || !isRecord(native)
          || native.invocation_id !== iteration.native_child_id || native.original_root_id !== ownerSessionId
          || native.owner_principal_id !== ownerPrincipalId) throw new Error("The consent receipt changed the original source binding; the exact request key is retained.");
      }
      const next = validateRepositoryStatus(await requestJson(endpoint, {}, generation), jobId);
      if (next.repository_review.native_child_id !== iteration.native_child_id || next.revision < current.revision
        || next.repository_review.iteration_index === null || next.repository_review.iteration_index < iteration.iteration_index) throw new Error("The continuation readback changed the original repository binding.");
      if (kind === "resume") {
        if (!safeArtifactRef(receipt.manifest_artifact_ref) || !isSha256Digest(receipt.manifest_artifact_digest)
          || receipt.cleanup_proven !== true || !["failed", "succeeded"].includes(String(receipt.status))) throw new Error("Physical execution readback remains unverified.");
        const continuation = receipt.repository_review;
        if (isRecord(continuation)) {
          if (Object.keys(next.repository_review).some((key) => continuation[key] !== next.repository_review[key as keyof RepositoryReview])) throw new Error("The execution readback changed its next original iteration.");
        } else if (receipt.status === "succeeded" && (next.status !== "succeeded" || next.repository_review.iteration_id !== iteration.iteration_id)) {
          throw new Error("The final local execution readback did not match the original iteration.");
        }
      }
      if (kind === "consent" && (next.repository_review.iteration_id !== iteration.iteration_id || next.repository_review.contact_state === "not_started")) throw new Error("The source consent readback has not confirmed the original iteration contact.");
      if (isCurrent(generation)) {
        setRepositoryStatus(next);
        setSourceMutationUncertain(false);
        setRepositoryPreview(null);
        setAcknowledgedSource(false);
        setAcknowledgedDiagnostics(false);
        setNotice(kind === "consent" ? "Source and diagnostics consent recorded for this iteration." : "Reviewed patch execution recorded. Review the current outcome.");
      }
    } catch (cause) {
      if (isCurrent(generation) && !(cause instanceof StaleRepairRequest)) {
        setSourceMutationUncertain(true);
        setRepositoryPreview(null);
        setAcknowledgedSource(false);
        setAcknowledgedDiagnostics(false);
        setError(cause instanceof Error ? cause.message : "Repository continuation failed.");
      }
    } finally { if (isCurrent(generation)) setBusy(false); }
  }

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

  const repositoryForRender = hasCurrentBinding && repositoryStatus?.job_id === jobId
    && generationScopeRef.current.bindingKey === bindingKey ? repositoryStatus : null;
  if (repositoryForRender) {
    const current = repositoryForRender;
    const review = current.repository_review;
    const preview = repositoryPreview;
    const effectsBlocked = Boolean(current.repository_stop) || review.contact_state === "not_prepared"
      || (current.source_recovery !== null && current.source_recovery.state !== "continuation_ready");
    const canInspect = !effectsBlocked && !sourceMutationUncertain && current.status === "running" && current.recovery_action === "review_code_egress" && review.contact_state === "not_started";
    const canExecute = !effectsBlocked && !sourceMutationUncertain && current.status === "running" && current.recovery_action === "execute_approved_patch" && current.patch_proposal?.status === "awaiting_approval"
      && current.approval?.status === "approved" && isFiniteFutureTimestamp(current.approval.expires_at) && isFiniteFutureTimestamp(current.patch_proposal.expires_at);
    return <section className="rounded border border-cyan-400/30 p-3" aria-label="Repository repair execution">
      <div className="font-semibold">Repository repair</div>
      <div>{statusLabel(current.status)}{review.contact_state !== "not_prepared" && <> · iteration {review.iteration_index} of at most 3</>}</div>
      <div>Provider contact: {current.provider_contacted ? "recorded" : "not recorded"} · no learning</div>
      {current.source_recovery && <div aria-label="Original Source recovery readback">
        <div>Original recovery: {statusLabel(current.source_recovery.state)}</div>
        <div>Recovery reason: {current.source_recovery.reason}</div>
        <div>Original physical hold: {current.source_recovery.physical_hold === null ? "unknown" : current.source_recovery.physical_hold ? "held" : "released"}</div>
        <div>Original result: {current.source_recovery.original_result === null ? "unavailable" : statusLabel(current.source_recovery.original_result)}</div>
        <div role="status">Public Source recovery actions are unavailable pending acceptance. Refresh reads the original durable status; no recovery is retried.</div>
      </div>}
      {current.repository_stop && <div>
        <div>Stop reason: {current.repository_stop.reason}</div>
        <div role="status">{current.repository_stop.pending ? "Original reservation remains held. Physical cleanup is pending verification." : "Original repository stop recorded."}</div>
        {current.repository_stop.limit_evidence && <div>
          <div>Recorded Root cost: {current.repository_stop.limit_evidence.root_liability_microusd} microusd · original limit {current.repository_stop.limit_evidence.original_root_max_cost_microusd} microusd</div>
          <div>Recorded group cost: {current.repository_stop.limit_evidence.group_liability_microusd} microusd · original limit {current.repository_stop.limit_evidence.original_group_max_cost_microusd} microusd</div>
          <div>Recorded group calls: {current.repository_stop.limit_evidence.group_calls} · original limit {current.repository_stop.limit_evidence.original_group_max_calls}</div>
          <div>Original cutoff: {current.repository_stop.limit_evidence.original_deadline_at}</div>
          {current.repository_stop.limit_evidence.goal_cutoff_at && <div>Original Goal cutoff: {current.repository_stop.limit_evidence.goal_cutoff_at}</div>}
        </div>}
      </div>}
      <button type="button" disabled={busy || loading} onClick={() => void refresh()}>Refresh repair status</button>
      {sourceMutationUncertain && <div role="status">Continuation outcome is uncertain. Refresh the original repair before another action.</div>}
      {error && <div role="alert">{error}</div>}{sourceError && <div role="alert">{sourceError}</div>}{notice && <div role="status">{notice}</div>}
      {canInspect && <button type="button" disabled={busy || loading || sourceLoading} onClick={() => void inspectRepositorySource()}>Inspect exact source and diagnostics</button>}
      {canInspect && preview && <div>
        <div>{preview.egress.effective_profile_id} via {preview.egress.effective_upstream} · {preview.egress.combined_input_bytes} / {preview.egress.maximum_input_bytes} input bytes · {preview.egress.maximum_output_tokens} output tokens</div>
        <div>Original cutoff: {new Date(preview.egress.original_deadline_at).toLocaleString()} · {preview.egress.remaining_inference_calls} calls remaining</div>
        <div>Redaction: {preview.egress.redaction_version}</div>
        {preview.source_packet.selected_files.map((file) => <details key={file.path}><summary>{file.path} · {file.size_bytes} bytes · {safeDigest(file.sha256)}</summary><pre className="whitespace-pre-wrap break-all">{file.text}</pre></details>)}
        <details><summary>Exact request body</summary><pre className="whitespace-pre-wrap break-all">{JSON.stringify(preview.egress.request_body, null, 2)}</pre></details>
        <details><summary>Command diagnostics</summary><pre className="whitespace-pre-wrap break-all">{preview.egress.diagnostics.stdout || "No stdout"}{"\n"}{preview.egress.diagnostics.stderr || "No stderr"}</pre></details>
        <label><input type="checkbox" checked={acknowledgedSource} onChange={(event) => setAcknowledgedSource(event.target.checked)} />I acknowledge the exact selected source</label>
        <label><input type="checkbox" checked={acknowledgedDiagnostics} onChange={(event) => setAcknowledgedDiagnostics(event.target.checked)} />I acknowledge these command diagnostics</label>
        <button type="button" disabled={busy || loading || sourceLoading || !acknowledgedSource || !acknowledgedDiagnostics || !isFiniteFutureTimestamp(preview.egress.original_deadline_at)} onClick={() => void continueRepository("consent")}>Allow this iteration's source and diagnostics</button>
      </div>}
      {current.patch_proposal && <div>
        <div>Patch: {current.patch_proposal.summary}</div><div>Proposal {current.patch_proposal.proposal_id} · revision {current.patch_proposal.revision}</div>
        <div>Approval: {statusLabel(current.approval?.status)} · {current.approval?.id ?? "unavailable"}</div>
        <div>Patch artifact: {current.patch_proposal.patch_artifact_ref} · {safeDigest(current.patch_proposal.patch_sha256)}</div>
        <div>Selected paths: {current.patch_proposal.allowed_paths.join(", ")} · named check arguments: {current.patch_proposal.test_args.join(" ")}</div>
        {!effectsBlocked && current.status === "running" && current.approval?.status === "pending" && onOpenApprovals && <button type="button" disabled={busy || loading} onClick={onOpenApprovals}>Review exact patch approval</button>}
        {canExecute && <button type="button" disabled={busy || loading} onClick={() => void continueRepository("resume")}>Execute this approved patch</button>}
      </div>}
      {current.iterations.map((iteration, position) => {
        const outcome = current.iteration_states[position];
        return <details key={iteration.index}><summary>Iteration {iteration.index}: {statusLabel(outcome.status)} · cleanup proven</summary>
          <div>Input tree: {safeDigest(iteration.input_tree_digest)} · patch: {safeDigest(iteration.patch_digest)}</div>
          <div>Command references: {iteration.command_refs.join(", ")}</div><div>Result references: {iteration.result_artifacts.join(", ")}</div>
          <div>{outcome.manifest_artifact_ref} · {safeDigest(outcome.manifest_artifact_digest)}</div>
          {outcome.command_results_status === "unknown" ? <div>Command outcomes: Unknown</div> : outcome.command_results?.map((command) => <div key={command.check}>{command.check}: {statusLabel(command.status)} · exit {command.exit_code ?? "unknown"}</div>)}
        </details>;
      })}
      <div role="status">{current.status === "unknown_external_effect" ? "Unknown outcome. Reconcile the original repository execution, reservation and debt. Physical cleanup is pending verification." : statusLabel(current.recovery_action)}</div>
      {current.status === "succeeded" && <div>The recorded named checks passed. Review the local patch; passing checks do not establish patch quality or authorize publication.</div>}
    </section>;
  }

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
        {posture.profile === "repo-node24-npm-v1" ? <div>Recorded job preflight: {typeof projectionForRender.preflight?.status === "string" ? projectionForRender.preflight.status : "blocked"}</div> : <div>Preflight: {projectionForRender.preflight?.ok === true ? "verified" : `blocked or unknown${projectionForRender.preflight && typeof projectionForRender.preflight.reason === "string" ? ` · ${projectionForRender.preflight.reason}` : ""}`}</div>}
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
      {status === "succeeded" && hasCurrentBinding && ownerPrincipalId && ownerSessionId && <RepoPublicationPanel repair={projectionForRender} ownerPrincipalId={ownerPrincipalId} ownerSessionId={ownerSessionId} onOpenApprovals={onOpenApprovals} />}
      {error && <div className="mt-2 rounded border border-amber-500/40 p-2" role="alert">{error}</div>}
    </section>
  );
}
