export function githubReadbackRequest(acknowledged: boolean, revision: unknown, remoteId?: number) {
  if (acknowledged !== true || typeof revision !== "number" || !Number.isSafeInteger(revision) || revision < 1 || (remoteId !== undefined && (!Number.isSafeInteger(remoteId) || remoteId < 1))) {
    throw new Error("Explicit current-revision GitHub readback acknowledgment required");
  }
  return { acknowledged_readback: true, expected_connection_revision: revision, ...(remoteId === undefined ? {} : { remote_id: remoteId }) };
}

export interface GitHubCapacityClosure {
  closure_id: string;
  artifact_id: string;
  artifact_sha256: string;
  closed_at: string;
  observation_only: true;
  native_kind: "github_followthrough_v1" | "engineering.repo-publication.v1";
}

export function githubCapacityClosure(value: unknown): GitHubCapacityClosure | null {
  if (value === undefined || value === null) return null;
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Invalid GitHub capacity closure receipt");
  const row = value as Record<string, unknown>;
  if (typeof row.closure_id !== "string" || !row.closure_id || typeof row.artifact_id !== "string" || !row.artifact_id
    || typeof row.artifact_sha256 !== "string" || !/^[a-f0-9]{64}$/.test(row.artifact_sha256)
    || typeof row.closed_at !== "string" || !Number.isFinite(Date.parse(row.closed_at)) || row.observation_only !== true
    || !["github_followthrough_v1", "engineering.repo-publication.v1"].includes(String(row.native_kind))) {
    throw new Error("Invalid GitHub capacity closure receipt");
  }
  return { closure_id: row.closure_id, artifact_id: row.artifact_id,
    artifact_sha256: row.artifact_sha256, closed_at: row.closed_at,
    native_kind: row.native_kind as GitHubCapacityClosure["native_kind"], observation_only: true };
}

/** Persist the exact finite request before sending; loading never sends it. */
export function githubCapacityClosePending(scope: string, candidate: Record<string, unknown>) {
  const key = `seraph:github-capacity-close:v1:${scope}`;
  const existing = sessionStorage.getItem(key);
  if (existing !== null && existing.length > 2048) throw new Error("Retained capacity-close request exceeds its finite bound");
  const body: unknown = existing === null ? candidate : JSON.parse(existing);
  if (!body || typeof body !== "object" || Array.isArray(body)) throw new Error("Invalid retained capacity-close request");
  const row = body as Record<string, unknown>;
  const positive = (value: unknown) => typeof value === "number" && Number.isSafeInteger(value) && value > 0;
  const keys = ["acknowledged_capacity_close", "expected_job_revision", "expected_connection_revision", "expected_connection_fence", "idempotency_key", "remote_id", "remote_commit_id", "pr_number"];
  if (Object.keys(row).some(name => !keys.includes(name)) || row.acknowledged_capacity_close !== true
    || ![row.expected_job_revision, row.expected_connection_revision, row.expected_connection_fence].every(positive)
    || typeof row.idempotency_key !== "string" || !/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(row.idempotency_key)
    || (row.remote_id !== undefined && !positive(row.remote_id)) || (row.pr_number !== undefined && !positive(row.pr_number))
    || (row.remote_commit_id !== undefined && (typeof row.remote_commit_id !== "string" || !/^[a-f0-9]{40}$/.test(row.remote_commit_id)))
    || (row.remote_id !== undefined && (row.remote_commit_id !== undefined || row.pr_number !== undefined))) {
    throw new Error("Invalid retained capacity-close request");
  }
  const serialized = JSON.stringify(row);
  if (existing === null) sessionStorage.setItem(key, serialized);
  if (sessionStorage.getItem(key) !== (existing ?? serialized)) throw new Error("Capacity-close request could not be retained");
  return { body: row, clear: () => {
    if (sessionStorage.getItem(key) !== (existing ?? serialized)) throw new Error("Retained close request changed");
    sessionStorage.removeItem(key);
  } };
}

export function githubCapacityCloseStored(scope: string) {
  if (sessionStorage.getItem(`seraph:github-capacity-close:v1:${scope}`) === null) return null;
  return githubCapacityClosePending(scope, {});
}

export interface GitHubCapacityCloseInspection {
  state: "applied" | "permanently_stale_not_applied" | "inconclusive";
  job_id: string;
  job_revision: number;
  request: Record<string, unknown>;
  request_digest: string;
  closure: GitHubCapacityClosure | null;
}

function exactBody(left: Record<string, unknown>, right: Record<string, unknown>) {
  const keys = Object.keys(left).sort();
  return JSON.stringify(keys) === JSON.stringify(Object.keys(right).sort())
    && keys.every(key => left[key] === right[key]);
}

/** A canonical inspection proves absence only when its exact CAS is stale forever. */
export function githubCapacityCloseInspection(value: unknown, jobId: string, body: Record<string, unknown>): GitHubCapacityCloseInspection {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Pending close inspection unavailable");
  const row = value as Record<string, unknown>;
  if (!["applied", "permanently_stale_not_applied", "inconclusive"].includes(String(row.state))
    || row.job_id !== jobId || typeof row.job_revision !== "number" || !Number.isSafeInteger(row.job_revision) || row.job_revision < 1
    || typeof row.request_digest !== "string" || !/^[a-f0-9]{64}$/.test(row.request_digest)
    || !row.request || typeof row.request !== "object" || Array.isArray(row.request)
    || !exactBody(row.request as Record<string, unknown>, body)) throw new Error("Pending close inspection binding changed");
  const closure = githubCapacityClosure(row.closure);
  if ((row.state === "applied") !== Boolean(closure)) throw new Error("Pending close inspection receipt mismatch");
  return { state: row.state as GitHubCapacityCloseInspection["state"], job_id: jobId, job_revision: row.job_revision,
    request: row.request as Record<string, unknown>, request_digest: row.request_digest, closure };
}
