import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../game/EventBus", () => ({
  EventBus: {
    emit: vi.fn(),
  },
}));

import { CockpitView } from "./CockpitView";
import { useChatStore } from "../../stores/chatStore";
import { useQuestStore } from "../../stores/questStore";

function mockResponse(data: unknown, ok = true) {
  return {
    ok,
    json: async () => data,
  };
}

describe("CockpitView", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("localStorage", {
      getItem: vi.fn(() => null),
      setItem: vi.fn(),
      removeItem: vi.fn(),
    });
    useChatStore.setState({
      messages: [],
      sessionId: "session-1",
      sessions: [{ id: "session-1", title: "Session 1", created_at: "", updated_at: "", last_message: null, last_message_role: null }],
      connectionStatus: "connected",
      isAgentBusy: false,
      ambientState: "idle",
      ambientTooltip: "",
      onboardingCompleted: true,
    });
    useQuestStore.setState({
      goalTree: [],
      dashboard: { domains: {}, active_count: 0, completed_count: 0, total_count: 0 },
      loading: false,
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("surfaces workflow runs in the cockpit inspector", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/sessions")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/tree")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/dashboard")) {
        return Promise.resolve(mockResponse({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }));
      }
      if (url.includes("/api/observer/state")) return Promise.resolve(mockResponse({}));
      if (url.includes("/api/approvals/pending")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/workflows")) {
        return Promise.resolve(
          mockResponse({
            workflows: [
              {
                name: "summarize-file",
                tool_name: "workflow_summarize_file",
                description: "Summarize an existing workspace file",
                inputs: {
                  file_path: { type: "string", description: "Workspace file", required: true },
                },
                requires_tools: ["read_file"],
                requires_skills: [],
                user_invocable: true,
                enabled: true,
                step_count: 1,
                file_path: "defaults/workflows/summarize-file.json",
                policy_modes: ["balanced", "full"],
                execution_boundaries: ["workspace"],
                risk_level: "low",
                requires_approval: false,
                approval_behavior: "direct",
                is_available: true,
                missing_tools: [],
                missing_skills: [],
              },
            ],
          }),
        );
      }
      if (url.includes("/api/observer/continuity")) {
        return Promise.resolve(
          mockResponse({
            daemon: { connected: false, pending_notification_count: 1, capture_mode: "balanced" },
            notifications: [],
            queued_insights: [
              {
                id: "queue-1",
                intervention_id: "intervention-1",
                content_excerpt: "Hold this until the browser is back.",
                intervention_type: "advisory",
                urgency: 3,
                reasoning: "high_interruption_cost",
                created_at: "2026-03-18T12:00:00Z",
              },
            ],
            queued_insight_count: 1,
            recent_interventions: [
              {
                id: "intervention-1",
                session_id: "session-1",
                intervention_type: "advisory",
                content_excerpt: "Hold this until the browser is back.",
                policy_action: "bundle",
                policy_reason: "high_interruption_cost",
                delivery_decision: "queue",
                latest_outcome: "queued",
                transport: null,
                notification_id: null,
                feedback_type: null,
                updated_at: "2026-03-18T12:02:00Z",
                continuity_surface: "bundle_queue",
              },
            ],
          }),
        );
      }
      if (url.includes("/api/audit/events")) {
        return Promise.resolve(
          mockResponse([
            {
              id: "evt-file",
              session_id: "session-1",
              event_type: "tool_result",
              tool_name: "write_file",
              risk_level: "medium",
              policy_mode: "balanced",
              summary: "write_file returned output (30 chars)",
              details: { arguments: { file_path: "notes/brief.md" } },
              created_at: "2026-03-18T12:01:30Z",
            },
            {
              id: "evt-call",
              session_id: "session-1",
              event_type: "tool_call",
              tool_name: "workflow_web_brief_to_file",
              risk_level: "medium",
              policy_mode: "balanced",
              summary: "Calling workflow",
              details: { arguments: { query: "seraph", file_path: "notes/brief.md" } },
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
          ]),
        );
      }
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={() => {}} />);

    await waitFor(() => expect(screen.getByText("Workflow runs")).toBeInTheDocument());
    expect(screen.getByText("bundle 1 queued")).toBeInTheDocument();
    expect(screen.getByText("Hold this until the browser is back.")).toBeInTheDocument();
    fireEvent.click(screen.getAllByText("workflow_web_brief_to_file succeeded (2 steps)")[0]);

    expect(screen.getByText("Draft Rerun")).toBeInTheDocument();
    expect(screen.getByText("Use Output")).toBeInTheDocument();
    const runButton = screen.getByText("Run summarize-file");
    expect(runButton).toBeInTheDocument();
    fireEvent.click(runButton);
    expect(screen.getByDisplayValue(/Run workflow "summarize-file" with file_path="notes\/brief.md"\./)).toBeInTheDocument();
    expect(screen.getAllByText("web-brief-to-file").length).toBeGreaterThan(0);
  });
});
