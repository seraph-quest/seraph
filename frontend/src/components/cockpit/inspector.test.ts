import { describe, expect, it } from "vitest";

import {
  collectArtifacts,
  collectWorkflowRuns,
  formatInspectorValue,
  resolveWorkBoardArtifact,
  type ArtifactRecord,
  type CockpitAuditEvent,
} from "./inspector";

describe("collectArtifacts", () => {
  it("collects recent file outputs from filesystem write integrations", () => {
    const events: CockpitAuditEvent[] = [
      {
        id: "evt-1",
        session_id: "session-1",
        event_type: "integration_succeeded",
        tool_name: "filesystem:workspace",
        risk_level: "low",
        policy_mode: "full",
        summary: "Filesystem workspace succeeded",
        details: {
          operation: "write",
          file_path: "notes/today.md",
        },
        created_at: "2026-03-18T12:00:00Z",
      },
    ];

    expect(collectArtifacts(events)).toEqual([
      {
        id: "evt-1:filesystem",
        source: "filesystem write",
        filePath: "notes/today.md",
        sessionId: "session-1",
        createdAt: "2026-03-18T12:00:00Z",
        summary: "Filesystem workspace succeeded",
      },
    ]);
  });

  it("collects write_file tool outputs and keeps the newest entry per file", () => {
    const events: CockpitAuditEvent[] = [
      {
        id: "evt-new",
        session_id: "session-1",
        event_type: "tool_result",
        tool_name: "write_file",
        risk_level: "low",
        policy_mode: "full",
        summary: "write_file returned output (20 chars)",
        details: {
          arguments: {
            file_path: "reports/plan.md",
          },
        },
        created_at: "2026-03-18T12:05:00Z",
      },
      {
        id: "evt-old",
        session_id: "session-1",
        event_type: "integration_succeeded",
        tool_name: "filesystem:workspace",
        risk_level: "low",
        policy_mode: "full",
        summary: "Filesystem workspace succeeded",
        details: {
          operation: "write",
          file_path: "reports/plan.md",
        },
        created_at: "2026-03-18T12:00:00Z",
      },
      {
        id: "evt-read",
        session_id: "session-1",
        event_type: "integration_succeeded",
        tool_name: "filesystem:workspace",
        risk_level: "low",
        policy_mode: "full",
        summary: "Filesystem workspace succeeded",
        details: {
          operation: "read",
          file_path: "reports/plan.md",
        },
        created_at: "2026-03-18T11:55:00Z",
      },
    ];

    expect(collectArtifacts(events)).toEqual([
      {
        id: "evt-new:tool",
        source: "write_file tool",
        filePath: "reports/plan.md",
        sessionId: "session-1",
        createdAt: "2026-03-18T12:05:00Z",
        summary: "write_file returned output (20 chars)",
      },
    ]);
  });
});

describe("formatInspectorValue", () => {
  it("formats nullish and structured values for the inspector", () => {
    expect(formatInspectorValue(null)).toBe("n/a");
    expect(formatInspectorValue(undefined)).toBe("n/a");
    expect(formatInspectorValue("plain")).toBe("plain");
    expect(formatInspectorValue({ tool: "write_file", ok: true })).toBe(
      '{\n  "tool": "write_file",\n  "ok": true\n}',
    );
  });
});

