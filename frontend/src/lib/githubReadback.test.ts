import { describe, expect, it } from "vitest";
import { githubCapacityClosePending, githubCapacityCloseStored, githubCapacityCloseInspection, githubCapacityClosure, githubReadbackRequest } from "./githubReadback";

describe("legacy GitHub exact readback request", () => {
  it("requires fresh explicit acknowledgment and canonical revision", () => {
    expect(() => githubReadbackRequest(false, 3, 7)).toThrow();
    for (const revision of [0, -1, "3", NaN, Infinity]) expect(() => githubReadbackRequest(true, revision, 7)).toThrow();
    expect(githubReadbackRequest(true, 3, 7)).toEqual({ acknowledged_readback: true, expected_connection_revision: 3, remote_id: 7 });
    expect(githubReadbackRequest(true, 4)).toEqual({ acknowledged_readback: true, expected_connection_revision: 4 });
  });
});

describe("finite GitHub capacity closure", () => {
  const body = { acknowledged_capacity_close: true, expected_job_revision: 9, expected_connection_revision: 4,
    expected_connection_fence: 2, idempotency_key: "12345678-1234-1234-1234-123456789abc", remote_id: 7 };
  it("retains exact pending bytes across revision changes until confirmed", () => {
    sessionStorage.clear();
    const first = githubCapacityClosePending("owner:root:job:legacy", body);
    const retry = githubCapacityClosePending("owner:root:job:legacy", { ...body, expected_job_revision: 10, remote_id: 9 });
    expect(retry.body).toEqual(first.body);
    expect(githubCapacityClosePending("owner:other-root:job:legacy", { ...body, expected_job_revision: 10 }).body.expected_job_revision).toBe(10);
    retry.clear();
    expect(githubCapacityClosePending("owner:root:job:legacy", { ...body, expected_job_revision: 11 }).body.expected_job_revision).toBe(11);
    sessionStorage.clear();
  });
  it("rejects caller proof, coerced acknowledgment and unsafe CAS identities", () => {
    for (const value of [{ ...body, acknowledged_capacity_close: 1 }, { ...body, proof: true },
      { ...body, expected_connection_fence: "2" }, { ...body, expected_job_revision: Infinity },
      { ...body, remote_commit_id: "a".repeat(40) }]) {
      sessionStorage.clear();
      expect(() => githubCapacityClosePending("invalid", value)).toThrow();
    }
  });
  it("rejects oversized retained bytes before parsing or replacing them", () => {
    sessionStorage.clear();
    const key = "seraph:github-capacity-close:v1:oversized";
    const original = "{" + "a".repeat(2048);
    sessionStorage.setItem(key, original);
    expect(() => githubCapacityClosePending("oversized", body)).toThrow(/finite bound/);
    expect(sessionStorage.getItem(key)).toBe(original);
    sessionStorage.clear();
  });
  it("projects only the durable closed identity without changing effect truth", () => {
    expect(githubCapacityClosure(null)).toBeNull();
    expect(() => githubCapacityClosure({ observation_only: true })).toThrow();
    const closed = { closure_id: "close-a", artifact_id: "artifact-a", artifact_sha256: "a".repeat(64),
      closed_at: "2026-10-03T00:00:00Z", native_kind: "github_followthrough_v1", observation_only: true };
    expect(githubCapacityClosure(closed)).toEqual(closed);
    expect(() => githubCapacityClosure({ ...closed, observation_only: false })).toThrow();
  });
  it("requires exact inspection echo and protects changed retained bytes from an old clear", () => {
    sessionStorage.clear();
    expect(githubCapacityCloseStored("inspect")).toBeNull();
    const pending = githubCapacityClosePending("inspect", body);
    const result = { state: "permanently_stale_not_applied", job_id: "job", job_revision: 10,
      request: body, request_digest: "a".repeat(64), closure: null };
    expect(githubCapacityCloseInspection(result, "job", body).state).toBe(result.state);
    for (const changed of [{ ...result, job_id: "other" }, { ...result, request: { ...body, remote_id: 8 } },
      { ...result, state: "applied" }, { ...result, request_digest: "bad" }]) {
      expect(() => githubCapacityCloseInspection(changed, "job", body)).toThrow();
    }
    const key = "seraph:github-capacity-close:v1:inspect";
    const replacement = JSON.stringify({ ...body, idempotency_key: "87654321-1234-1234-1234-123456789abc" });
    sessionStorage.setItem(key, replacement);
    expect(() => pending.clear()).toThrow(/changed/);
    expect(sessionStorage.getItem(key)).toBe(replacement);
    sessionStorage.clear();
  });
});
