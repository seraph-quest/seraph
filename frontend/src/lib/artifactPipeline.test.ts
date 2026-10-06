import { beforeEach, describe, expect, it } from "vitest";
import { pipelineStorageKey, readPipelineStorage, writePipelineStorage, validatePipeline } from "./artifactPipeline";
import { PIPELINE_RECOVERY_REASONS } from "./guardianInbox";

const key = pipelineStorageKey("operator:one", "session-one", "task-one");
const pending = { path: "/api/work-board/tasks/task-one/pipeline-preview", body: {
  expected_revision: 3, source_input_artifact_id: "artifact-one", idempotency_key: "request-one" } };

describe("exact retained pipeline mutation", () => {
  beforeEach(() => window.sessionStorage.clear());
  it("retains exact bounded request before reload without a new request key", () => {
    const value = { schema_version: 1 as const, operation_id: null, pending };
    writePipelineStorage(key, "task-one", value);
    expect(readPipelineStorage(key, "task-one")).toEqual(value);
    expect(readPipelineStorage(pipelineStorageKey("operator:two", "session-one", "task-one"), "task-one").pending).toBeNull();
    expect(readPipelineStorage(pipelineStorageKey("operator:one", "session-two", "task-one"), "task-one").pending).toBeNull();
  });
  it.each(["/api/work-board/pipelines/other/accept", "/api/work-board/tasks/other/pipeline-preview", "https://attacker.example/post", "/api/work-board/pipelines/operation/anything"])("rejects a path outside the exact finite operation: %s", (path) => {
    window.sessionStorage.setItem(key, JSON.stringify({ schema_version: 1, operation_id: "operation", pending: { ...pending, path } }));
    expect(() => readPipelineStorage(key, "task-one")).toThrow();
  });
  it("fails closed on corrupt, untyped, oversized and authority-bearing retained bodies", () => {
    for (const body of [{ ...pending.body, expected_revision: "3" }, { ...pending.body, source_input_artifact_id: 123 }, { ...pending.body, root: "/other-root" }, { ...pending.body, idempotency_key: "x".repeat(17000) }]) {
      window.sessionStorage.setItem(key, JSON.stringify({ schema_version: 1, operation_id: null, pending: { ...pending, body } }));
      expect(() => readPipelineStorage(key, "task-one")).toThrow();
    }
    window.sessionStorage.setItem(key, "{");
    expect(() => readPipelineStorage(key, "task-one")).toThrow();
  });
  it("requires the exact fixed capabilities on operation readback", () => {
    expect(() => validatePipeline({ operation_id: "operation", revision: 1, parent_revision: 1, plan_version: 1,
      digest: "a".repeat(64), status: "proposed", no_learning: true, source_scope: { start_url: "https://example.com/", allowed_hosts: ["example.com"], approved_url_prefixes: ["https://example.com/"] },
      steps: [{ capability_id: "work.arbitrary-code.v1", task_id: "task-one", task_revision: 1, status: "todo" }] })).toThrow();
  });
  it("accepts only the closed read-only recovery codes without requiring opportunity preview on generic GET", () => {
    const operation = { operation_id: "operation", revision: 1, parent_revision: 1, plan_version: 1,
      digest: "a".repeat(64), status: "proposed", no_learning: true, source_scope: { start_url: "https://example.com/", allowed_hosts: ["example.com"], approved_url_prefixes: ["https://example.com/"] },
      steps: [{ slot: "public_source", capability_id: "browser.public-task.v1", task_id: "task-one", task_revision: 1, status: "triage" }] };
    for (const recovery_reason of PIPELINE_RECOVERY_REASONS) expect(validatePipeline({ ...operation, recovery_reason }).recovery_reason).toBe(recovery_reason);
    expect(() => validatePipeline({ ...operation, recovery_reason: "private_path:/vault/secret" })).toThrow(/linkage/);
    expect(() => validatePipeline({ ...operation, opportunity_id: "opportunity-1", opportunity_revision: -1 })).toThrow(/linkage/);
  });
});