describe("collectWorkflowRuns", () => {
  it("groups workflow call and result events into linked workflow runs", () => {
    const events: CockpitAuditEvent[] = [
      {
        id: "evt-file",
        session_id: "session-1",
        event_type: "tool_result",
        tool_name: "write_file",
        risk_level: "medium",
        policy_mode: "balanced",
        summary: "write_file returned output (30 chars)",
        details: {
          arguments: {
            file_path: "notes/brief.md",
          },
        },
        created_at: "2026-03-18T12:01:30Z",
      },
      {
        id: "evt-call",
        session_id: "session-1",
        event_type: "tool_call",
        tool_name: "workflow_web_brief_to_file",
        risk_level: "medium",
        policy_mode: "balanced",
        summary: 'Calling tool: workflow_web_brief_to_file({"query":"seraph","file_path":"notes/brief.md"})',
        details: {
          arguments: {
            query: "seraph",
            file_path: "notes/brief.md",
          },
        },
        created_at: "2026-03-18T12:01:00Z",
      },
      {
        id: "evt-result",
        session_id: "session-1",
        event_type: "tool_result",
        tool_name: "workflow_web_brief_to_file",
        risk_level: "medium",
        policy_mode: "balanced",
        summary: "workflow_web_brief_to_file succeeded (2 steps)",
        details: {
          workflow_name: "web-brief-to-file",
          step_tools: ["web_search", "write_file"],
          artifact_paths: ["notes/brief.md"],
          continued_error_steps: [],
        },
        created_at: "2026-03-18T12:01:45Z",
      },
    ];

    expect(collectWorkflowRuns(events)).toEqual([
      {
        id: "evt-call",
        toolName: "workflow_web_brief_to_file",
        workflowName: "web-brief-to-file",
        sessionId: "session-1",
        status: "succeeded",
        startedAt: "2026-03-18T12:01:00Z",
        updatedAt: "2026-03-18T12:01:45Z",
        summary: "workflow_web_brief_to_file succeeded (2 steps)",
        stepTools: ["web_search", "write_file"],
        artifactPaths: ["notes/brief.md"],
        continuedErrorSteps: [],
        arguments: {
          query: "seraph",
          file_path: "notes/brief.md",
        },
        artifacts: [
          {
            id: "evt-file:tool",
            source: "write_file tool",
            filePath: "notes/brief.md",
            sessionId: "session-1",
            createdAt: "2026-03-18T12:01:30Z",
            summary: "write_file returned output (30 chars)",
          },
        ],
      },
    ]);
  });

  it("uses workflow artifact registry receipts when audit writes are absent", () => {
    const events: CockpitAuditEvent[] = [
      {
        id: "evt-call",
        session_id: "session-1",
        event_type: "tool_call",
        tool_name: "workflow_web_brief_to_file",
        risk_level: "medium",
        policy_mode: "balanced",
        summary: "Calling workflow",
        details: {
          arguments: {
            query: "seraph",
          },
        },
        created_at: "2026-03-18T12:01:00Z",
      },
      {
        id: "evt-result",
        session_id: "session-1",
        event_type: "tool_result",
        tool_name: "workflow_web_brief_to_file",
        risk_level: "medium",
        policy_mode: "balanced",
        summary: "workflow_web_brief_to_file succeeded (2 steps)",
        details: {
          workflow_name: "web-brief-to-file",
          step_tools: ["web_search", "write_file"],
          artifact_registry: [
            {
              artifact_id: "art_123",
              artifact_type: "workflow_output",
              file_path: "notes/brief.md",
              producer: "workflow:web-brief-to-file",
              run_id: "session-1:workflow_web_brief_to_file:web-brief",
              session_id: "session-1",
              content_sha256: "abc123",
              size_bytes: 42,
              trust_boundary: { status: "stable" },
              recovery_hint: "Replay before replacing.",
            },
          ],
          continued_error_steps: [],
        },
        created_at: "2026-03-18T12:01:45Z",
      },
    ];

    const [run] = collectWorkflowRuns(events);

    expect(run.artifactPaths).toEqual(["notes/brief.md"]);
    expect(run.artifacts).toEqual([
      {
        id: "art_123",
        source: "artifact registry: workflow:web-brief-to-file",
        filePath: "notes/brief.md",
        sessionId: "session-1",
        createdAt: "2026-03-18T12:01:45Z",
        summary: "workflow_web_brief_to_file succeeded (2 steps)",
        artifactType: "workflow_output",
        producer: "workflow:web-brief-to-file",
        runId: "session-1:workflow_web_brief_to_file:web-brief",
        contentSha256: "abc123",
        sizeBytes: 42,
        trustBoundary: { status: "stable" },
        recoveryHint: "Replay before replacing.",
      },
    ]);
  });

  it("matches repeated workflow runs in arrival order", () => {
    const events: CockpitAuditEvent[] = [
      {
        id: "evt-call-1",
        session_id: "session-1",
        event_type: "tool_call",
        tool_name: "workflow_web_brief_to_file",
        risk_level: "medium",
        policy_mode: "balanced",
        summary: "Calling workflow run one",
        details: {
          arguments: {
            query: "first",
            file_path: "notes/first.md",
          },
        },
        created_at: "2026-03-18T12:01:00Z",
      },
      {
        id: "evt-call-2",
        session_id: "session-1",
        event_type: "tool_call",
        tool_name: "workflow_web_brief_to_file",
        risk_level: "medium",
        policy_mode: "balanced",
        summary: "Calling workflow run two",
        details: {
          arguments: {
            query: "second",
            file_path: "notes/second.md",
          },
        },
        created_at: "2026-03-18T12:01:10Z",
      },
      {
        id: "evt-file-1",
        session_id: "session-1",
        event_type: "tool_result",
        tool_name: "write_file",
        risk_level: "medium",
        policy_mode: "balanced",
        summary: "write_file returned output (20 chars)",
        details: {
          arguments: {
            file_path: "notes/first.md",
          },
        },
        created_at: "2026-03-18T12:01:15Z",
      },
      {
        id: "evt-file-2",
        session_id: "session-1",
        event_type: "tool_result",
        tool_name: "write_file",
        risk_level: "medium",
        policy_mode: "balanced",
        summary: "write_file returned output (20 chars)",
        details: {
          arguments: {
            file_path: "notes/second.md",
          },
        },
        created_at: "2026-03-18T12:01:16Z",
      },
      {
        id: "evt-result-1",
        session_id: "session-1",
        event_type: "tool_result",
        tool_name: "workflow_web_brief_to_file",
        risk_level: "medium",
        policy_mode: "balanced",
        summary: "workflow_web_brief_to_file succeeded (2 steps)",
        details: {
          workflow_name: "web-brief-to-file",
          step_tools: ["web_search", "write_file"],
          artifact_paths: ["notes/first.md"],
          continued_error_steps: [],
        },
        created_at: "2026-03-18T12:01:20Z",
      },
      {
        id: "evt-result-2",
        session_id: "session-1",
        event_type: "tool_result",
        tool_name: "workflow_web_brief_to_file",
        risk_level: "medium",
        policy_mode: "balanced",
        summary: "workflow_web_brief_to_file succeeded (2 steps)",
        details: {
          workflow_name: "web-brief-to-file",
          step_tools: ["web_search", "write_file"],
          artifact_paths: ["notes/second.md"],
          continued_error_steps: [],
        },
        created_at: "2026-03-18T12:01:30Z",
      },
    ];

    const runs = collectWorkflowRuns(events);

    expect(runs).toHaveLength(2);
    const firstRun = runs.find((run) => run.id === "evt-call-1");
    const secondRun = runs.find((run) => run.id === "evt-call-2");

    expect(firstRun).toBeDefined();
    expect(firstRun?.arguments).toEqual({ query: "first", file_path: "notes/first.md" });
    expect(firstRun?.artifactPaths).toEqual(["notes/first.md"]);
    expect(firstRun?.artifacts.map((artifact) => artifact.filePath)).toEqual(["notes/first.md"]);
    expect(secondRun).toBeDefined();
    expect(secondRun?.arguments).toEqual({ query: "second", file_path: "notes/second.md" });
    expect(secondRun?.artifactPaths).toEqual(["notes/second.md"]);
    expect(secondRun?.artifacts.map((artifact) => artifact.filePath)).toEqual(["notes/second.md"]);
  });
});

