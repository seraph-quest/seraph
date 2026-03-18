import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
              {
                name: "web-brief-to-file",
                tool_name: "workflow_web_brief_to_file",
                description: "Search and save a brief",
                inputs: {
                  query: { type: "string", description: "Search query", required: true },
                  file_path: { type: "string", description: "Workspace file", required: true },
                },
                requires_tools: ["web_search", "write_file"],
                requires_skills: [],
                user_invocable: true,
                enabled: true,
                step_count: 2,
                file_path: "defaults/workflows/web-brief-to-file.json",
                policy_modes: ["balanced", "full"],
                execution_boundaries: ["workspace", "network"],
                risk_level: "medium",
                requires_approval: false,
                approval_behavior: "direct",
                is_available: false,
                missing_tools: ["write_file"],
                missing_skills: [],
              },
            ],
          }),
        );
      }
      if (url.includes("/api/skills/reload")) return Promise.resolve(mockResponse({ status: "reloaded" }));
      if (url.includes("/api/workflows/reload")) return Promise.resolve(mockResponse({ status: "reloaded" }));
      if (url.includes("/api/skills/goal-reflection")) {
        return Promise.resolve(mockResponse({ status: "updated" }));
      }
      if (url.includes("/api/skills")) {
        return Promise.resolve(
          mockResponse({
            skills: [
              {
                name: "goal-reflection",
                enabled: true,
                description: "Reflect on goals",
                requires_tools: ["reflect_goal"],
                user_invocable: true,
              },
              {
                name: "calendar-planning",
                enabled: false,
                description: "Plan from calendar",
                requires_tools: ["calendar_events"],
                user_invocable: false,
              },
            ],
          }),
        );
      }
      if (url.includes("/api/mcp/servers/browser/test")) {
        return Promise.resolve(mockResponse({ status: "ok", tool_count: 4 }));
      }
      if (url.includes("/api/mcp/servers")) {
        return Promise.resolve(
          mockResponse({
            servers: [
              {
                name: "browser",
                enabled: true,
                url: "http://localhost:9001/mcp",
                status: "connected",
                tool_count: 4,
                connected: true,
              },
              {
                name: "vault",
                enabled: false,
                url: "http://localhost:9002/mcp",
                status: "auth_required",
                status_message: "Missing token",
                has_headers: true,
              },
            ],
          }),
        );
      }
      if (url.includes("/api/settings/tool-policy-mode")) return Promise.resolve(mockResponse({ mode: "balanced" }));
      if (url.includes("/api/settings/mcp-policy-mode")) return Promise.resolve(mockResponse({ mode: "approval" }));
      if (url.includes("/api/settings/approval-mode")) return Promise.resolve(mockResponse({ mode: "high_risk" }));
      if (url.includes("/api/tools")) {
        return Promise.resolve(
          mockResponse([
            { name: "read_file", risk_level: "low", execution_boundaries: ["workspace"] },
            { name: "shell_execute", risk_level: "high", execution_boundaries: ["workspace"] },
            { name: "mcp_browser_search", risk_level: "medium", execution_boundaries: ["external_mcp"] },
          ]),
        );
      }
      if (url.includes("/api/observer/continuity")) {
        return Promise.resolve(
          mockResponse({
            daemon: { connected: false, pending_notification_count: 1, capture_mode: "balanced" },
            notifications: [
              {
                id: "note-1",
                intervention_id: "intervention-1",
                title: "Guardian nudge",
                body: "Stand up and reset before the next block.",
                intervention_type: "advisory",
                urgency: 2,
                created_at: "2026-03-18T12:03:00Z",
              },
            ],
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
    expect(screen.getByText("Desktop shell")).toBeInTheDocument();
    expect(screen.getByText("Operator surface")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Set tool policy to balanced" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Set MCP policy to approval" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Set approval mode to high_risk" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("browser")).toBeInTheDocument();
    expect(screen.getByText("4 tools live")).toBeInTheDocument();
    expect(screen.getByText("vault")).toBeInTheDocument();
    expect(screen.getByText("auth required")).toBeInTheDocument();
    expect(screen.getByText("goal-reflection")).toBeInTheDocument();
    expect(screen.getByText("invocable · reflect_goal")).toBeInTheDocument();
    expect(screen.getByText("calendar-planning")).toBeInTheDocument();
    expect(screen.getByText("runtime · calendar_events")).toBeInTheDocument();
    expect(screen.getByText("invocable 1/2 available")).toBeInTheDocument();
    expect(screen.getByText("approval 0 · blocked 1")).toBeInTheDocument();
    expect(screen.getByText("blocked web-brief-to-file · tools write_file")).toBeInTheDocument();
    expect(screen.getByText("bundle 1 queued")).toBeInTheDocument();
    expect(screen.getByText("Guardian nudge")).toBeInTheDocument();
    expect(screen.getAllByText("Hold this until the browser is back.").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "Test browser" }));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/mcp/servers/browser/test"),
        expect.objectContaining({ method: "POST" }),
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "Turn off goal-reflection" }));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/skills/goal-reflection"),
        expect.objectContaining({ method: "PUT" }),
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "Set tool policy to full" }));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/settings/tool-policy-mode"),
        expect.objectContaining({ method: "PUT" }),
      ),
    );
    fireEvent.click(screen.getAllByText("Draft Follow-up")[0]);
    expect(screen.getByDisplayValue(/Follow up on this desktop alert:/)).toBeInTheDocument();
    fireEvent.click(screen.getAllByText("workflow_web_brief_to_file succeeded (2 steps)")[0]);

    expect(screen.getByText("Draft Rerun")).toBeInTheDocument();
    expect(screen.getByText("Use Output")).toBeInTheDocument();
    const runButton = screen.getByText("Run summarize-file");
    expect(runButton).toBeInTheDocument();
    fireEvent.click(runButton);
    expect(screen.getByDisplayValue(/Run workflow "summarize-file" with file_path="notes\/brief.md"\./)).toBeInTheDocument();
    fireEvent.click(screen.getByText("Dismiss"));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/observer/notifications/note-1/dismiss"),
        expect.objectContaining({ method: "POST" }),
      ),
    );
    fireEvent.click(screen.getAllByRole("button", { name: /reload/i })[0]);
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/skills/reload"),
        expect.objectContaining({ method: "POST" }),
      ),
    );
    expect(screen.getAllByText("web-brief-to-file").length).toBeGreaterThan(0);
  }, 15000);

  it("does not process refresh payloads after the cockpit unmounts", async () => {
    const deferredResponses = Array.from({ length: 11 }, () => {
      let resolve!: (value: { ok: boolean; json: () => Promise<unknown> }) => void;
      const promise = new Promise<{ ok: boolean; json: () => Promise<unknown> }>((res) => {
        resolve = res;
      });
      return { promise, resolve };
    });
    const jsonSpies = deferredResponses.map(() => vi.fn(async () => ({})));
    let cockpitFetchCount = 0;

    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/sessions")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/tree")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/dashboard")) {
        return Promise.resolve(mockResponse({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }));
      }
      const next = deferredResponses[cockpitFetchCount];
      cockpitFetchCount += 1;
      return next.promise;
    });

    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    const view = render(<CockpitView onSend={vi.fn()} />);

    await waitFor(() => expect(cockpitFetchCount).toBe(11));
    view.unmount();

    await act(async () => {
      deferredResponses.forEach((response, index) => {
        response.resolve({
          ok: true,
          json: jsonSpies[index],
        });
      });
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(jsonSpies.every((spy) => spy.mock.calls.length === 0)).toBe(true);
    expect(consoleError).not.toHaveBeenCalled();
  });
});
