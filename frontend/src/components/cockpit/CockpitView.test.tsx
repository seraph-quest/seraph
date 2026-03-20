import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CockpitView } from "./CockpitView";
import { useChatStore } from "../../stores/chatStore";
import { useQuestStore } from "../../stores/questStore";

function mockResponse(data: unknown, ok = true, status = ok ? 200 : 500) {
  return {
    ok,
    status,
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
      if (url.includes("/api/operator/timeline")) return Promise.resolve(mockResponse({ items: [] }));
      if (url.includes("/api/capabilities/overview")) {
        return Promise.resolve(
          mockResponse({
            tool_policy_mode: "balanced",
            mcp_policy_mode: "approval",
            approval_mode: "high_risk",
            summary: {
              native_tools_ready: 1,
              native_tools_total: 3,
              skills_ready: 1,
              skills_total: 2,
              workflows_ready: 1,
              workflows_total: 2,
              starter_packs_ready: 0,
              starter_packs_total: 1,
              mcp_servers_ready: 1,
              mcp_servers_total: 2,
            },
            native_tools: [
              { name: "read_file", description: "Read", risk_level: "low", execution_boundaries: ["workspace_read"], availability: "ready" },
              { name: "shell_execute", description: "Shell", risk_level: "high", execution_boundaries: ["sandbox_execution"], availability: "blocked", blocked_reason: "tool_policy_balanced" },
              { name: "mcp_browser_search", description: "Browser MCP", risk_level: "medium", execution_boundaries: ["external_mcp"], availability: "blocked", blocked_reason: "mcp_policy_approval" },
            ],
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
            skills: [
              {
                name: "goal-reflection",
                enabled: true,
                description: "Reflect on goals",
                requires_tools: ["reflect_goal"],
                user_invocable: true,
                availability: "ready",
                missing_tools: [],
              },
              {
                name: "calendar-planning",
                enabled: false,
                description: "Plan from calendar",
                requires_tools: ["calendar_events"],
                user_invocable: false,
                availability: "disabled",
                missing_tools: [],
              },
            ],
            mcp_servers: [
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
                availability: "blocked",
                blocked_reason: "auth_required",
              },
            ],
            starter_packs: [
              {
                name: "research-briefing",
                label: "Research briefing",
                description: "Enable a starter research pack.",
                sample_prompt: "Run workflow \"web-brief-to-file\" with query=\"seraph\", file_path=\"notes/brief.md\".",
                skills: ["goal-reflection"],
                workflows: ["web-brief-to-file"],
                ready_skills: ["goal-reflection"],
                ready_workflows: [],
                blocked_skills: [],
                blocked_workflows: [{ name: "web-brief-to-file", availability: "blocked", missing_tools: ["write_file"], missing_skills: [] }],
                availability: "partial",
                recommended_actions: [
                  { type: "activate_starter_pack", label: "Activate pack", name: "research-briefing" },
                  { type: "set_tool_policy", label: "Allow write_file", mode: "full" },
                ],
              },
            ],
            catalog_items: [
              {
                name: "daily-standup",
                type: "skill",
                description: "Generate a standup report",
                category: "productivity",
                bundled: true,
                installed: false,
                missing_tools: [],
                recommended_actions: [{ type: "install_catalog_item", label: "Install skill", name: "daily-standup" }],
              },
            ],
            recommendations: [
              {
                id: "starter-pack:research-briefing",
                label: "Activate Research briefing",
                description: "Enable the starter briefing pack.",
                action: { type: "activate_starter_pack", label: "Activate pack", name: "research-briefing" },
              },
            ],
            runbooks: [
              {
                id: "workflow:summarize-file",
                name: "summarize-file",
                label: "Run summarize-file",
                description: "Summarize a workspace file",
                source: "workflow",
                command: "Run workflow \"summarize-file\" with file_path=\"notes/brief.md\".",
                availability: "ready",
                blocking_reasons: [],
                recommended_actions: [],
                parameter_schema: { file_path: { type: "string", description: "Workspace file" } },
                risk_level: "low",
                execution_boundaries: ["workspace"],
                action: { type: "draft_workflow", label: "Draft workflow", name: "summarize-file" },
              },
            ],
          }),
        );
      }
      if (url.includes("/api/workflows/runs")) {
        return Promise.resolve(
          mockResponse({
            runs: [
              {
                id: "evt-call",
                tool_name: "workflow_web_brief_to_file",
                workflow_name: "web-brief-to-file",
                session_id: "session-1",
                status: "succeeded",
                started_at: "2026-03-18T12:01:00Z",
                updated_at: "2026-03-18T12:01:45Z",
                summary: "workflow_web_brief_to_file succeeded (2 steps)",
                step_tools: ["web_search", "write_file"],
                artifact_paths: ["notes/brief.md"],
                continued_error_steps: [],
                arguments: { query: "seraph", file_path: "notes/brief.md" },
                risk_level: "medium",
                execution_boundaries: ["external_read", "workspace_write"],
                accepts_secret_refs: false,
                pending_approval_count: 0,
                pending_approval_ids: [],
                thread_id: "session-1",
                thread_label: "Session 1",
                thread_source: "session",
                run_fingerprint: "fp-1",
                run_identity: "session-1:workflow_web_brief_to_file:fp-1",
                replay_allowed: true,
                replay_block_reason: null,
                replay_draft: "Run workflow \"web-brief-to-file\" with query=\"seraph\", file_path=\"notes/brief.md\".",
                retry_from_step_draft: "Retry workflow \"web-brief-to-file\" from step \"write_file\" with query=\"seraph\", file_path=\"notes/brief.md\".",
                step_records: [
                  {
                    id: "web_search",
                    index: 0,
                    tool: "web_search",
                    status: "succeeded",
                    argument_keys: ["query"],
                    artifact_paths: [],
                    result_summary: "2 web results",
                    error_kind: null,
                  },
                  {
                    id: "write_file",
                    index: 1,
                    tool: "write_file",
                    status: "degraded",
                    argument_keys: ["file_path"],
                    artifact_paths: ["notes/brief.md"],
                    result_summary: "saved fallback note",
                    error_kind: "tool_failed",
                  },
                ],
                approval_recovery_message: null,
                timeline: [
                  { kind: "workflow_started", at: "2026-03-18T12:01:00Z", summary: "Workflow started" },
                  { kind: "workflow_succeeded", at: "2026-03-18T12:01:45Z", summary: "workflow_web_brief_to_file succeeded (2 steps)" },
                ],
              },
            ],
          }),
        );
      }
      if (url.includes("/api/skills/reload")) return Promise.resolve(mockResponse({ status: "reloaded" }));
      if (url.includes("/api/workflows/reload")) return Promise.resolve(mockResponse({ status: "reloaded" }));
      if (url.includes("/api/capabilities/starter-packs/research-briefing/activate")) return Promise.resolve(mockResponse({ status: "activated" }));
      if (url.includes("/api/skills/goal-reflection")) {
        return Promise.resolve(mockResponse({ status: "updated" }));
      }
      if (url.includes("/api/mcp/servers/browser/test")) {
        return Promise.resolve(mockResponse({ status: "ok", tool_count: 4 }));
      }
      if (url.includes("/api/settings/tool-policy-mode")) return Promise.resolve(mockResponse({ mode: "balanced" }));
      if (url.includes("/api/settings/mcp-policy-mode")) return Promise.resolve(mockResponse({ mode: "approval" }));
      if (url.includes("/api/settings/approval-mode")) return Promise.resolve(mockResponse({ mode: "high_risk" }));
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
                surface: "notification",
                session_id: "session-1",
                resume_message: "Continue from this guardian intervention: Stand up and reset before the next block.",
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

    await waitFor(() => expect(screen.getByText("Workflow timeline")).toBeInTheDocument());
    expect(screen.getByText("Operator timeline")).toBeInTheDocument();
    expect(screen.getByText("Desktop shell")).toBeInTheDocument();
    expect(screen.getByText("Operator terminal")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Set tool policy to balanced" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Set MCP policy to approval" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Set approval mode to high_risk" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("Research briefing")).toBeInTheDocument();
    expect(screen.getByText("Activate Research briefing")).toBeInTheDocument();
    expect(screen.getByText("4 tools live")).toBeInTheDocument();
    expect(screen.getByText("vault")).toBeInTheDocument();
    expect(screen.getByText("auth required")).toBeInTheDocument();
    expect(screen.getByText("goal-reflection")).toBeInTheDocument();
    expect(screen.getByText("ready · reflect_goal")).toBeInTheDocument();
    expect(screen.getByText("calendar-planning")).toBeInTheDocument();
    expect(screen.getByText("disabled · calendar_events")).toBeInTheDocument();
    expect(screen.getByText("invocable 1/2 available")).toBeInTheDocument();
    expect(screen.getByText("approval 0 · blocked 1")).toBeInTheDocument();
    expect(screen.getByText("blocked web-brief-to-file")).toBeInTheDocument();
    expect(screen.getByText("tools write_file")).toBeInTheDocument();
    expect(screen.getByText("bundle 1 queued")).toBeInTheDocument();
    expect(screen.getByText("Guardian nudge")).toBeInTheDocument();
    expect(screen.getByText("Run summarize-file")).toBeInTheDocument();
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
    fireEvent.click(screen.getAllByText("Continue")[0]);
    await waitFor(() =>
      expect(screen.getByDisplayValue(/Continue from this guardian intervention:/)).toBeInTheDocument(),
    );
    fireEvent.click(screen.getAllByText("workflow_web_brief_to_file succeeded (2 steps)")[0]);

    expect(screen.getByText("Draft Boundary-Aware Rerun")).toBeInTheDocument();
    expect(screen.getByText(/web_search succeeded · 2 web results/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry step" })).toBeInTheDocument();
    expect(screen.getByText("Use Output")).toBeInTheDocument();
    const runButton = screen.getByRole("button", { name: "Run summarize-file" });
    expect(runButton).toBeInTheDocument();
    fireEvent.click(runButton);
    await waitFor(() =>
      expect(
        screen.getByDisplayValue(/Run workflow "summarize-file" with file_path="notes\/brief.md"\./),
      ).toBeInTheDocument(),
    );
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

  it("supports starter-pack repairs and saved runbook macros", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/sessions")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/tree")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/dashboard")) {
        return Promise.resolve(mockResponse({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }));
      }
      if (url.includes("/api/observer/state")) return Promise.resolve(mockResponse({}));
      if (url.includes("/api/audit/events")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/approvals/pending")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/operator/timeline")) return Promise.resolve(mockResponse({ items: [] }));
      if (url.includes("/api/observer/continuity")) {
        return Promise.resolve(mockResponse({
          daemon: { connected: false, pending_notification_count: 0, capture_mode: "balanced" },
          notifications: [],
          queued_insights: [],
          queued_insight_count: 0,
          recent_interventions: [],
        }));
      }
      if (url.includes("/api/capabilities/starter-packs/research-briefing/activate")) {
        return Promise.resolve(mockResponse({ status: "activated" }));
      }
      if (url.includes("/api/capabilities/overview")) {
        return Promise.resolve(mockResponse({
          tool_policy_mode: "balanced",
          mcp_policy_mode: "approval",
          approval_mode: "high_risk",
          summary: {
            native_tools_ready: 1,
            native_tools_total: 2,
            skills_ready: 1,
            skills_total: 1,
            workflows_ready: 0,
            workflows_total: 1,
            starter_packs_ready: 0,
            starter_packs_total: 1,
            mcp_servers_ready: 0,
            mcp_servers_total: 0,
          },
          native_tools: [{ name: "write_file", description: "Write", risk_level: "medium", execution_boundaries: ["workspace_write"], availability: "blocked", blocked_reason: "tool_policy_balanced" }],
          skills: [{ name: "web-briefing", enabled: true, description: "Web briefing", requires_tools: ["write_file"], availability: "blocked", missing_tools: ["write_file"] }],
          workflows: [{
            name: "web-brief-to-file",
            tool_name: "workflow_web_brief_to_file",
            description: "Research and save",
            inputs: { query: { type: "string", description: "Query", required: true } },
            requires_tools: ["write_file"],
            requires_skills: ["web-briefing"],
            user_invocable: true,
            enabled: true,
            step_count: 2,
            file_path: "defaults/workflows/web-brief-to-file.json",
            policy_modes: ["balanced", "full"],
            execution_boundaries: ["workspace_write"],
            risk_level: "medium",
            requires_approval: false,
            approval_behavior: "direct",
            is_available: false,
            missing_tools: ["write_file"],
            missing_skills: [],
            recommended_actions: [{ type: "set_tool_policy", label: "Allow write_file", mode: "full" }],
          }],
          mcp_servers: [],
          starter_packs: [{
            name: "research-briefing",
            label: "Research briefing",
            description: "Enable the research pack.",
            sample_prompt: "Run workflow \"web-brief-to-file\" with query=\"seraph\".",
            skills: ["web-briefing"],
            workflows: ["web-brief-to-file"],
            ready_skills: [],
            ready_workflows: [],
            blocked_skills: [{ name: "web-briefing", availability: "blocked", missing_tools: ["write_file"] }],
            blocked_workflows: [{ name: "web-brief-to-file", availability: "blocked", missing_tools: ["write_file"], missing_skills: [] }],
            availability: "blocked",
            recommended_actions: [
              { type: "activate_starter_pack", label: "Activate pack", name: "research-briefing" },
              { type: "set_tool_policy", label: "Allow write_file", mode: "full" },
            ],
          }],
          catalog_items: [],
          recommendations: [],
          runbooks: [{
            id: "workflow:web-brief-to-file",
            name: "web-brief-to-file",
            label: "Run web-brief-to-file",
            description: "Draft the research pack workflow",
            source: "workflow",
            command: "Run workflow \"web-brief-to-file\" with query=\"seraph\".",
            availability: "blocked",
            blocking_reasons: ["missing tool: write_file"],
            recommended_actions: [{ type: "set_tool_policy", label: "Allow write_file", mode: "full" }],
            parameter_schema: { query: { type: "string", description: "Query" } },
            risk_level: "medium",
            execution_boundaries: ["workspace_write"],
            action: { type: "draft_workflow", label: "Draft workflow", name: "web-brief-to-file" },
          }],
        }));
      }
      if (url.includes("/api/workflows/runs")) return Promise.resolve(mockResponse({ runs: [] }));
      if (url.includes("/api/settings/tool-policy-mode")) return Promise.resolve(mockResponse({ mode: "balanced" }));
      if (url.includes("/api/settings/mcp-policy-mode")) return Promise.resolve(mockResponse({ mode: "approval" }));
      if (url.includes("/api/settings/approval-mode")) return Promise.resolve(mockResponse({ mode: "high_risk" }));
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={() => {}} />);

    await waitFor(() => expect(screen.getByText("Operator terminal")).toBeInTheDocument());
    fireEvent.click(await screen.findByRole("button", { name: "save macro" }, { timeout: 5000 }));
    await waitFor(() => expect(screen.getByText("1 saved")).toBeInTheDocument());
    expect(screen.getAllByText("Run web-brief-to-file").length).toBeGreaterThan(1);
    const starterPackRow = screen.getByText("Research briefing").closest(".cockpit-operator-row");
    expect(starterPackRow).not.toBeNull();
    fireEvent.click(within(starterPackRow as HTMLElement).getByRole("button", { name: "repair" }));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/settings/tool-policy-mode"),
        expect.objectContaining({ method: "PUT" }),
      ),
    );
    const macroRow = screen.getAllByText("Run web-brief-to-file")[1]?.closest(".cockpit-operator-row");
    expect(macroRow).not.toBeNull();
    fireEvent.click(within(macroRow as HTMLElement).getByRole("button", { name: "repair" }));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/capabilities/preflight?target_type=runbook&name=workflow%3Aweb-brief-to-file"),
      ),
    );
  });

  it("drafts the starter-pack command after a successful bootstrap", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/sessions")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/tree")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/dashboard")) {
        return Promise.resolve(mockResponse({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }));
      }
      if (url.includes("/api/observer/state")) return Promise.resolve(mockResponse({}));
      if (url.includes("/api/audit/events")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/approvals/pending")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/operator/timeline")) return Promise.resolve(mockResponse({ items: [] }));
      if (url.includes("/api/observer/continuity")) {
        return Promise.resolve(mockResponse({
          daemon: { connected: false, pending_notification_count: 0, capture_mode: "balanced" },
          notifications: [],
          queued_insights: [],
          queued_insight_count: 0,
          recent_interventions: [],
        }));
      }
      if (url.includes("/api/capabilities/overview")) {
        return Promise.resolve(mockResponse({
          tool_policy_mode: "balanced",
          mcp_policy_mode: "approval",
          approval_mode: "high_risk",
          summary: {
            native_tools_ready: 1,
            native_tools_total: 1,
            skills_ready: 1,
            skills_total: 1,
            workflows_ready: 0,
            workflows_total: 1,
            starter_packs_ready: 0,
            starter_packs_total: 1,
            mcp_servers_ready: 0,
            mcp_servers_total: 0,
          },
          native_tools: [{ name: "write_file", description: "Write", risk_level: "medium", execution_boundaries: ["workspace_write"], availability: "blocked", blocked_reason: "tool_policy_balanced" }],
          skills: [{ name: "web-briefing", enabled: true, description: "Web briefing", requires_tools: ["write_file"], availability: "blocked", missing_tools: ["write_file"] }],
          workflows: [{
            name: "web-brief-to-file",
            tool_name: "workflow_web_brief_to_file",
            description: "Research and save",
            inputs: { query: { type: "string", description: "Query", required: true } },
            requires_tools: ["write_file"],
            requires_skills: ["web-briefing"],
            user_invocable: true,
            enabled: true,
            step_count: 2,
            file_path: "defaults/workflows/web-brief-to-file.json",
            policy_modes: ["balanced", "full"],
            execution_boundaries: ["workspace_write"],
            risk_level: "medium",
            requires_approval: false,
            approval_behavior: "direct",
            is_available: false,
            missing_tools: ["write_file"],
            missing_skills: [],
            recommended_actions: [{ type: "set_tool_policy", label: "Allow write_file", mode: "full" }],
          }],
          mcp_servers: [],
          starter_packs: [{
            name: "research-briefing",
            label: "Research briefing",
            description: "Enable the research pack.",
            sample_prompt: "Run workflow \"web-brief-to-file\" with query=\"seraph\".",
            skills: ["web-briefing"],
            workflows: ["web-brief-to-file"],
            ready_skills: [],
            ready_workflows: [],
            blocked_skills: [{ name: "web-briefing", availability: "blocked", missing_tools: ["write_file"] }],
            blocked_workflows: [{ name: "web-brief-to-file", availability: "blocked", missing_tools: ["write_file"], missing_skills: [] }],
            availability: "blocked",
            recommended_actions: [
              { type: "activate_starter_pack", label: "Activate pack", name: "research-briefing" },
              { type: "set_tool_policy", label: "Allow write_file", mode: "full" },
            ],
          }],
          catalog_items: [],
          recommendations: [],
          runbooks: [],
        }));
      }
      if (url.includes("/api/capabilities/bootstrap")) {
        expect(init?.method).toBe("POST");
        return Promise.resolve(mockResponse({
          target_type: "starter_pack",
          name: "research-briefing",
          label: "Research briefing",
          status: "ready",
          ready: true,
          availability: "ready",
          blocking_reasons: [],
          applied_actions: [{ type: "set_tool_policy", mode: "full", status: "applied" }],
          manual_actions: [],
          command: "Run workflow \"web-brief-to-file\" with query=\"seraph\".",
          overview: { summary: { starter_packs_ready: 1 } },
        }));
      }
      if (url.includes("/api/workflows/runs")) return Promise.resolve(mockResponse({ runs: [] }));
      if (url.includes("/api/settings/tool-policy-mode")) return Promise.resolve(mockResponse({ mode: "balanced" }));
      if (url.includes("/api/settings/mcp-policy-mode")) return Promise.resolve(mockResponse({ mode: "approval" }));
      if (url.includes("/api/settings/approval-mode")) return Promise.resolve(mockResponse({ mode: "high_risk" }));
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={() => {}} />);

    await waitFor(() => expect(screen.getByText("Research briefing")).toBeInTheDocument());
    const starterPackRow = screen.getByText("Research briefing").closest(".cockpit-operator-row");
    expect(starterPackRow).not.toBeNull();
    fireEvent.click(within(starterPackRow as HTMLElement).getByRole("button", { name: "activate" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/capabilities/bootstrap"),
        expect.objectContaining({ method: "POST" }),
      ),
    );
    await waitFor(() =>
      expect(screen.getByDisplayValue('Run workflow "web-brief-to-file" with query="seraph".')).toBeInTheDocument(),
    );
  });

  it("runs manual bootstrap actions for blocked runbooks before stopping", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/sessions")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/tree")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/dashboard")) {
        return Promise.resolve(mockResponse({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }));
      }
      if (url.includes("/api/observer/state")) return Promise.resolve(mockResponse({}));
      if (url.includes("/api/audit/events")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/approvals/pending")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/operator/timeline")) return Promise.resolve(mockResponse({ items: [] }));
      if (url.includes("/api/observer/continuity")) {
        return Promise.resolve(mockResponse({
          daemon: { connected: false, pending_notification_count: 0, capture_mode: "balanced" },
          notifications: [],
          queued_insights: [],
          queued_insight_count: 0,
          recent_interventions: [],
        }));
      }
      if (url.includes("/api/capabilities/overview")) {
        return Promise.resolve(mockResponse({
          tool_policy_mode: "balanced",
          mcp_policy_mode: "approval",
          approval_mode: "high_risk",
          summary: {
            native_tools_ready: 0,
            native_tools_total: 0,
            skills_ready: 0,
            skills_total: 0,
            workflows_ready: 0,
            workflows_total: 0,
            starter_packs_ready: 0,
            starter_packs_total: 0,
            mcp_servers_ready: 0,
            mcp_servers_total: 1,
          },
          native_tools: [],
          skills: [],
          workflows: [],
          mcp_servers: [{
            name: "github",
            status: "auth_required",
            enabled: true,
            description: "GitHub MCP",
            tool_count: 0,
            auth_hint: "Add a token",
            missing_env_vars: ["GITHUB_TOKEN"],
            required_env_vars: ["GITHUB_TOKEN"],
            recommended_actions: [{ type: "test_mcp_server", label: "Test server", name: "github" }],
          }],
          starter_packs: [],
          catalog_items: [],
          recommendations: [],
          runbooks: [{
            id: "runbook:github-sync",
            name: "github-sync",
            label: "GitHub sync",
            description: "Repair GitHub MCP and resume the runbook",
            source: "workflow",
            command: "Run workflow \"github-sync\".",
            availability: "blocked",
            blocking_reasons: ["mcp auth required: github"],
            recommended_actions: [{ type: "test_mcp_server", label: "Test server", name: "github" }],
            parameter_schema: {},
            risk_level: "medium",
            execution_boundaries: ["external_mcp"],
            action: { type: "draft_workflow", label: "Draft workflow", name: "github-sync" },
          }],
        }));
      }
      if (url.includes("/api/capabilities/preflight")) {
        return Promise.resolve(mockResponse({
          target_type: "runbook",
          name: "runbook:github-sync",
          label: "GitHub sync",
          description: "Repair GitHub MCP and resume the runbook",
          availability: "blocked",
          blocking_reasons: ["mcp auth required: github"],
          autorepair_actions: [],
          recommended_actions: [{ type: "test_mcp_server", label: "Test server", name: "github" }],
          ready: false,
          can_autorepair: false,
        }));
      }
      if (url.includes("/api/capabilities/bootstrap")) {
        expect(init?.method).toBe("POST");
        return Promise.resolve(mockResponse({
          target_type: "runbook",
          name: "runbook:github-sync",
          label: "GitHub sync",
          status: "blocked",
          ready: false,
          availability: "blocked",
          blocking_reasons: ["mcp auth required: github"],
          applied_actions: [],
          manual_actions: [{ type: "test_mcp_server", label: "Test server", name: "github" }],
        }));
      }
      if (url.includes("/api/mcp/servers/github/test")) {
        return Promise.resolve(mockResponse({ status: "ok", tool_count: 3 }));
      }
      if (url.includes("/api/workflows/runs")) return Promise.resolve(mockResponse({ runs: [] }));
      if (url.includes("/api/settings/tool-policy-mode")) return Promise.resolve(mockResponse({ mode: "balanced" }));
      if (url.includes("/api/settings/mcp-policy-mode")) return Promise.resolve(mockResponse({ mode: "approval" }));
      if (url.includes("/api/settings/approval-mode")) return Promise.resolve(mockResponse({ mode: "high_risk" }));
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={() => {}} />);

    await waitFor(() => expect(screen.getByText("GitHub sync")).toBeInTheDocument());
    const runbookRow = screen.getByText("GitHub sync").closest(".cockpit-operator-row");
    expect(runbookRow).not.toBeNull();
    fireEvent.click(within(runbookRow as HTMLElement).getByRole("button", { name: "repair" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/capabilities/bootstrap"),
        expect.objectContaining({ method: "POST" }),
      ),
    );
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/mcp/servers/github/test"),
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });

  it("keeps step repair visible even when replay is blocked", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/sessions")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/tree")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/dashboard")) {
        return Promise.resolve(mockResponse({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }));
      }
      if (url.includes("/api/observer/state")) return Promise.resolve(mockResponse({}));
      if (url.includes("/api/approvals/pending")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/operator/timeline")) return Promise.resolve(mockResponse({ items: [] }));
      if (url.includes("/api/observer/continuity")) {
        return Promise.resolve(mockResponse({
          daemon: { connected: false, pending_notification_count: 0, capture_mode: "balanced" },
          notifications: [],
          queued_insights: [],
          queued_insight_count: 0,
          recent_interventions: [],
        }));
      }
      if (url.includes("/api/capabilities/overview")) {
        return Promise.resolve(mockResponse({
          tool_policy_mode: "balanced",
          mcp_policy_mode: "approval",
          approval_mode: "high_risk",
          summary: {
            native_tools_ready: 1,
            native_tools_total: 1,
            skills_ready: 0,
            skills_total: 0,
            workflows_ready: 0,
            workflows_total: 1,
            starter_packs_ready: 0,
            starter_packs_total: 0,
            mcp_servers_ready: 0,
            mcp_servers_total: 0,
          },
          native_tools: [{ name: "write_file", description: "Write", risk_level: "medium", execution_boundaries: ["workspace_write"], availability: "blocked", blocked_reason: "tool_policy_balanced" }],
          skills: [],
          workflows: [],
          mcp_servers: [],
          starter_packs: [],
          catalog_items: [],
          recommendations: [],
          runbooks: [],
        }));
      }
      if (url.includes("/api/workflows/runs")) {
        return Promise.resolve(mockResponse({
          runs: [
            {
              id: "evt-call",
              tool_name: "workflow_web_brief_to_file",
              workflow_name: "web-brief-to-file",
              session_id: "session-1",
              status: "failed",
              started_at: "2026-03-18T12:01:00Z",
              updated_at: "2026-03-18T12:01:45Z",
              summary: "workflow_web_brief_to_file failed at write_file",
              step_tools: ["web_search", "write_file"],
              artifact_paths: [],
              continued_error_steps: ["write_file"],
              arguments: { query: "seraph", file_path: "notes/brief.md" },
              risk_level: "medium",
              execution_boundaries: ["external_read", "workspace_write"],
              accepts_secret_refs: false,
              pending_approval_count: 0,
              pending_approval_ids: [],
              thread_id: "session-1",
              thread_label: "Session 1",
              run_fingerprint: "fp-1",
              run_identity: "session-1:workflow_web_brief_to_file:fp-1",
              replay_allowed: false,
              replay_block_reason: "availability",
              replay_draft: null,
              retry_from_step_draft: null,
              replay_recommended_actions: [{ type: "set_tool_policy", label: "Allow write_file", mode: "full" }],
              step_records: [
                {
                  id: "write_file",
                  index: 1,
                  tool: "write_file",
                  status: "failed",
                  argument_keys: ["file_path"],
                  artifact_paths: [],
                  result_summary: "Permission blocked",
                  error_kind: "tool_failed",
                  error_summary: "write_file blocked by tool policy",
                  recovery_actions: [{ type: "set_tool_policy", label: "Allow write_file", mode: "full" }],
                  is_recoverable: true,
                },
              ],
              approval_recovery_message: null,
              timeline: [
                { kind: "workflow_started", at: "2026-03-18T12:01:00Z", summary: "Workflow started" },
                { kind: "workflow_step_failed", at: "2026-03-18T12:01:45Z", summary: "write_file failed" },
              ],
            },
          ],
        }));
      }
      if (url.includes("/api/settings/tool-policy-mode")) return Promise.resolve(mockResponse({ mode: "full" }));
      if (url.includes("/api/settings/mcp-policy-mode")) return Promise.resolve(mockResponse({ mode: "approval" }));
      if (url.includes("/api/settings/approval-mode")) return Promise.resolve(mockResponse({ mode: "high_risk" }));
      if (url.includes("/api/audit/events")) return Promise.resolve(mockResponse([]));
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={() => {}} />);

    await waitFor(() => expect(screen.getByText("workflow_web_brief_to_file failed at write_file")).toBeInTheDocument());
    fireEvent.click(screen.getByText("workflow_web_brief_to_file failed at write_file"));
    expect(screen.getByRole("button", { name: "Repair step" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Repair step" }));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/settings/tool-policy-mode"),
        expect.objectContaining({ method: "PUT" }),
      ),
    );
  });

  it("surfaces routing summaries in the operator timeline", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/sessions")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/tree")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/dashboard")) {
        return Promise.resolve(mockResponse({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }));
      }
      if (url.includes("/api/observer/state")) return Promise.resolve(mockResponse({}));
      if (url.includes("/api/approvals/pending")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/workflows/runs")) return Promise.resolve(mockResponse({ runs: [] }));
      if (url.includes("/api/audit/events")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/observer/continuity")) {
        return Promise.resolve(mockResponse({
          daemon: { connected: false, pending_notification_count: 0, capture_mode: "balanced" },
          notifications: [],
          queued_insights: [],
          queued_insight_count: 0,
          recent_interventions: [],
        }));
      }
      if (url.includes("/api/capabilities/overview")) {
        return Promise.resolve(mockResponse({
          tool_policy_mode: "balanced",
          mcp_policy_mode: "approval",
          approval_mode: "high_risk",
          summary: {
            native_tools_ready: 0,
            native_tools_total: 0,
            skills_ready: 0,
            skills_total: 0,
            workflows_ready: 0,
            workflows_total: 0,
            starter_packs_ready: 0,
            starter_packs_total: 0,
            mcp_servers_ready: 0,
            mcp_servers_total: 0,
          },
          native_tools: [],
          skills: [],
          workflows: [],
          mcp_servers: [],
          starter_packs: [],
          catalog_items: [],
          recommendations: [],
          runbooks: [],
        }));
      }
      if (url.includes("/api/operator/timeline")) {
        return Promise.resolve(mockResponse({
          items: [
            {
              id: "routing-1",
              kind: "routing",
              title: "chat_agent",
              summary: "Selected openai/gpt-4o-mini for chat_agent",
              status: "selected",
              created_at: "2026-03-18T12:01:00Z",
              updated_at: "2026-03-18T12:01:00Z",
              thread_id: "session-1",
              thread_label: "Session 1",
              source: "runtime",
              metadata: {
                selected_model: "openai/gpt-4o-mini",
                selected_source: "fallback_chain",
                reroute_cause: "policy_guardrails",
                max_budget_class: "standard",
                required_task_class: "interactive",
                required_policy_intents: ["fast", "cheap"],
                max_cost_tier: "medium",
                max_latency_tier: "low",
                rejected_target_count: 2,
              },
            },
          ],
        }));
      }
      if (url.includes("/api/settings/tool-policy-mode")) return Promise.resolve(mockResponse({ mode: "balanced" }));
      if (url.includes("/api/settings/mcp-policy-mode")) return Promise.resolve(mockResponse({ mode: "approval" }));
      if (url.includes("/api/settings/approval-mode")) return Promise.resolve(mockResponse({ mode: "high_risk" }));
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={() => {}} />);

    await waitFor(() => expect(screen.getByText("Selected openai/gpt-4o-mini for chat_agent")).toBeInTheDocument());
    expect(screen.getByText(/model openai\/gpt-4o-mini · fallback_chain · policy_guardrails · budget standard · task interactive/)).toBeInTheDocument();
    expect(screen.getByText(/intents fast, cheap · cost medium · latency low · rejected 2/)).toBeInTheDocument();
  });

  it("does not offer extension studio actions for operator timeline items", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/sessions")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/tree")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/dashboard")) {
        return Promise.resolve(mockResponse({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }));
      }
      if (url.includes("/api/observer/state")) return Promise.resolve(mockResponse({}));
      if (url.includes("/api/approvals/pending")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/workflows/runs")) return Promise.resolve(mockResponse({ runs: [] }));
      if (url.includes("/api/audit/events")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/observer/continuity")) {
        return Promise.resolve(mockResponse({
          daemon: { connected: false, pending_notification_count: 0, capture_mode: "balanced" },
          notifications: [],
          queued_insights: [],
          queued_insight_count: 0,
          recent_interventions: [],
        }));
      }
      if (url.includes("/api/capabilities/overview")) {
        return Promise.resolve(mockResponse({
          tool_policy_mode: "balanced",
          mcp_policy_mode: "approval",
          approval_mode: "high_risk",
          summary: {
            native_tools_ready: 0,
            native_tools_total: 0,
            skills_ready: 0,
            skills_total: 0,
            workflows_ready: 0,
            workflows_total: 0,
            starter_packs_ready: 0,
            starter_packs_total: 0,
            mcp_servers_ready: 0,
            mcp_servers_total: 0,
          },
          native_tools: [],
          skills: [],
          workflows: [],
          mcp_servers: [],
          starter_packs: [],
          catalog_items: [],
          recommendations: [],
          runbooks: [],
        }));
      }
      if (url.includes("/api/operator/timeline")) {
        return Promise.resolve(mockResponse({
          items: [
            {
              id: "routing-1",
              kind: "routing",
              title: "chat_agent",
              summary: "Selected openai/gpt-4o-mini for chat_agent",
              status: "selected",
              created_at: "2026-03-18T12:01:00Z",
              updated_at: "2026-03-18T12:01:00Z",
              thread_id: "session-1",
              thread_label: "Session 1",
              source: "runtime",
              metadata: {
                selected_model: "openai/gpt-4o-mini",
              },
            },
          ],
        }));
      }
      if (url.includes("/api/settings/tool-policy-mode")) return Promise.resolve(mockResponse({ mode: "balanced" }));
      if (url.includes("/api/settings/mcp-policy-mode")) return Promise.resolve(mockResponse({ mode: "approval" }));
      if (url.includes("/api/settings/approval-mode")) return Promise.resolve(mockResponse({ mode: "high_risk" }));
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={() => {}} />);

    const row = await screen.findByText("Selected openai/gpt-4o-mini for chat_agent");
    fireEvent.click(row);

    expect(screen.queryByRole("button", { name: "Open Studio" })).not.toBeInTheDocument();
  });

  it("surfaces the latest assistant response in the main cockpit column", async () => {
    useChatStore.setState({
      messages: [
        {
          id: "user-1",
          role: "user",
          content: "What changed today?",
          timestamp: Date.now() - 1000,
        },
        {
          id: "agent-1",
          role: "agent",
          content: "You completed the workflow batch and refreshed the roadmap queue.",
          timestamp: Date.now(),
        },
      ],
    });

    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/sessions")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/tree")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/dashboard")) {
        return Promise.resolve(mockResponse({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }));
      }
      if (url.includes("/api/observer/state")) return Promise.resolve(mockResponse({}));
      if (url.includes("/api/audit/events")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/approvals/pending")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/operator/timeline")) return Promise.resolve(mockResponse({ items: [] }));
      if (url.includes("/api/observer/continuity")) {
        return Promise.resolve(mockResponse({
          daemon: { connected: false, pending_notification_count: 0, capture_mode: "balanced" },
          notifications: [],
          queued_insights: [],
          queued_insight_count: 0,
          recent_interventions: [],
        }));
      }
      if (url.includes("/api/capabilities/overview")) {
        return Promise.resolve(mockResponse({
          tool_policy_mode: "balanced",
          mcp_policy_mode: "approval",
          approval_mode: "high_risk",
          summary: {},
          native_tools: [],
          skills: [],
          workflows: [],
          mcp_servers: [],
          starter_packs: [],
        }));
      }
      if (url.includes("/api/workflows/runs")) return Promise.resolve(mockResponse({ runs: [] }));
      if (url.includes("/api/settings/tool-policy-mode")) return Promise.resolve(mockResponse({ mode: "balanced" }));
      if (url.includes("/api/settings/mcp-policy-mode")) return Promise.resolve(mockResponse({ mode: "approval" }));
      if (url.includes("/api/settings/approval-mode")) return Promise.resolve(mockResponse({ mode: "high_risk" }));
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={() => {}} />);

    await waitFor(() => expect(screen.getByText("Latest response")).toBeInTheDocument());
    expect(
      screen.getAllByText("You completed the workflow batch and refreshed the roadmap queue.").length,
    ).toBeGreaterThan(1);
  });

  it("does not queue a continuation draft when the target thread cannot be opened", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/sessions/missing-session/messages")) {
        return Promise.resolve(mockResponse({ detail: "Not found" }, false, 404));
      }
      if (url.includes("/api/sessions")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/tree")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/dashboard")) {
        return Promise.resolve(mockResponse({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }));
      }
      if (url.includes("/api/observer/state")) return Promise.resolve(mockResponse({}));
      if (url.includes("/api/approvals/pending")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/operator/timeline")) return Promise.resolve(mockResponse({ items: [] }));
      if (url.includes("/api/workflows/runs")) return Promise.resolve(mockResponse({ runs: [] }));
      if (url.includes("/api/audit/events")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/capabilities/overview")) {
        return Promise.resolve(
          mockResponse({
            tool_policy_mode: "balanced",
            mcp_policy_mode: "approval",
            approval_mode: "high_risk",
            summary: {},
            native_tools: [],
            skills: [],
            workflows: [],
            mcp_servers: [],
            starter_packs: [],
            catalog_items: [],
            recommendations: [],
            runbooks: [],
          }),
        );
      }
      if (url.includes("/api/observer/continuity")) {
        return Promise.resolve(
          mockResponse({
            daemon: { connected: false, pending_notification_count: 1, capture_mode: "balanced" },
            notifications: [
              {
                id: "note-stale",
                intervention_id: "intervention-stale",
                title: "Resume stale thread",
                body: "This thread no longer exists.",
                intervention_type: "advisory",
                urgency: 1,
                created_at: "2026-03-18T12:03:00Z",
                session_id: "missing-session",
                resume_message: "Continue from stale thread",
              },
            ],
            queued_insights: [],
            queued_insight_count: 0,
            recent_interventions: [],
          }),
        );
      }
      if (url.includes("/api/settings/tool-policy-mode")) return Promise.resolve(mockResponse({ mode: "balanced" }));
      if (url.includes("/api/settings/mcp-policy-mode")) return Promise.resolve(mockResponse({ mode: "approval" }));
      if (url.includes("/api/settings/approval-mode")) return Promise.resolve(mockResponse({ mode: "high_risk" }));
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={() => {}} />);

    await waitFor(() => expect(screen.getByRole("button", { name: "Continue" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    await waitFor(() => expect(screen.getByText("Unable to open that thread.")).toBeInTheDocument());
    expect(screen.queryByDisplayValue("Continue from stale thread")).not.toBeInTheDocument();
    expect(useChatStore.getState().sessionId).toBe("session-1");
  });

  it("uses workflow-attached approvals when the pending sidebar is capped away", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/sessions/session-2/messages")) {
        return Promise.resolve(mockResponse([]));
      }
      if (url.includes("/api/sessions")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/tree")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/dashboard")) {
        return Promise.resolve(mockResponse({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }));
      }
      if (url.includes("/api/observer/state")) return Promise.resolve(mockResponse({}));
      if (url.includes("/api/audit/events")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/approvals/pending")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/operator/timeline")) return Promise.resolve(mockResponse({ items: [] }));
      if (url.includes("/api/observer/continuity")) {
        return Promise.resolve(mockResponse({
          daemon: { connected: false, pending_notification_count: 0, capture_mode: "balanced" },
          notifications: [],
          queued_insights: [],
          queued_insight_count: 0,
          recent_interventions: [],
        }));
      }
      if (url.includes("/api/capabilities/overview")) {
        return Promise.resolve(mockResponse({
          tool_policy_mode: "balanced",
          mcp_policy_mode: "approval",
          approval_mode: "high_risk",
          summary: {},
          native_tools: [],
          skills: [],
          workflows: [],
          mcp_servers: [],
          starter_packs: [],
          catalog_items: [],
          recommendations: [],
          runbooks: [],
        }));
      }
      if (url.includes("/api/workflows/runs")) {
        return Promise.resolve(mockResponse({
          runs: [{
            id: "workflow-run-1",
            tool_name: "workflow_web_brief_to_file",
            workflow_name: "web-brief-to-file",
            session_id: "session-2",
            status: "awaiting_approval",
            started_at: "2026-03-18T12:01:00Z",
            updated_at: "2026-03-18T12:01:45Z",
            summary: "workflow_web_brief_to_file is waiting for approval",
            step_tools: ["web_search", "write_file"],
            artifact_paths: [],
            continued_error_steps: [],
            arguments: { query: "seraph", file_path: "notes/brief.md" },
            risk_level: "medium",
            execution_boundaries: ["external_read", "workspace_write"],
            accepts_secret_refs: false,
            pending_approval_count: 1,
            pending_approval_ids: ["approval-run-1"],
            pending_approvals: [{
              id: "approval-run-1",
              summary: "Approve write_file for web brief",
              risk_level: "medium",
              created_at: "2026-03-18T12:01:30Z",
              thread_id: "session-2",
              thread_label: "Approval thread",
              resume_message: "Continue workflow after approval.",
            }],
            thread_id: "session-2",
            thread_label: "Approval thread",
            thread_source: "session",
            replay_allowed: true,
            replay_block_reason: null,
            replay_draft: "Run workflow \"web-brief-to-file\" with query=\"seraph\", file_path=\"notes/brief.md\".",
            approval_recovery_message: "Continue workflow after approval.",
            timeline: [
              { kind: "workflow_started", at: "2026-03-18T12:01:00Z", summary: "Workflow started" },
            ],
          }],
        }));
      }
      if (url.includes("/api/settings/tool-policy-mode")) return Promise.resolve(mockResponse({ mode: "balanced" }));
      if (url.includes("/api/settings/mcp-policy-mode")) return Promise.resolve(mockResponse({ mode: "approval" }));
      if (url.includes("/api/settings/approval-mode")) return Promise.resolve(mockResponse({ mode: "high_risk" }));
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={() => {}} />);

    fireEvent.click(await screen.findByRole("button", { name: "Continue" }, { timeout: 5000 }));

    await waitFor(() => expect(useChatStore.getState().sessionId).toBe("session-2"), { timeout: 5000 });
    expect(
      await screen.findByDisplayValue("Continue workflow after approval.", {}, { timeout: 5000 }),
    ).toBeInTheDocument();
  });

  it("shows a visible pending state and fresh-thread guidance while the agent is working", async () => {
    useChatStore.setState({
      messages: [
        {
          id: "user-1",
          role: "user",
          content: "Summarize what I can do here.",
          timestamp: Date.now() - 1000,
        },
      ],
      sessionId: null,
      sessions: [],
      isAgentBusy: true,
    });

    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/sessions")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/tree")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/dashboard")) {
        return Promise.resolve(mockResponse({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }));
      }
      if (url.includes("/api/observer/state")) return Promise.resolve(mockResponse({}));
      if (url.includes("/api/audit/events")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/approvals/pending")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/operator/timeline")) return Promise.resolve(mockResponse({ items: [] }));
      if (url.includes("/api/observer/continuity")) {
        return Promise.resolve(mockResponse({
          daemon: { connected: false, pending_notification_count: 0, capture_mode: "balanced" },
          notifications: [],
          queued_insights: [],
          queued_insight_count: 0,
          recent_interventions: [],
        }));
      }
      if (url.includes("/api/workflows")) return Promise.resolve(mockResponse({ workflows: [] }));
      if (url.includes("/api/skills")) return Promise.resolve(mockResponse({ skills: [] }));
      if (url.includes("/api/mcp/servers")) return Promise.resolve(mockResponse({ servers: [] }));
      if (url.includes("/api/settings/tool-policy-mode")) return Promise.resolve(mockResponse({ mode: "balanced" }));
      if (url.includes("/api/settings/mcp-policy-mode")) return Promise.resolve(mockResponse({ mode: "approval" }));
      if (url.includes("/api/settings/approval-mode")) return Promise.resolve(mockResponse({ mode: "high_risk" }));
      if (url.includes("/api/tools")) return Promise.resolve(mockResponse([]));
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={vi.fn()} />);

    await waitFor(() => expect(screen.getAllByText("Seraph is responding").length).toBeGreaterThan(0));
    expect(screen.getByRole("button", { name: "Working" })).toBeDisabled();
    expect(screen.getAllByText(/fresh thread/i).length).toBeGreaterThan(0);
    expect(screen.getAllByRole("button", { name: "Start fresh" }).length).toBeGreaterThan(0);
  });

  it("opens the extension studio and loads workflow validation details", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/sessions")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/tree")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/dashboard")) {
        return Promise.resolve(mockResponse({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }));
      }
      if (url.includes("/api/observer/state")) return Promise.resolve(mockResponse({}));
      if (url.includes("/api/audit/events")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/approvals/pending")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/operator/timeline")) return Promise.resolve(mockResponse({ items: [] }));
      if (url.includes("/api/observer/continuity")) {
        return Promise.resolve(mockResponse({
          daemon: { connected: false, pending_notification_count: 0, capture_mode: "balanced" },
          notifications: [],
          queued_insights: [],
          queued_insight_count: 0,
          recent_interventions: [],
        }));
      }
      if (url.includes("/api/capabilities/overview")) {
        return Promise.resolve(mockResponse({
          tool_policy_mode: "balanced",
          mcp_policy_mode: "approval",
          approval_mode: "high_risk",
          summary: {
            native_tools_ready: 1,
            native_tools_total: 1,
            skills_ready: 0,
            skills_total: 0,
            workflows_ready: 0,
            workflows_total: 1,
            starter_packs_ready: 0,
            starter_packs_total: 0,
            mcp_servers_ready: 0,
            mcp_servers_total: 0,
          },
          native_tools: [{ name: "web_search", description: "Search", risk_level: "low", execution_boundaries: ["external_read"], availability: "ready" }],
          skills: [],
          workflows: [{
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
            file_path: "defaults/workflows/web-brief-to-file.md",
            policy_modes: ["balanced", "full"],
            execution_boundaries: ["external_read", "workspace_write"],
            risk_level: "medium",
            requires_approval: false,
            approval_behavior: "never",
            is_available: false,
            availability: "blocked",
            missing_tools: ["write_file"],
            missing_skills: [],
            recommended_actions: [{ type: "set_tool_policy", label: "Allow write_file", mode: "full" }],
          }],
          mcp_servers: [],
          starter_packs: [],
          catalog_items: [],
          recommendations: [],
          runbooks: [],
        }));
      }
      if (url.includes("/api/workflows/runs")) return Promise.resolve(mockResponse({ runs: [] }));
      if (url.includes("/api/settings/tool-policy-mode")) return Promise.resolve(mockResponse({ mode: "balanced" }));
      if (url.includes("/api/settings/mcp-policy-mode")) return Promise.resolve(mockResponse({ mode: "approval" }));
      if (url.includes("/api/settings/approval-mode")) return Promise.resolve(mockResponse({ mode: "high_risk" }));
      if (url.includes("/api/capabilities/preflight")) {
        return Promise.resolve(mockResponse({
          target_type: "workflow",
          name: "web-brief-to-file",
          label: "web-brief-to-file",
          description: "Search and save a brief",
          availability: "blocked",
          blocking_reasons: ["missing tools: write_file"],
          autorepair_actions: [{ type: "set_tool_policy", label: "Allow write_file", mode: "full" }],
          recommended_actions: [{ type: "toggle_workflow", label: "Enable workflow", name: "web-brief-to-file", enabled: true }],
          command: 'Run workflow "web-brief-to-file".',
          risk_level: "medium",
          execution_boundaries: ["external_read", "workspace_write"],
          can_autorepair: true,
          ready: false,
        }));
      }
      if (url.includes("/api/workflows/diagnostics")) {
        return Promise.resolve(mockResponse({
          loaded_count: 1,
          error_count: 1,
          workflows: [{ name: "web-brief-to-file" }],
          load_errors: [{ file_path: "defaults/workflows/web-brief-to-file.md", message: "Undeclared step tool." }],
        }));
      }
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={() => {}} />);

    await waitFor(() => expect(screen.getByRole("button", { name: "Extension studio" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Extension studio" }));

    const studio = await screen.findByLabelText("Extension studio");
    fireEvent.click(within(studio).getByRole("button", { name: "Refresh validation" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/capabilities/preflight?target_type=workflow&name=web-brief-to-file"),
      ),
    );
    await waitFor(() => expect(within(studio).getByText(/missing tools: write_file/i)).toBeInTheDocument());
    await waitFor(() => expect(within(studio).getByText(/Undeclared step tool/i)).toBeInTheDocument());
  });

  it("surfaces workflow resume metadata and opens the studio from a workflow run", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/sessions")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/tree")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/dashboard")) {
        return Promise.resolve(mockResponse({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }));
      }
      if (url.includes("/api/observer/state")) return Promise.resolve(mockResponse({}));
      if (url.includes("/api/audit/events")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/approvals/pending")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/operator/timeline")) return Promise.resolve(mockResponse({ items: [] }));
      if (url.includes("/api/observer/continuity")) {
        return Promise.resolve(mockResponse({
          daemon: { connected: false, pending_notification_count: 0, capture_mode: "balanced" },
          notifications: [],
          queued_insights: [],
          queued_insight_count: 0,
          recent_interventions: [],
        }));
      }
      if (url.includes("/api/capabilities/overview")) {
        return Promise.resolve(mockResponse({
          tool_policy_mode: "balanced",
          mcp_policy_mode: "approval",
          approval_mode: "high_risk",
          summary: {
            native_tools_ready: 1,
            native_tools_total: 1,
            skills_ready: 0,
            skills_total: 0,
            workflows_ready: 1,
            workflows_total: 1,
            starter_packs_ready: 0,
            starter_packs_total: 0,
            mcp_servers_ready: 0,
            mcp_servers_total: 0,
          },
          native_tools: [{ name: "read_file", description: "Read", risk_level: "low", execution_boundaries: ["workspace_read"], availability: "ready" }],
          skills: [],
          workflows: [{
            name: "resume-review",
            tool_name: "workflow_resume_review",
            description: "Resume a review workflow",
            inputs: { file_path: { type: "string", description: "Workspace file", required: true } },
            requires_tools: ["read_file"],
            requires_skills: [],
            user_invocable: true,
            enabled: true,
            step_count: 1,
            file_path: "defaults/workflows/resume-review.md",
            policy_modes: ["balanced", "full"],
            execution_boundaries: ["workspace_read"],
            risk_level: "low",
            requires_approval: false,
            approval_behavior: "never",
            is_available: true,
            availability: "ready",
            missing_tools: [],
            missing_skills: [],
          }],
          mcp_servers: [],
          starter_packs: [],
          catalog_items: [],
          recommendations: [],
          runbooks: [],
        }));
      }
      if (url.includes("/api/workflows/runs")) {
        return Promise.resolve(mockResponse({
          runs: [{
            id: "run-1",
            tool_name: "workflow_resume_review",
            workflow_name: "resume-review",
            session_id: "session-1",
            status: "awaiting_approval",
            started_at: "2026-03-20T09:00:00Z",
            updated_at: "2026-03-20T09:05:00Z",
            summary: "resume-review waiting on approval",
            step_tools: ["read_file"],
            artifact_paths: [],
            continued_error_steps: [],
            risk_level: "low",
            thread_id: "session-1",
            run_identity: "resume-run-123456",
            run_fingerprint: "resume-fingerprint-abcdef",
            resume_checkpoint_label: "review_checkpoint",
            thread_continue_message: "Continue from the review checkpoint.",
            approval_recovery_message: "Approval recovery is available.",
            replay_allowed: true,
            replay_draft: 'Run workflow "resume-review" with file_path="notes/review.md".',
          }],
        }));
      }
      if (url.includes("/api/settings/tool-policy-mode")) return Promise.resolve(mockResponse({ mode: "balanced" }));
      if (url.includes("/api/settings/mcp-policy-mode")) return Promise.resolve(mockResponse({ mode: "approval" }));
      if (url.includes("/api/settings/approval-mode")) return Promise.resolve(mockResponse({ mode: "high_risk" }));
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={() => {}} />);

    const workflowLabel = await screen.findByText("resume-review");
    const workflowRow = workflowLabel.closest(".cockpit-row");
    expect(workflowRow).not.toBeNull();
    expect(within(workflowRow as HTMLElement).getByText(/checkpoint review_checkpoint/i)).toBeInTheDocument();
    expect(within(workflowRow as HTMLElement).getByText(/run resume-r/i)).toBeInTheDocument();

    fireEvent.click(within(workflowRow as HTMLElement).getByRole("button", { name: "Studio" }));

    const studio = await screen.findByLabelText("Extension studio");
    expect(within(studio).getByText("Resume a review workflow")).toBeInTheDocument();
  });

  it("lets operators edit MCP config from the extension studio", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/sessions")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/tree")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/dashboard")) {
        return Promise.resolve(mockResponse({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }));
      }
      if (url.includes("/api/observer/state")) return Promise.resolve(mockResponse({}));
      if (url.includes("/api/audit/events")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/approvals/pending")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/operator/timeline")) return Promise.resolve(mockResponse({ items: [] }));
      if (url.includes("/api/observer/continuity")) {
        return Promise.resolve(mockResponse({
          daemon: { connected: false, pending_notification_count: 0, capture_mode: "balanced" },
          notifications: [],
          queued_insights: [],
          queued_insight_count: 0,
          recent_interventions: [],
        }));
      }
      if (url.includes("/api/capabilities/overview")) {
        return Promise.resolve(mockResponse({
          tool_policy_mode: "balanced",
          mcp_policy_mode: "approval",
          approval_mode: "high_risk",
          summary: {
            native_tools_ready: 0,
            native_tools_total: 0,
            skills_ready: 0,
            skills_total: 0,
            workflows_ready: 0,
            workflows_total: 0,
            starter_packs_ready: 0,
            starter_packs_total: 0,
            mcp_servers_ready: 0,
            mcp_servers_total: 1,
          },
          native_tools: [],
          skills: [],
          workflows: [],
          mcp_servers: [{
            name: "github",
            enabled: true,
            url: "http://localhost:9001/mcp",
            description: "GitHub MCP",
            status: "auth_required",
            status_message: "Missing token",
            has_headers: true,
            auth_hint: "Add a GitHub token",
            tool_count: 0,
            availability: "blocked",
            blocked_reason: "auth_required",
            recommended_actions: [{ type: "test_mcp_server", label: "Test server", name: "github" }],
          }],
          starter_packs: [],
          catalog_items: [],
          recommendations: [],
          runbooks: [],
        }));
      }
      if (url.includes("/api/workflows/runs")) return Promise.resolve(mockResponse({ runs: [] }));
      if (url.includes("/api/settings/tool-policy-mode")) return Promise.resolve(mockResponse({ mode: "balanced" }));
      if (url.includes("/api/settings/mcp-policy-mode")) return Promise.resolve(mockResponse({ mode: "approval" }));
      if (url.includes("/api/settings/approval-mode")) return Promise.resolve(mockResponse({ mode: "high_risk" }));
      if (url.includes("/api/mcp/servers/github") && init?.method === "PUT") {
        return Promise.resolve(mockResponse({ status: "updated", name: "github" }));
      }
      if (url.includes("/api/mcp/servers/validate")) {
        return Promise.resolve(mockResponse({
          valid: true,
          name: "github",
          url: "http://localhost:9010/mcp",
          status: "ready_to_test",
          issues: [],
          warnings: [],
          missing_env_vars: [],
          enabled: true,
          description: "GitHub MCP",
          has_headers: true,
          auth_hint: "Add a GitHub token",
          existing: true,
        }));
      }
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={() => {}} />);

    const githubLabel = await screen.findByText("github");
    const githubRow = githubLabel.closest(".cockpit-operator-row");
    expect(githubRow).not.toBeNull();

    fireEvent.click(within(githubRow as HTMLElement).getByRole("button", { name: "studio" }));

    const studio = await screen.findByLabelText("Extension studio");
    const urlInput = within(studio).getByLabelText("mcp url");
    fireEvent.change(urlInput, { target: { value: "http://localhost:9010/mcp" } });
    fireEvent.click(within(studio).getByRole("button", { name: "Save config" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/mcp/servers/github"),
        expect.objectContaining({ method: "PUT" }),
      ),
    );

    fireEvent.click(within(studio).getByRole("button", { name: "Validate config" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/mcp/servers/validate"),
        expect.objectContaining({
          method: "POST",
          body: expect.stringContaining('"url":"http://localhost:9010/mcp"'),
        }),
      ),
    );
    await waitFor(() => expect(within(studio).getByText(/github config is ready to test/i)).toBeInTheDocument());
  });

  it("preserves the existing workflow file when saving studio drafts", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/sessions")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/tree")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/dashboard")) {
        return Promise.resolve(mockResponse({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }));
      }
      if (url.includes("/api/observer/state")) return Promise.resolve(mockResponse({}));
      if (url.includes("/api/audit/events")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/approvals/pending")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/operator/timeline")) return Promise.resolve(mockResponse({ items: [] }));
      if (url.includes("/api/observer/continuity")) {
        return Promise.resolve(mockResponse({
          daemon: { connected: false, pending_notification_count: 0, capture_mode: "balanced" },
          notifications: [],
          queued_insights: [],
          queued_insight_count: 0,
          recent_interventions: [],
        }));
      }
      if (url.includes("/api/capabilities/overview")) {
        return Promise.resolve(mockResponse({
          tool_policy_mode: "balanced",
          mcp_policy_mode: "approval",
          approval_mode: "high_risk",
          summary: {
            native_tools_ready: 0,
            native_tools_total: 0,
            skills_ready: 0,
            skills_total: 0,
            workflows_ready: 1,
            workflows_total: 1,
            starter_packs_ready: 0,
            starter_packs_total: 0,
            mcp_servers_ready: 0,
            mcp_servers_total: 0,
          },
          native_tools: [],
          skills: [],
          workflows: [{
            name: "summarize-file",
            tool_name: "workflow_summarize_file",
            description: "Summarize an existing workspace file",
            inputs: { file_path: { type: "string", description: "Workspace file", required: true } },
            requires_tools: ["read_file"],
            requires_skills: [],
            user_invocable: true,
            enabled: true,
            step_count: 1,
            file_path: "defaults/workflows/custom-file.md",
            policy_modes: ["balanced", "full"],
            execution_boundaries: ["workspace_read"],
            risk_level: "low",
            requires_approval: false,
            approval_behavior: "direct",
            is_available: true,
            availability: "ready",
            missing_tools: [],
            missing_skills: [],
            recommended_actions: [],
          }],
          mcp_servers: [],
          starter_packs: [],
          catalog_items: [],
          recommendations: [],
          runbooks: [],
        }));
      }
      if (url.includes("/api/workflows/runs")) return Promise.resolve(mockResponse({ runs: [] }));
      if (url.includes("/api/settings/tool-policy-mode")) return Promise.resolve(mockResponse({ mode: "balanced" }));
      if (url.includes("/api/settings/mcp-policy-mode")) return Promise.resolve(mockResponse({ mode: "approval" }));
      if (url.includes("/api/settings/approval-mode")) return Promise.resolve(mockResponse({ mode: "high_risk" }));
      if (url.includes("/api/workflows/summarize-file/source")) {
        return Promise.resolve(mockResponse({ content: "name: summarize-file\nsummary: workflow draft" }));
      }
      if (url.includes("/api/workflows/save")) {
        return Promise.resolve(mockResponse({ status: "saved", name: "renamed-workflow", file_name: "custom-file.md" }));
      }
      if (url.includes("/api/workflows/validate")) {
        return Promise.resolve(mockResponse({ valid: true, issues: [], warnings: [] }));
      }
      if (url.includes("/api/capabilities/preflight?target_type=workflow&name=summarize-file")) {
        return Promise.resolve(mockResponse({
          target_type: "workflow",
          name: "summarize-file",
          label: "summarize-file",
          description: "Summarize an existing workspace file",
          availability: "ready",
          blocking_reasons: [],
          recommended_actions: [],
          autorepair_actions: [],
          can_autorepair: false,
          ready: true,
        }));
      }
      if (url.includes("/api/workflows/diagnostics")) {
        return Promise.resolve(mockResponse({ loaded_count: 1, error_count: 0, workflows: [], load_errors: [] }));
      }
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={vi.fn()} />);

    await waitFor(() => expect(screen.getByRole("button", { name: "Extension studio" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Extension studio" }));

    const studio = await screen.findByLabelText("Extension studio");
    const draftInput = await within(studio).findByLabelText("authoring draft");
    fireEvent.change(draftInput, { target: { value: "name: renamed-workflow\nsummary: workflow draft" } });
    fireEvent.click(within(studio).getByRole("button", { name: "Save draft" }));

    await waitFor(() => {
      const saveCall = fetchMock.mock.calls.find(
        ([input]) => String(input).includes("/api/workflows/save"),
      );
      expect(saveCall).toBeDefined();
      const init = saveCall?.[1] as RequestInit | undefined;
      const body = JSON.parse(String(init?.body ?? "{}")) as { file_name?: string };
      expect(body.file_name).toBe("custom-file.md");
    });
  });

  it("keeps successful cockpit surfaces visible when one refresh endpoint fails", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/sessions")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/tree")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/dashboard")) {
        return Promise.resolve(mockResponse({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }));
      }
      if (url.includes("/api/observer/state")) return Promise.resolve(mockResponse({}));
      if (url.includes("/api/audit/events")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/approvals/pending")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/operator/timeline")) return Promise.reject(new Error("timeline unavailable"));
      if (url.includes("/api/observer/continuity")) {
        return Promise.resolve(mockResponse({
          daemon: { connected: false, pending_notification_count: 0, capture_mode: "balanced" },
          notifications: [],
          queued_insights: [],
          queued_insight_count: 0,
          recent_interventions: [],
        }));
      }
      if (url.includes("/api/capabilities/overview")) {
        return Promise.resolve(mockResponse({
          tool_policy_mode: "balanced",
          mcp_policy_mode: "approval",
          approval_mode: "high_risk",
          summary: {
            native_tools_ready: 1,
            native_tools_total: 1,
            skills_ready: 1,
            skills_total: 1,
            workflows_ready: 1,
            workflows_total: 1,
            starter_packs_ready: 1,
            starter_packs_total: 1,
            mcp_servers_ready: 0,
            mcp_servers_total: 0,
          },
          native_tools: [{ name: "read_file", description: "Read", risk_level: "low", execution_boundaries: ["workspace_read"], availability: "ready" }],
          skills: [{ name: "daily-standup", enabled: true, description: "Standup", requires_tools: ["read_file"], availability: "ready", missing_tools: [] }],
          workflows: [{
            name: "summarize-file",
            tool_name: "workflow_summarize_file",
            description: "Summarize a file",
            inputs: { file_path: { type: "string", description: "Workspace file", required: true } },
            requires_tools: ["read_file"],
            requires_skills: [],
            user_invocable: true,
            enabled: true,
            step_count: 1,
            file_path: "defaults/workflows/summarize-file.md",
            policy_modes: ["balanced", "full"],
            execution_boundaries: ["workspace_read"],
            risk_level: "low",
            requires_approval: false,
            approval_behavior: "direct",
            is_available: true,
            availability: "ready",
            missing_tools: [],
            missing_skills: [],
          }],
          mcp_servers: [],
          starter_packs: [{
            name: "daily-operator-rhythm",
            label: "Daily operator rhythm",
            description: "Starter pack",
            sample_prompt: "Run workflow \"summarize-file\" with file_path=\"notes/today.md\".",
            skills: ["daily-standup"],
            workflows: ["summarize-file"],
            ready_skills: ["daily-standup"],
            ready_workflows: ["summarize-file"],
            blocked_skills: [],
            blocked_workflows: [],
            availability: "ready",
            recommended_actions: [],
          }],
          catalog_items: [],
          recommendations: [],
          runbooks: [{
            id: "workflow:summarize-file",
            name: "summarize-file",
            label: "Run summarize-file",
            description: "Summarize a file",
            source: "workflow",
            command: "Run workflow \"summarize-file\" with file_path=\"notes/today.md\".",
            availability: "ready",
            blocking_reasons: [],
            recommended_actions: [],
            parameter_schema: { file_path: { type: "string", description: "Workspace file" } },
            risk_level: "low",
            execution_boundaries: ["workspace_read"],
            action: { type: "draft_workflow", label: "Draft workflow", name: "summarize-file" },
          }],
        }));
      }
      if (url.includes("/api/workflows/runs")) return Promise.resolve(mockResponse({ runs: [] }));
      if (url.includes("/api/settings/tool-policy-mode")) return Promise.resolve(mockResponse({ mode: "balanced" }));
      if (url.includes("/api/settings/mcp-policy-mode")) return Promise.resolve(mockResponse({ mode: "approval" }));
      if (url.includes("/api/settings/approval-mode")) return Promise.resolve(mockResponse({ mode: "high_risk" }));
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={vi.fn()} />);

    await waitFor(() => expect(screen.getByText("Daily operator rhythm")).toBeInTheDocument());
    expect(screen.getByText("Run summarize-file")).toBeInTheDocument();
    expect(screen.queryByText("Operator surface unavailable.")).not.toBeInTheDocument();
  });

  it("ignores stale studio source responses after switching entries", async () => {
    let resolveWorkflowSource!: (value: unknown) => void;
    const workflowSourcePromise = new Promise<unknown>((resolve) => {
      resolveWorkflowSource = resolve;
    });

    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/sessions")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/tree")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/dashboard")) {
        return Promise.resolve(mockResponse({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }));
      }
      if (url.includes("/api/observer/state")) return Promise.resolve(mockResponse({}));
      if (url.includes("/api/audit/events")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/approvals/pending")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/operator/timeline")) return Promise.resolve(mockResponse({ items: [] }));
      if (url.includes("/api/observer/continuity")) {
        return Promise.resolve(mockResponse({
          daemon: { connected: false, pending_notification_count: 0, capture_mode: "balanced" },
          notifications: [],
          queued_insights: [],
          queued_insight_count: 0,
          recent_interventions: [],
        }));
      }
      if (url.includes("/api/capabilities/overview")) {
        return Promise.resolve(mockResponse({
          tool_policy_mode: "balanced",
          mcp_policy_mode: "approval",
          approval_mode: "high_risk",
          summary: {
            native_tools_ready: 1,
            native_tools_total: 1,
            skills_ready: 1,
            skills_total: 1,
            workflows_ready: 1,
            workflows_total: 1,
            starter_packs_ready: 0,
            starter_packs_total: 0,
            mcp_servers_ready: 0,
            mcp_servers_total: 0,
          },
          native_tools: [{ name: "read_file", description: "Read", risk_level: "low", execution_boundaries: ["workspace_read"], availability: "ready" }],
          skills: [{
            name: "daily-standup",
            enabled: true,
            description: "Generate a standup update",
            requires_tools: ["read_file"],
            availability: "ready",
            missing_tools: [],
          }],
          workflows: [{
            name: "summarize-file",
            tool_name: "workflow_summarize_file",
            description: "Summarize a file",
            inputs: { file_path: { type: "string", description: "Workspace file", required: true } },
            requires_tools: ["read_file"],
            requires_skills: [],
            user_invocable: true,
            enabled: true,
            step_count: 1,
            file_path: "defaults/workflows/summarize-file.md",
            policy_modes: ["balanced", "full"],
            execution_boundaries: ["workspace_read"],
            risk_level: "low",
            requires_approval: false,
            approval_behavior: "direct",
            is_available: true,
            availability: "ready",
            missing_tools: [],
            missing_skills: [],
          }],
          mcp_servers: [],
          starter_packs: [],
          catalog_items: [],
          recommendations: [],
          runbooks: [],
        }));
      }
      if (url.includes("/api/workflows/runs")) return Promise.resolve(mockResponse({ runs: [] }));
      if (url.includes("/api/settings/tool-policy-mode")) return Promise.resolve(mockResponse({ mode: "balanced" }));
      if (url.includes("/api/settings/mcp-policy-mode")) return Promise.resolve(mockResponse({ mode: "approval" }));
      if (url.includes("/api/settings/approval-mode")) return Promise.resolve(mockResponse({ mode: "high_risk" }));
      if (url.includes("/api/workflows/summarize-file/source")) {
        return workflowSourcePromise as Promise<{ ok: boolean; status: number; json: () => Promise<unknown> }>;
      }
      if (url.includes("/api/skills/daily-standup/source")) {
        return Promise.resolve(mockResponse({ content: "name: daily-standup\nsummary: skill draft" }));
      }
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={vi.fn()} />);

    await waitFor(() => expect(screen.getByRole("button", { name: "Extension studio" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Extension studio" }));

    const studio = await screen.findByLabelText("Extension studio");
    fireEvent.click(within(studio).getByText("daily-standup").closest("button") as HTMLButtonElement);

    await waitFor(() =>
      expect(within(studio).getByLabelText("authoring draft")).toHaveValue("name: daily-standup\nsummary: skill draft"),
    );

    await act(async () => {
      resolveWorkflowSource(mockResponse({ content: "name: summarize-file\nsummary: workflow draft" }));
      await Promise.resolve();
    });

    expect(within(studio).getByLabelText("authoring draft")).toHaveValue("name: daily-standup\nsummary: skill draft");
  });

  it("does not process refresh payloads after the cockpit unmounts", async () => {
    const deferredResponses = Array.from({ length: 10 }, () => {
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

    await waitFor(() => expect(cockpitFetchCount).toBe(10));
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

  it("keeps the composer text when send fails", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/sessions")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/tree")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/dashboard")) {
        return Promise.resolve(mockResponse({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }));
      }
      if (url.includes("/api/observer/state")) return Promise.resolve(mockResponse({}));
      if (url.includes("/api/audit/events")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/approvals/pending")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/operator/timeline")) return Promise.resolve(mockResponse({ items: [] }));
      if (url.includes("/api/observer/continuity")) {
        return Promise.resolve(mockResponse({
          daemon: { connected: false, pending_notification_count: 0, capture_mode: "balanced" },
          notifications: [],
          queued_insights: [],
          queued_insight_count: 0,
          recent_interventions: [],
        }));
      }
      if (url.includes("/api/capabilities/overview")) {
        return Promise.resolve(mockResponse({
          tool_policy_mode: "balanced",
          mcp_policy_mode: "approval",
          approval_mode: "high_risk",
          summary: {},
          native_tools: [],
          skills: [],
          workflows: [],
          mcp_servers: [],
          starter_packs: [],
        }));
      }
      if (url.includes("/api/workflows/runs")) return Promise.resolve(mockResponse({ runs: [] }));
      if (url.includes("/api/settings/tool-policy-mode")) return Promise.resolve(mockResponse({ mode: "balanced" }));
      if (url.includes("/api/settings/mcp-policy-mode")) return Promise.resolve(mockResponse({ mode: "approval" }));
      if (url.includes("/api/settings/approval-mode")) return Promise.resolve(mockResponse({ mode: "high_risk" }));
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={vi.fn(async () => false)} />);

    const composer = await screen.findByPlaceholderText(/Ask Seraph/i);
    fireEvent.change(composer, { target: { value: "keep me" } });
    fireEvent.submit(composer.closest("form")!);

    await waitFor(() => expect(screen.getByDisplayValue("keep me")).toBeInTheDocument());
  });
});
