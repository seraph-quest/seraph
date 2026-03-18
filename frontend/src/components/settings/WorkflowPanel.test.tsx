import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { WorkflowPanel } from "./WorkflowPanel";

function mockResponse(data: unknown, ok = true) {
  return {
    ok,
    json: async () => data,
  };
}

describe("WorkflowPanel", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("hides the draft button when a workflow is unavailable under the current surface", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({
        workflows: [
          {
            name: "web-brief-to-file",
            tool_name: "workflow_web_brief_to_file",
            description: "Search and save",
            inputs: {},
            requires_tools: ["web_search", "write_file"],
            requires_skills: [],
            user_invocable: true,
            enabled: true,
            step_count: 2,
            file_path: "/tmp/web-brief.md",
            policy_modes: ["balanced", "full"],
            execution_boundaries: ["external_read", "workspace_write"],
            risk_level: "medium",
            requires_approval: false,
            approval_behavior: "never",
            is_available: false,
            missing_tools: ["write_file"],
            missing_skills: [],
          },
        ],
      }),
    );

    render(<WorkflowPanel />);

    await waitFor(() => expect(screen.getByText("web-brief-to-file")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "draft" })).not.toBeInTheDocument();
    expect(screen.getByText(/unavailable now/i)).toBeInTheDocument();
    expect(screen.getByText(/missing tools: write_file/i)).toBeInTheDocument();
  });

  it("shows the draft button when a workflow is enabled and available", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({
        workflows: [
          {
            name: "goal-snapshot-to-file",
            tool_name: "workflow_goal_snapshot_to_file",
            description: "Export goals",
            inputs: {},
            requires_tools: ["get_goals", "write_file"],
            requires_skills: ["goal-reflection"],
            user_invocable: true,
            enabled: true,
            step_count: 2,
            file_path: "/tmp/goal-snapshot.md",
            policy_modes: ["balanced", "full"],
            execution_boundaries: ["guardian_state_read", "workspace_write"],
            risk_level: "medium",
            requires_approval: false,
            approval_behavior: "never",
            is_available: true,
            missing_tools: [],
            missing_skills: [],
          },
        ],
      }),
    );

    render(<WorkflowPanel />);

    await waitFor(() => expect(screen.getByText("goal-snapshot-to-file")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "draft" })).toBeInTheDocument();
  });
});
