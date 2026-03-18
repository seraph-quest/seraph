import { describe, expect, it } from "vitest";

import { collectArtifacts, formatInspectorValue, type CockpitAuditEvent } from "./inspector";

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