describe("resolveWorkBoardArtifact", () => {
  const digestA = "a".repeat(64);
  const digestB = "b".repeat(64);

  function artifact(overrides: Partial<ArtifactRecord> = {}): ArtifactRecord {
    return {
      id: "artifact-a",
      source: "artifact registry",
      filePath: "artifacts/result.md",
      sessionId: "session-a",
      createdAt: "2026-09-24T08:00:00Z",
      summary: "safe artifact summary",
      runId: "run-a",
      contentSha256: digestA,
      ...overrides,
    };
  }

  it("does not select a same-path artifact from another session", () => {
    const foreign = artifact({ sessionId: "session-foreign", runId: "run-foreign" });
    const local = artifact({ id: "artifact-b", sessionId: "session-b", runId: "run-b", contentSha256: digestB });

    expect(resolveWorkBoardArtifact(
      [foreign, local],
      { file_path: "artifacts/result.md", content_sha256: digestB },
      { ownerSessionId: "session-b", workflowRunId: null },
    )).toBe(local);
  });

  it("requires the immutable owning run and matching digest for run-scoped path readback", () => {
    const otherRun = artifact({ id: "same-path-other-run", runId: "run-other" });
    const owningRun = artifact({ id: "same-path-owning-run", runId: "run-owned" });

    expect(resolveWorkBoardArtifact(
      [otherRun, owningRun],
      { file_path: "artifacts/result.md", content_sha256: digestA },
      { ownerSessionId: "session-a", workflowRunId: "run-owned" },
    )).toBe(owningRun);
    expect(resolveWorkBoardArtifact(
      [owningRun],
      { file_path: "artifacts/result.md", content_sha256: digestB },
      { ownerSessionId: "session-a", workflowRunId: "run-owned" },
    )).toBeNull();
  });

  it("requires session scope when a receipt has no workflow run", () => {
    const value = artifact();
    expect(resolveWorkBoardArtifact(
      [value],
      { artifact_id: value.id, content_sha256: digestA },
      { ownerSessionId: null, workflowRunId: null },
    )).toBeNull();
  });

  it("does not resolve an unbound path when the receipt has no digest", () => {
    expect(resolveWorkBoardArtifact(
      [artifact()],
      { file_path: "artifacts/result.md" },
      { ownerSessionId: "session-a", workflowRunId: null },
    )).toBeNull();
  });
});
