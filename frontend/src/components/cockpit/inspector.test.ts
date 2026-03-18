import { describe, expect, it } from "vitest";

import {
  collectArtifacts,
  collectWorkflowRuns,
  formatInspectorValue,
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
});
