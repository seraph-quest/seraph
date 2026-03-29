import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CockpitView } from "./CockpitView";
import { useChatStore } from "../../stores/chatStore";
import { useCockpitLayoutStore } from "../../stores/cockpitLayoutStore";
import { usePanelLayoutStore } from "../../stores/panelLayoutStore";
import { useQuestStore } from "../../stores/questStore";
import { getDefaultPaneVisibility } from "./layouts";

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
    useCockpitLayoutStore.setState({
      activeLayoutId: "default",
      inspectorVisible: true,
      paneVisibility: getDefaultPaneVisibility("default"),
      savedPaneVisibility: {
        default: getDefaultPaneVisibility("default"),
      },
    });
    usePanelLayoutStore.setState({
      panels: {
        ...usePanelLayoutStore.getState().panels,
      },
    });
  });

  afterEach(() => {
    vi.useRealTimers();
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
                  source_path: {
                    type: "string",
                    description: "Workspace file",
                    required: true,
                    artifact_input: true,
                    artifact_types: ["markdown_document", "workspace_file"],
                  },
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
                name: "annotate-image",
                tool_name: "workflow_annotate_image",
                description: "Annotate an image asset",
                inputs: {
                  image_path: {
                    type: "string",
                    description: "Image file",
                    required: true,
                    artifact_input: true,
                    artifact_types: ["image"],
                  },
                },
                requires_tools: ["read_file"],
                requires_skills: [],
                user_invocable: true,
                enabled: true,
                step_count: 1,
                file_path: "defaults/workflows/annotate-image.json",
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
                branch_kind: "retry_failed_step",
                branch_depth: 0,
                checkpoint_context_available: true,
                replay_allowed: true,
                replay_block_reason: null,
                replay_draft: "Run workflow \"web-brief-to-file\" with query=\"seraph\", file_path=\"notes/brief.md\".",
                retry_from_step_draft: "Retry workflow \"web-brief-to-file\" from step \"write_file\" with query=\"seraph\", file_path=\"notes/brief.md\".",
                checkpoint_candidates: [
                  {
                    step_id: "web_search",
                    kind: "branch_from_checkpoint",
                    status: "succeeded",
                    resume_supported: true,
                    resume_draft:
                      "Run workflow \"web-brief-to-file\" with query=\"seraph\", file_path=\"notes/brief.md\", _seraph_resume_from_step=\"web_search\".",
                  },
                  {
                    step_id: "write_file",
                    kind: "retry_failed_step",
                    status: "degraded",
                    resume_supported: true,
                    resume_draft:
                      "Run workflow \"web-brief-to-file\" with query=\"seraph\", file_path=\"notes/brief.md\", _seraph_resume_from_step=\"write_file\".",
                  },
                ],
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
    expect(screen.getByText("Activity ledger")).toBeInTheDocument();
    expect(screen.queryByText("Desktop shell")).not.toBeInTheDocument();
    expect(screen.getByText("Operator terminal")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Set tool policy to balanced" })).toHaveAttribute("aria-pressed", "true"),
    );
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
    expect(screen.getByText("invocable 2/3 available")).toBeInTheDocument();
    expect(screen.getByText("approval 0 · blocked 1")).toBeInTheDocument();
    expect(screen.getByText("blocked web-brief-to-file")).toBeInTheDocument();
    expect(screen.getByText("tools write_file")).toBeInTheDocument();
    expect(screen.getByText("bundle 1 queued")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Windows" }));
    const windowsMenu = await screen.findByText("Desktop Shell");
    fireEvent.click(windowsMenu.closest("button") as HTMLButtonElement);

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
    expect(screen.getByRole("button", { name: "Branch web_search" })).toBeInTheDocument();
    expect(screen.getByText("Use Output")).toBeInTheDocument();
    const runButton = screen.getByRole("button", { name: "Run summarize-file" });
    expect(runButton).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Run annotate-image" })).not.toBeInTheDocument();
    fireEvent.click(runButton);
    await waitFor(() =>
      expect(
        screen.getByDisplayValue(/Run workflow "summarize-file" with source_path="notes\/brief.md"\./),
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
  }, 30000);

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

  it("surfaces manual bootstrap actions for blocked runbooks without auto-running them", async () => {
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
    const mcpTestCallCountBefore = fetchMock.mock.calls.filter(
      ([input, init]) => String(input).includes("/api/mcp/servers/github/test") && init?.method === "POST",
    ).length;
    fireEvent.click(within(runbookRow as HTMLElement).getByRole("button", { name: "repair" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/capabilities/bootstrap"),
        expect.objectContaining({ method: "POST" }),
      ),
    );
    await waitFor(() => expect(screen.getByText("doctor plans")).toBeInTheDocument());
    const mcpTestCallCountAfter = fetchMock.mock.calls.filter(
      ([input, init]) => String(input).includes("/api/mcp/servers/github/test") && init?.method === "POST",
    ).length;
    expect(mcpTestCallCountAfter).toBe(mcpTestCallCountBefore);
  });

  it("routes explicit manual bootstrap extension enables through the lifecycle approval path", async () => {
    let approvalQueued = false;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/sessions")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/tree")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/dashboard")) {
        return Promise.resolve(mockResponse({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }));
      }
      if (url.includes("/api/observer/state")) return Promise.resolve(mockResponse({}));
      if (url.includes("/api/audit/events")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/activity/ledger")) {
        return Promise.resolve(mockResponse({ items: [], summary: { total_items: 0, visible_groups: 0 } }));
      }
      if (url.includes("/api/approvals/pending")) {
        return Promise.resolve(mockResponse(
          approvalQueued
            ? [{
              id: "approval-extension-enable",
              session_id: null,
              thread_id: null,
              thread_label: null,
              tool_name: "extension_enable",
              risk_level: "high",
              status: "pending",
              summary: "Enable Research Pack contributions with high-risk capabilities",
              created_at: "2026-03-23T10:00:00Z",
              resume_message: null,
              extension_id: "seraph.research-pack",
              extension_display_name: "Research Pack",
              extension_action: "enable",
              package_path: "/tmp/workspace/extensions/research-pack",
              lifecycle_boundaries: ["workspace_write"],
              permissions: { tool_names: ["write_file"] },
            }]
            : [],
        ));
      }
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
          runbooks: [{
            id: "runbook:research-briefing",
            name: "research-briefing",
            label: "Research briefing",
            description: "Enable packaged workflow support and resume the runbook",
            source: "workflow",
            command: "Run workflow \"web-brief-to-file\" with query=\"seraph\".",
            availability: "blocked",
            blocking_reasons: ["workflow packaged in a disabled extension"],
            recommended_actions: [{ type: "enable_extension", label: "Enable Research Pack", name: "seraph.research-pack", target: "Research Pack" }],
            parameter_schema: {},
            risk_level: "medium",
            execution_boundaries: ["workspace_write"],
            action: { type: "draft_workflow", label: "Draft workflow", name: "web-brief-to-file" },
          }],
        }));
      }
      if (url.includes("/api/capabilities/preflight")) {
        return Promise.resolve(mockResponse({
          target_type: "runbook",
          name: "runbook:research-briefing",
          label: "Research briefing",
          description: "Enable packaged workflow support and resume the runbook",
          availability: "blocked",
          blocking_reasons: ["workflow packaged in a disabled extension"],
          autorepair_actions: [],
          recommended_actions: [{ type: "enable_extension", label: "Enable Research Pack", name: "seraph.research-pack", target: "Research Pack" }],
          ready: false,
          can_autorepair: false,
        }));
      }
      if (url.includes("/api/capabilities/bootstrap")) {
        expect(init?.method).toBe("POST");
        return Promise.resolve(mockResponse({
          target_type: "runbook",
          name: "runbook:research-briefing",
          label: "Research briefing",
          status: "blocked",
          ready: false,
          availability: "blocked",
          blocking_reasons: ["workflow packaged in a disabled extension"],
          applied_actions: [],
          manual_actions: [{ type: "enable_extension", label: "Enable Research Pack", name: "seraph.research-pack", target: "Research Pack" }],
        }));
      }
      if (url.includes("/api/extensions/seraph.research-pack/enable") && init?.method === "POST") {
        approvalQueued = true;
        return Promise.resolve(mockResponse({
          detail: {
            type: "approval_required",
            approval_id: "approval-extension-enable",
            tool_name: "extension_enable",
            risk_level: "high",
            message: "Enable extension 'Research Pack' with access to high-risk capabilities. Approve it first, then retry the extension action.",
          },
        }, false, 409));
      }
      if (url.includes("/api/extensions") && !url.includes("/source")) {
        return Promise.resolve(mockResponse({
          extensions: [{
            id: "seraph.research-pack",
            display_name: "Research Pack",
            version: "2026.3.21",
            kind: "capability-pack",
            trust: "local",
            source: "manifest",
            location: "workspace",
            status: "ready",
            summary: "Research workflows",
            issues: [],
            load_errors: [],
            toggle_targets: [{ type: "workflow", name: "web-brief-to-file" }],
            toggleable_contribution_types: ["workflows"],
            passive_contribution_types: ["runbooks"],
            enable_supported: true,
            disable_supported: false,
            removable: true,
            enabled_scope: "toggleable_contributions",
            configurable: false,
            metadata_supported: false,
            config_scope: "metadata_only",
            enabled: false,
            config: {},
            studio_files: [],
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

    await waitFor(() => expect(screen.getByText("Research briefing")).toBeInTheDocument());
    const runbookRow = screen.getByText("Research briefing").closest(".cockpit-operator-row");
    expect(runbookRow).not.toBeNull();
    fireEvent.click(within(runbookRow as HTMLElement).getByRole("button", { name: "repair" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/capabilities/bootstrap"),
        expect.objectContaining({ method: "POST" }),
      ),
    );
    fireEvent.click(await screen.findByRole("button", { name: "run manual" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/extensions/seraph.research-pack/enable"),
        expect.objectContaining({ method: "POST" }),
      ),
    );
    expect(await screen.findByText("Enable Research Pack contributions with high-risk capabilities")).toBeInTheDocument();
    expect(await screen.findByText("approval-extension-enable")).toBeInTheDocument();
    expect(screen.queryByText("Research briefing repair sequence applied")).not.toBeInTheDocument();
  });

  it("blocks generated multi-step privileged repair bundles from running in one click", async () => {
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
          runbooks: [{
            id: "runbook:research-briefing",
            name: "research-briefing",
            label: "Research briefing",
            description: "Enable the pack and repair policy",
            source: "workflow",
            command: null,
            availability: "blocked",
            blocking_reasons: ["missing policy and extension enablement"],
            recommended_actions: [
              { type: "set_tool_policy", label: "Allow write_file", mode: "full" },
              { type: "enable_extension", label: "Enable extension", name: "seraph.research-pack" },
            ],
            parameter_schema: {},
            risk_level: "high",
            execution_boundaries: ["workspace_write"],
            action: { type: "draft_workflow", label: "Draft workflow", name: "research-briefing" },
          }],
        }));
      }
      if (url.includes("/api/capabilities/preflight")) {
        return Promise.resolve(mockResponse({
          target_type: "runbook",
          name: "runbook:research-briefing",
          label: "Research briefing",
          description: "Enable the pack and repair policy",
          availability: "blocked",
          blocking_reasons: ["missing policy and extension enablement"],
          autorepair_actions: [],
          recommended_actions: [
            { type: "set_tool_policy", label: "Allow write_file", mode: "full" },
            { type: "enable_extension", label: "Enable extension", name: "seraph.research-pack" },
          ],
          ready: false,
          can_autorepair: false,
        }));
      }
      if (url.includes("/api/capabilities/bootstrap")) {
        return Promise.resolve(mockResponse({
          target_type: "runbook",
          name: "runbook:research-briefing",
          label: "Research briefing",
          status: "blocked",
          ready: false,
          availability: "blocked",
          blocking_reasons: ["missing policy and extension enablement"],
          applied_actions: [],
          manual_actions: [
            { type: "set_tool_policy", label: "Allow write_file", mode: "full" },
            { type: "enable_extension", label: "Enable extension", name: "seraph.research-pack" },
          ],
        }));
      }
      if (url.includes("/api/workflows/runs")) return Promise.resolve(mockResponse({ runs: [] }));
      if (url.includes("/api/settings/tool-policy-mode")) return Promise.resolve(mockResponse({ mode: "full" }));
      if (url.includes("/api/extensions/seraph.research-pack/enable")) return Promise.resolve(mockResponse({ status: "enabled" }));
      if (url.includes("/api/settings/mcp-policy-mode")) return Promise.resolve(mockResponse({ mode: "approval" }));
      if (url.includes("/api/settings/approval-mode")) return Promise.resolve(mockResponse({ mode: "high_risk" }));
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={() => {}} />);

    await waitFor(() => expect(screen.getByText("Research briefing")).toBeInTheDocument());
    const runbookRow = screen.getByText("Research briefing").closest(".cockpit-operator-row");
    expect(runbookRow).not.toBeNull();
    fireEvent.click(within(runbookRow as HTMLElement).getByRole("button", { name: "repair" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/capabilities/bootstrap"),
        expect.objectContaining({ method: "POST" }),
      ),
    );
    const toolPolicyUpdateCountBefore = fetchMock.mock.calls.filter(
      ([input, init]) => String(input).includes("/api/settings/tool-policy-mode") && init?.method === "PUT",
    ).length;
    const extensionEnableCountBefore = fetchMock.mock.calls.filter(
      ([input, init]) => String(input).includes("/api/extensions/seraph.research-pack/enable") && init?.method === "POST",
    ).length;
    fireEvent.click(await screen.findByRole("button", { name: "run manual" }));

    await waitFor(() =>
      expect(screen.getByText(/Research briefing requires step-by-step execution/)).toBeInTheDocument(),
    );
    const toolPolicyUpdateCountAfter = fetchMock.mock.calls.filter(
      ([input, init]) => String(input).includes("/api/settings/tool-policy-mode") && init?.method === "PUT",
    ).length;
    const extensionEnableCountAfter = fetchMock.mock.calls.filter(
      ([input, init]) => String(input).includes("/api/extensions/seraph.research-pack/enable") && init?.method === "POST",
    ).length;
    expect(toolPolicyUpdateCountAfter).toBe(toolPolicyUpdateCountBefore);
    expect(extensionEnableCountAfter).toBe(extensionEnableCountBefore);
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

  it("surfaces routing summaries in the activity ledger", async () => {
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
      if (url.includes("/api/activity/ledger")) {
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
    expect(screen.queryByRole("button", { name: "Open Thread" })).not.toBeInTheDocument();
  });

  it("keeps repair actions reachable when the actionable event is a grouped child", async () => {
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
      if (url.includes("/api/activity/ledger")) {
        return Promise.resolve(mockResponse({
          summary: {
            window_hours: 24,
            started_at: "2026-03-19T10:00:00Z",
            total_items: 2,
            visible_items: 2,
            pending_approvals: 0,
            failure_count: 1,
            llm_call_count: 1,
            llm_cost_usd: 0.0123,
            input_tokens: 1000,
            output_tokens: 250,
            user_triggered_llm_calls: 1,
            autonomous_llm_calls: 0,
            categories: { llm: 1, workflow: 0, approval: 0, guardian: 0, agent: 1, system: 0 },
          },
          items: [
            {
              id: "llm-1",
              kind: "llm_call",
              category: "llm",
              group_key: "request:agent-ws:session-1:123",
              title: "LLM call",
              summary: "Conversation reasoning for Session 1 using claude-sonnet-4",
              status: "success",
              created_at: "2026-03-19T10:01:00Z",
              updated_at: "2026-03-19T10:01:00Z",
              thread_id: "session-1",
              thread_label: "Session 1",
              source: "websocket_chat",
              model: "openrouter/anthropic/claude-sonnet-4",
              provider: "openrouter",
              prompt_tokens: 1000,
              completion_tokens: 250,
              cost_usd: 0.0123,
              duration_ms: 810,
              metadata: {
                request_id: "agent-ws:session-1:123",
                capability_family: "conversation",
                runtime_path: "chat_agent",
                required_policy_intents: ["fast"],
                max_cost_tier: "medium",
                max_latency_tier: "low",
              },
            },
            {
              id: "tool-1",
              kind: "tool_failed",
              category: "agent",
              group_key: "request:agent-ws:session-1:123",
              title: "write_file",
              summary: "Workspace write blocked by policy",
              status: "failed",
              created_at: "2026-03-19T10:01:30Z",
              updated_at: "2026-03-19T10:01:30Z",
              thread_id: "session-1",
              thread_label: "Session 1",
              source: "audit",
              duration_ms: 420,
              recommended_actions: [
                { type: "set_tool_policy", label: "Allow workspace writes", mode: "full" },
              ],
              metadata: { request_id: "agent-ws:session-1:123", event_type: "tool_failed" },
            },
          ],
        }));
      }
      if (url.includes("/api/settings/tool-policy-mode")) return Promise.resolve(mockResponse({ mode: "full" }));
      if (url.includes("/api/settings/mcp-policy-mode")) return Promise.resolve(mockResponse({ mode: "approval" }));
      if (url.includes("/api/settings/approval-mode")) return Promise.resolve(mockResponse({ mode: "high_risk" }));
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={() => {}} />);

    await waitFor(() => expect(screen.getByText("Conversation reasoning for Session 1 using claude-sonnet-4")).toBeInTheDocument());
    expect(
      screen.getByRole("button", {
        name: /LLM call[\s\S]*Conversation reasoning for Session 1 using claude-sonnet-4[\s\S]*chat_agent/i,
      }),
    ).toBeInTheDocument();
    expect(screen.getByText("write_file")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Repair" }));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/settings/tool-policy-mode"),
        expect.objectContaining({ method: "PUT" }),
      ),
    );
  });

  it("renders activity ledger summaries and filters from the new endpoint", async () => {
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
      if (url.includes("/api/activity/ledger")) {
        return Promise.resolve(mockResponse({
          summary: {
            window_hours: 24,
            started_at: "2026-03-19T10:00:00Z",
            total_items: 3,
            pending_approvals: 0,
            failure_count: 0,
            llm_call_count: 1,
            llm_cost_usd: 0.0123,
            input_tokens: 1000,
            output_tokens: 250,
            user_triggered_llm_calls: 1,
            autonomous_llm_calls: 0,
            llm_cost_by_runtime_path: [
              { key: "chat_agent", calls: 1, cost_usd: 0.0123, input_tokens: 1000, output_tokens: 250 },
            ],
            llm_cost_by_capability_family: [
              { key: "conversation", calls: 1, cost_usd: 0.0123, input_tokens: 1000, output_tokens: 250 },
            ],
            categories: { llm: 1, workflow: 1, approval: 0, guardian: 0, agent: 1, system: 0 },
          },
          items: [
            {
              id: "llm-1",
              kind: "llm_call",
              category: "llm",
              group_key: "request:agent-ws:session-1:123",
              title: "LLM call",
              summary: "Conversation reasoning for Session 1 using claude-sonnet-4",
              status: "success",
              created_at: "2026-03-19T10:01:00Z",
              updated_at: "2026-03-19T10:01:00Z",
              thread_id: "session-1",
              thread_label: "Session 1",
              source: "websocket_chat",
              model: "openrouter/anthropic/claude-sonnet-4",
              provider: "openrouter",
              prompt_tokens: 1000,
              completion_tokens: 250,
              cost_usd: 0.0123,
              duration_ms: 810,
              metadata: { request_id: "agent-ws:session-1:123", runtime_path: "chat_agent", capability_family: "conversation" },
            },
            {
              id: "tool-1",
              kind: "tool_result",
              category: "agent",
              group_key: "request:agent-ws:session-1:123",
              title: "web_search",
              summary: "Found hinge specs",
              status: "succeeded",
              created_at: "2026-03-19T10:01:30Z",
              updated_at: "2026-03-19T10:01:30Z",
              thread_id: "session-1",
              thread_label: "Session 1",
              source: "audit",
              duration_ms: 420,
              metadata: { request_id: "agent-ws:session-1:123", event_type: "tool_result" },
            },
            {
              id: "wf-1",
              kind: "workflow_run",
              category: "workflow",
              title: "web-brief-to-file",
              summary: "Workflow resumed after approval",
              status: "succeeded",
              created_at: "2026-03-19T10:02:00Z",
              updated_at: "2026-03-19T10:02:00Z",
              thread_id: "session-1",
              thread_label: "Session 1",
              source: "workflow",
              recommended_actions: [],
              metadata: {},
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

    await waitFor(() => expect(screen.getByText("Activity ledger")).toBeInTheDocument());
    expect(await screen.findByText(/spend \$0\.012/)).toBeInTheDocument();
    expect(screen.getByText(/conversation \$0\.012/)).toBeInTheDocument();
    expect(screen.getByText(/chat_agent \$0\.012/)).toBeInTheDocument();
    expect(screen.getByText("Conversation reasoning for Session 1 using claude-sonnet-4")).toBeInTheDocument();
    expect(screen.getByText("web_search")).toBeInTheDocument();
    expect(screen.getByText(/2 tools|1 tool/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "llm" }));
    expect(screen.queryByText("Workflow resumed after approval")).not.toBeInTheDocument();
  });

  it("surfaces imported capability families and extension governance in the operator terminal", async () => {
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
      if (url.includes("/api/workflows/runs")) return Promise.resolve(mockResponse({ runs: [] }));
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
      if (url.includes("/api/extensions")) {
        return Promise.resolve(mockResponse({
          extensions: [{
            id: "seraph.browser-ops",
            display_name: "Browser Ops Pack",
            version: "2026.3.24",
            kind: "connector-pack",
            trust: "workspace",
            source: "manifest",
            location: "workspace",
            status: "degraded",
            summary: "Browser and messaging reach",
            issues: [],
            load_errors: [],
            toggle_targets: [],
            toggleable_contribution_types: ["browser_providers", "messaging_connectors"],
            passive_contribution_types: ["toolset_presets"],
            enable_supported: false,
            disable_supported: false,
            removable: true,
            enabled_scope: "none",
            configurable: true,
            metadata_supported: true,
            config_scope: "metadata_and_connector_configs",
            enabled: true,
            config: {},
            permission_summary: {
              status: "missing_permissions",
              ok: false,
              required: { network: true },
              missing: { network: true },
              risk_level: "high",
            },
            approval_profile: {
              requires_runtime_approval: true,
              runtime_behavior: "always",
              requires_lifecycle_approval: true,
              lifecycle_boundaries: ["network"],
              risk_level: "high",
            },
            connector_summary: {
              total: 2,
              ready: 1,
              states: { ready: 1, requires_config: 1 },
            },
            contributions: [
              {
                type: "browser_providers",
                reference: "browserbase",
                name: "browserbase",
                status: "ready",
                configured: true,
                enabled: true,
                capabilities: ["tabs", "snapshots"],
                requires_network: true,
                permission_profile: {
                  status: "granted",
                  requires_network: true,
                  missing_network: false,
                  requires_approval: false,
                  approval_behavior: "never",
                  missing_tools: [],
                  missing_execution_boundaries: [],
                },
                health: { state: "ready", ready: true, configured: true, enabled: true },
              },
              {
                type: "messaging_connectors",
                reference: "telegram",
                name: "telegram",
                status: "requires_config",
                configured: false,
                enabled: false,
                platform: "telegram",
                config_fields: [{ key: "bot_token", secret: true }],
                requires_network: true,
                permission_profile: {
                  status: "missing_permissions",
                  requires_network: true,
                  missing_network: true,
                  requires_approval: true,
                  approval_behavior: "always",
                  missing_tools: [],
                  missing_execution_boundaries: [],
                },
                health: { state: "requires_config", ready: false, configured: false, enabled: false },
              },
              {
                type: "toolset_presets",
                reference: "browser-ops",
                name: "browser ops",
                loaded: false,
                requires_network: true,
                permission_profile: {
                  status: "missing_permissions",
                  requires_network: true,
                  missing_network: true,
                  requires_approval: false,
                  approval_behavior: "never",
                  missing_tools: [],
                  missing_execution_boundaries: [],
                },
              },
            ],
            studio_files: [],
          }],
        }));
      }
      if (url.includes("/api/activity/ledger")) return Promise.resolve(mockResponse({ items: [], summary: {} }));
      if (url.includes("/api/settings/tool-policy-mode")) return Promise.resolve(mockResponse({ mode: "balanced" }));
      if (url.includes("/api/settings/mcp-policy-mode")) return Promise.resolve(mockResponse({ mode: "approval" }));
      if (url.includes("/api/settings/approval-mode")) return Promise.resolve(mockResponse({ mode: "high_risk" }));
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={() => {}} />);

    expect(await screen.findByText("browser providers")).toBeInTheDocument();
    expect(screen.getByText("extension boundaries")).toBeInTheDocument();
    expect(screen.getByText("Browser Ops Pack")).toBeInTheDocument();
    expect(screen.getByText(/lifecycle approval network/)).toBeInTheDocument();
    const operatorPane = screen.getByText("Operator terminal").closest("section");
    expect(operatorPane?.textContent ?? "").toContain("imported reach");
    expect(operatorPane?.textContent ?? "").toContain("1 active");
    expect(operatorPane?.textContent ?? "").toContain("3 installed");
    expect(operatorPane?.textContent ?? "").toContain("1 inactive");
  });

  it("derives activity ledger summary when the new endpoint omits summary fields", async () => {
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
      if (url.includes("/api/activity/ledger")) {
        return Promise.resolve(mockResponse({
          items: [
            {
              id: "llm-1",
              kind: "llm_call",
              category: "llm",
              title: "LLM call",
              summary: "Conversation reasoning for Session 1 using claude-sonnet-4",
              status: "success",
              created_at: "2026-03-19T10:01:00Z",
              updated_at: "2026-03-19T10:01:00Z",
              thread_id: "session-1",
              thread_label: "Session 1",
              source: "websocket_chat",
              model: "openrouter/anthropic/claude-sonnet-4",
              provider: "openrouter",
              prompt_tokens: 1000,
              completion_tokens: 250,
              cost_usd: 0.0123,
              duration_ms: 810,
              metadata: {
                runtime_path: " chat_agent ",
              },
            },
            {
              id: "llm-2",
              kind: "llm_call",
              category: "llm",
              title: "LLM call",
              summary: "Automation reasoning for Session 1",
              status: "success",
              created_at: "2026-03-19T10:02:00Z",
              updated_at: "2026-03-19T10:02:00Z",
              thread_id: "session-1",
              thread_label: "Session 1",
              source: "system",
              model: "openai/gpt-4.1-mini",
              provider: "openai",
              prompt_tokens: 100,
              completion_tokens: 50,
              cost_usd: 0.001,
              duration_ms: 120,
              metadata: {
                capability_family: " ",
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

    await waitFor(() => expect(screen.getByText("Activity ledger")).toBeInTheDocument());
    expect(await screen.findByText(/spend \$0\.013/)).toBeInTheDocument();
    expect(screen.getByText("1 user llm")).toBeInTheDocument();
    const runtimeSpendRow = screen.getByText((_, element) => !!(
      element?.classList.contains("cockpit-ledger-badge")
      && /chat_agent/i.test(element.textContent ?? "")
      && /\$0\.012/i.test(element.textContent ?? "")
    ));
    expect(runtimeSpendRow).toBeInTheDocument();
    const unattributedSpendRow = screen.getByText((_, element) => !!(
      element?.classList.contains("cockpit-ledger-badge")
      && /unattributed/i.test(element.textContent ?? "")
      && /\$0\.013/i.test(element.textContent ?? "")
      && /2x/i.test(element.textContent ?? "")
    ));
    expect(unattributedSpendRow).toBeInTheDocument();
  });

  it("clears stale activity ledger rows when the current ledger fetch fails before any successful payload", async () => {
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
      if (url.includes("/api/activity/ledger")) {
        if (url.includes("session_id=session-1") || !url.includes("session_id=")) {
          return Promise.resolve(mockResponse({
            items: [
              {
                id: "llm-1",
                kind: "llm_call",
                category: "llm",
                title: "LLM call",
                summary: "Conversation reasoning for Session 1 using claude-sonnet-4",
                status: "success",
                created_at: "2026-03-19T10:01:00Z",
                updated_at: "2026-03-19T10:01:00Z",
                thread_id: "session-1",
                thread_label: "Session 1",
                source: "websocket_chat",
                model: "openrouter/anthropic/claude-sonnet-4",
                provider: "openrouter",
                prompt_tokens: 1000,
                completion_tokens: 250,
                cost_usd: 0.0123,
                duration_ms: 810,
                metadata: {},
              },
            ],
          }));
        }
        return Promise.resolve(mockResponse({}, false, 500));
      }
      if (url.includes("/api/settings/tool-policy-mode")) return Promise.resolve(mockResponse({ mode: "balanced" }));
      if (url.includes("/api/settings/mcp-policy-mode")) return Promise.resolve(mockResponse({ mode: "approval" }));
      if (url.includes("/api/settings/approval-mode")) return Promise.resolve(mockResponse({ mode: "high_risk" }));
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={() => {}} />);

    await waitFor(() => expect(screen.getByText("Conversation reasoning for Session 1 using claude-sonnet-4")).toBeInTheDocument());

    act(() => {
      useChatStore.setState({
        sessionId: "session-2",
        sessions: [
          { id: "session-1", title: "Session 1", created_at: "", updated_at: "", last_message: null, last_message_role: null },
          { id: "session-2", title: "Session 2", created_at: "", updated_at: "", last_message: null, last_message_role: null },
        ],
      });
    });

    await waitFor(() => {
      expect(screen.queryByText("Conversation reasoning for Session 1 using claude-sonnet-4")).not.toBeInTheDocument();
    });
    expect(screen.getByText("No recent activity ledger entries for this filter.")).toBeInTheDocument();
  });

  it("does not offer extension studio actions for activity ledger items", async () => {
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
      if (url.includes("/api/activity/ledger")) {
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
  }, 15000);

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

    fireEvent.click(screen.getByRole("button", { name: "Windows" }));
    const menu = await screen.findByText("Desktop Shell");
    fireEvent.click(menu.closest("button") as HTMLButtonElement);

    await waitFor(() => expect(screen.getByRole("button", { name: "Continue" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    await waitFor(() => expect(screen.getByText("Unable to open that thread.")).toBeInTheDocument());
    expect(screen.queryByDisplayValue("Continue from stale thread")).not.toBeInTheDocument();
    expect(useChatStore.getState().sessionId).toBe("session-1");
  }, 15000);

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

    expect(await screen.findByRole("button", { name: "Approve" }, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Deny" })).toBeInTheDocument();
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

  it("lets operators hide and restore panes from the windows menu", async () => {
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

    await waitFor(() =>
      expect(screen.getByText("Activity ledger", { selector: ".cockpit-window-title" })).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByRole("button", { name: "Windows" }));
    const menu = screen.getByText("Show all").closest(".cockpit-window-launcher-drawer") as HTMLElement;
    const activityToggle = within(menu).getByText("Activity Ledger").closest("button") as HTMLButtonElement;
    fireEvent.click(activityToggle);

    await waitFor(() =>
      expect(screen.queryByText("Activity ledger", { selector: ".cockpit-window-title" })).not.toBeInTheDocument(),
    );

    const reopenedMenu = screen.getByText("Show all").closest(".cockpit-window-launcher-drawer") as HTMLElement;
    const activityRow = within(reopenedMenu).getByText("Activity Ledger").closest(".cockpit-windows-menu-row") as HTMLElement;
    fireEvent.click(within(activityRow).getByRole("button", { name: "Focus" }));

    const activityTitle = await screen.findByText("Activity ledger", { selector: ".cockpit-window-title" });
    expect(activityTitle).toBeInTheDocument();
    expect(activityTitle.closest(".cockpit-window")).toHaveClass("cockpit-window--active");
  });

  it("does not repack unrelated panes when toggling window visibility", async () => {
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

    const guardianTitle = await screen.findByText("Guardian state", { selector: ".cockpit-window-title" });

    act(() => {
      usePanelLayoutStore.getState().setRect("guardian_state_pane", { x: 222, y: 144 });
    });

    await waitFor(() => expect(guardianTitle.closest(".cockpit-window")).toHaveStyle({ left: "222px", top: "144px" }));

    fireEvent.click(screen.getByRole("button", { name: "Windows" }));
    const menu = screen.getByText("Show all").closest(".cockpit-window-launcher-drawer") as HTMLElement;
    fireEvent.click(within(menu).getByText("Priorities").closest("button") as HTMLButtonElement);

    await waitFor(() => expect(screen.queryByText("Priorities", { selector: ".cockpit-window-title" })).not.toBeInTheDocument());
    expect(guardianTitle.closest(".cockpit-window")).toHaveStyle({ left: "222px", top: "144px" });
  });

  it("hides a pane from its window close control", async () => {
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

    await waitFor(() =>
      expect(screen.getByText("Activity ledger", { selector: ".cockpit-window-title" })).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByLabelText("Hide Activity ledger"));

    await waitFor(() =>
      expect(screen.queryByText("Activity ledger", { selector: ".cockpit-window-title" })).not.toBeInTheDocument(),
    );
  });

  it("repacking the workspace keeps manually hidden panes hidden", async () => {
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

    await waitFor(() =>
      expect(screen.getByText("Activity ledger", { selector: ".cockpit-window-title" })).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByLabelText("Hide Activity ledger"));
    await waitFor(() =>
      expect(screen.queryByText("Activity ledger", { selector: ".cockpit-window-title" })).not.toBeInTheDocument(),
    );

    fireEvent.click(screen.getByRole("button", { name: "Reset view" }));

    await waitFor(() =>
      expect(screen.queryByText("Activity ledger", { selector: ".cockpit-window-title" })).not.toBeInTheDocument(),
    );
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
  }, 15000);

  it("surfaces workflow branch families and can continue the latest branch", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/sessions")) {
        return Promise.resolve(mockResponse([{ id: "session-1", title: "Session 1" }]));
      }
      if (url.includes("/api/goals/tree")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/dashboard")) {
        return Promise.resolve(mockResponse({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }));
      }
      if (url.includes("/api/observer/state")) return Promise.resolve(mockResponse({}));
      if (url.includes("/api/audit/events")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/approvals/pending")) return Promise.resolve(mockResponse([]));
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
          runs: [
            {
              id: "run-root",
              tool_name: "workflow_resume_review",
              workflow_name: "resume-review",
              session_id: "session-1",
              status: "succeeded",
              started_at: "2026-03-20T09:00:00Z",
              updated_at: "2026-03-20T09:05:00Z",
              summary: "root review workflow completed",
              step_tools: ["read_file"],
              artifact_paths: [],
              continued_error_steps: [],
              risk_level: "low",
              thread_id: "session-1",
              thread_label: "Session 1",
              run_identity: "resume-root-run",
              root_run_identity: "resume-root-run",
              branch_kind: "replay_from_start",
              branch_depth: 0,
              checkpoint_context_available: true,
              replay_allowed: true,
              replay_draft: 'Run workflow "resume-review" with file_path="notes/review.md".',
              timeline: [
                { kind: "workflow_started", at: "2026-03-20T09:00:00Z", summary: "Workflow started" },
                { kind: "workflow_succeeded", at: "2026-03-20T09:05:00Z", summary: "root review workflow completed" },
              ],
            },
            {
              id: "run-child",
              tool_name: "workflow_resume_review",
              workflow_name: "resume-review",
              session_id: "session-1",
              status: "degraded",
              started_at: "2026-03-20T09:06:00Z",
              updated_at: "2026-03-20T09:08:00Z",
              summary: "branch review needs continuation",
              step_tools: ["read_file"],
              artifact_paths: [],
              continued_error_steps: ["review_checkpoint"],
              risk_level: "low",
              thread_id: "session-1",
              thread_label: "Session 1",
              run_identity: "resume-child-run",
              parent_run_identity: "resume-root-run",
              root_run_identity: "resume-root-run",
              branch_kind: "branch_from_checkpoint",
              branch_depth: 1,
              resume_checkpoint_label: "review_checkpoint",
              checkpoint_context_available: true,
              replay_allowed: true,
              thread_continue_message: "Continue child branch from the review checkpoint.",
              retry_from_step_draft:
                'Run workflow "resume-review" with file_path="notes/review.md", _seraph_resume_from_step="review_checkpoint".',
              timeline: [
                { kind: "workflow_started", at: "2026-03-20T09:06:00Z", summary: "Branch workflow started" },
                { kind: "workflow_degraded", at: "2026-03-20T09:08:00Z", summary: "branch review needs continuation" },
              ],
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

    const rootSummary = await screen.findByText("root review workflow completed");
    const rootRow = rootSummary.closest(".cockpit-row");
    expect(rootRow).not.toBeNull();
    expect(within(rootRow as HTMLElement).getByText(/supervision branched/i)).toBeInTheDocument();
    expect(within(rootRow as HTMLElement).getByText(/1 child branch/i)).toBeInTheDocument();

    fireEvent.click(rootSummary);

    const inspector = document.querySelector(".cockpit-inspector") as HTMLElement;
    expect(within(inspector).getByRole("button", { name: "Open Latest Branch" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Continue Latest Branch" })).toBeInTheDocument();
    expect(within(inspector).getByText("child branch")).toBeInTheDocument();
    expect(within(inspector).getByText(/recovery ready/i)).toBeInTheDocument();

    fireEvent.click(within(inspector).getByRole("button", { name: "Continue Latest Branch" }));
    await waitFor(() =>
      expect(screen.getByDisplayValue("Continue child branch from the review checkpoint.")).toBeInTheDocument(),
    );

    fireEvent.click(within(inspector).getByRole("button", { name: "Open Latest Branch" }));
    await waitFor(() =>
      expect((inspector.querySelector(".cockpit-inspector-body") as HTMLElement).textContent).toContain("branch review needs continuation"),
    );
    expect(within(inspector).getByRole("button", { name: "Open Parent" })).toBeInTheDocument();
    expect(within(inspector).getByText("parent run")).toBeInTheDocument();
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
  }, 15000);

  it("routes packaged MCP test and toggle actions through extension connector endpoints", async () => {
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
            name: "github-packaged",
            enabled: false,
            url: "https://example.test/mcp",
            description: "Packaged GitHub MCP",
            status: "disconnected",
            status_message: null,
            tool_count: 0,
            has_headers: true,
            auth_hint: "Set GITHUB_TOKEN before enabling the connector",
            source: "extension",
            extension_id: "seraph.test-connector",
            extension_reference: "mcp/github.json",
            extension_display_name: "Test Connector",
            availability: "disabled",
            blocked_reason: "server_disabled",
            recommended_actions: [],
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
      if (url.includes("/api/extensions") && !url.includes("/source")) {
        return Promise.resolve(mockResponse({ extensions: [] }));
      }
      if (url.includes("/api/extensions/seraph.test-connector/connectors/test")) {
        return Promise.resolve(mockResponse({ status: "ok", tool_count: 2, tools: ["fetch_repo", "list_issues"] }));
      }
      if (url.includes("/api/extensions/seraph.test-connector/connectors/enabled")) {
        return Promise.resolve(mockResponse({
          status: "enabled",
          connector: { name: "github-packaged", enabled: true },
          extension: { id: "seraph.test-connector" },
          changed: { type: "mcp_server", name: "github-packaged", reference: "mcp/github.json", enabled: true, ok: true },
        }));
      }
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={() => {}} />);

    const packagedLabel = await screen.findByText("github-packaged");
    const packagedRow = packagedLabel.closest(".cockpit-operator-row");
    expect(packagedRow).not.toBeNull();

    fireEvent.click(within(packagedRow as HTMLElement).getByRole("button", { name: "Test github-packaged" }));
    fireEvent.click(within(packagedRow as HTMLElement).getByRole("button", { name: "Turn on github-packaged" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/extensions/seraph.test-connector/connectors/test"),
        expect.objectContaining({
          method: "POST",
          body: expect.stringContaining('"reference":"mcp/github.json"'),
        }),
      ),
    );

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/extensions/seraph.test-connector/connectors/enabled"),
        expect.objectContaining({
          method: "POST",
          body: expect.stringContaining('"enabled":true'),
        }),
      ),
    );

    expect(fetchMock.mock.calls.some(([input]) => String(input).includes("/api/mcp/servers/github-packaged"))).toBe(false);
  }, 15000);

  it("shows packaged MCP definitions as read-only in extension studio", async () => {
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
            name: "github-packaged",
            enabled: false,
            url: "https://example.test/mcp",
            description: "Packaged GitHub MCP",
            status: "disconnected",
            status_message: null,
            tool_count: 0,
            has_headers: true,
            auth_hint: "Set GITHUB_TOKEN before enabling the connector",
            source: "extension",
            extension_id: "seraph.test-connector",
            extension_reference: "mcp/github.json",
            extension_display_name: "Test Connector",
            availability: "disabled",
            blocked_reason: "server_disabled",
            recommended_actions: [],
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
      if (url.includes("/api/extensions") && !url.includes("/source")) {
        return Promise.resolve(mockResponse({ extensions: [] }));
      }
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={() => {}} />);

    const packagedLabel = await screen.findByText("github-packaged");
    const packagedRow = packagedLabel.closest(".cockpit-operator-row");
    expect(packagedRow).not.toBeNull();

    fireEvent.click(within(packagedRow as HTMLElement).getByRole("button", { name: "studio" }));

    const studio = await screen.findByLabelText("Extension studio");
    expect(within(studio).getByRole("button", { name: "Save config" })).toBeDisabled();
    expect(within(studio).getByRole("button", { name: "Validate config" })).toBeDisabled();
    expect(within(studio).getByLabelText("mcp url")).toBeDisabled();
    expect(within(studio).getByLabelText("description")).toBeDisabled();
    expect(within(studio).getByText(/Packaged MCP definitions are read-only here/i)).toBeInTheDocument();
  }, 15000);

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
  }, 15000);

  it("shows manifest entries for packaged extensions in the studio", async () => {
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
            name: "local-workflow",
            tool_name: "workflow_local_workflow",
            description: "Package-backed workflow",
            inputs: { file_path: { type: "string", description: "Workspace file", required: true } },
            requires_tools: ["read_file"],
            requires_skills: [],
            user_invocable: true,
            enabled: true,
            step_count: 1,
            file_path: "/tmp/workspace/extensions/seraph-test-installable/workflows/local-workflow.md",
            source: "manifest",
            extension_id: "seraph.test-installable",
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
      if (url.includes("/api/extensions") && !url.includes("/source")) {
        return Promise.resolve(mockResponse({
          extensions: [{
            id: "seraph.test-installable",
            display_name: "Test Installable",
            version: "2026.3.21",
            kind: "capability-pack",
            trust: "local",
            source: "manifest",
            location: "workspace",
            status: "ready",
            summary: "Test installable package",
            studio_files: [
              {
                key: "seraph.test-installable:manifest",
                role: "manifest",
                reference: "manifest.yaml",
                resolved_path: "/tmp/workspace/extensions/seraph-test-installable/manifest.yaml",
                label: "manifest.yaml",
                display_type: "manifest",
                format: "yaml",
                editable: true,
                save_supported: true,
                validation_supported: true,
                loaded: true,
                name: "Test Installable",
              },
              {
                key: "seraph.test-installable:workflows:workflows/local-workflow.md",
                role: "contribution",
                reference: "workflows/local-workflow.md",
                resolved_path: "/tmp/workspace/extensions/seraph-test-installable/workflows/local-workflow.md",
                label: "local-workflow",
                display_type: "workflow",
                contribution_type: "workflows",
                format: "markdown",
                editable: true,
                save_supported: true,
                validation_supported: true,
                loaded: true,
                name: "local-workflow",
              },
            ],
          }],
        }));
      }
      if (url.includes("/api/extensions/seraph.test-installable/source")) {
        return Promise.resolve(mockResponse({
          content: "id: seraph.test-installable\nversion: 2026.3.21\n",
          validation: {
            valid: true,
            manifest: { id: "seraph.test-installable", version: "2026.3.21" },
          },
        }));
      }
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={vi.fn()} />);

    await waitFor(() => expect(screen.getByRole("button", { name: "Extension studio" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Extension studio" }));

    const studio = await screen.findByLabelText("Extension studio");
    expect(within(studio).getByText("Test Installable")).toBeInTheDocument();

    fireEvent.click(within(studio).getAllByText("manifest.yaml")[0].closest("button") as HTMLButtonElement);

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/extensions/seraph.test-installable/source?reference=manifest.yaml"),
      ),
    );
    expect(within(studio).getByLabelText("manifest draft")).toBeInTheDocument();
  }, 15000);

  it("saves packaged workflow drafts through the extension source api", async () => {
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
            name: "local-workflow",
            tool_name: "workflow_local_workflow",
            description: "Package-backed workflow",
            inputs: { file_path: { type: "string", description: "Workspace file", required: true } },
            requires_tools: ["read_file"],
            requires_skills: [],
            user_invocable: true,
            enabled: true,
            step_count: 1,
            file_path: "/tmp/workspace/extensions/seraph-test-installable/workflows/local-workflow.md",
            source: "manifest",
            extension_id: "seraph.test-installable",
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
      if (url.includes("/api/extensions") && !url.includes("/source")) {
        return Promise.resolve(mockResponse({
          extensions: [{
            id: "seraph.test-installable",
            display_name: "Test Installable",
            version: "2026.3.21",
            kind: "capability-pack",
            trust: "local",
            source: "manifest",
            location: "workspace",
            status: "ready",
            summary: "Test installable package",
            studio_files: [
              {
                key: "seraph.test-installable:manifest",
                role: "manifest",
                reference: "manifest.yaml",
                resolved_path: "/tmp/workspace/extensions/seraph-test-installable/manifest.yaml",
                label: "manifest.yaml",
                display_type: "manifest",
                format: "yaml",
                editable: true,
                save_supported: true,
                validation_supported: true,
                loaded: true,
                name: "Test Installable",
              },
              {
                key: "seraph.test-installable:workflows:workflows/local-workflow.md",
                role: "contribution",
                reference: "workflows/local-workflow.md",
                resolved_path: "/tmp/workspace/extensions/seraph-test-installable/workflows/local-workflow.md",
                label: "local-workflow",
                display_type: "workflow",
                contribution_type: "workflows",
                format: "markdown",
                editable: true,
                save_supported: true,
                validation_supported: true,
                loaded: true,
                name: "local-workflow",
              },
            ],
          }],
        }));
      }
      if (url.includes("/api/extensions/seraph.test-installable/source") && init?.method === "POST") {
        return Promise.resolve(mockResponse({
          status: "saved",
          validation: {
            valid: true,
            workflow: { name: "local-workflow-updated" },
          },
        }));
      }
      if (url.includes("/api/extensions/seraph.test-installable/source")) {
        return Promise.resolve(mockResponse({
          content: "name: local-workflow\nsummary: package workflow draft",
          validation: { valid: true, workflow: { name: "local-workflow" } },
        }));
      }
      if (url.includes("/api/workflows/validate")) {
        return Promise.resolve(mockResponse({ valid: true, issues: [], warnings: [] }));
      }
      if (url.includes("/api/capabilities/preflight?target_type=workflow&name=local-workflow")) {
        return Promise.resolve(mockResponse({
          target_type: "workflow",
          name: "local-workflow",
          label: "local-workflow",
          description: "Package-backed workflow",
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
    fireEvent.change(draftInput, { target: { value: "name: local-workflow-updated\nsummary: package workflow draft" } });
    fireEvent.click(within(studio).getByRole("button", { name: "Save draft" }));

    await waitFor(() => {
      const saveCall = fetchMock.mock.calls.find(
        ([input, init]) => String(input).includes("/api/extensions/seraph.test-installable/source") && (init as RequestInit | undefined)?.method === "POST",
      );
      expect(saveCall).toBeDefined();
      const init = saveCall?.[1] as RequestInit | undefined;
      const body = JSON.parse(String(init?.body ?? "{}")) as { reference?: string; content?: string };
      expect(body.reference).toBe("workflows/local-workflow.md");
      expect(body.content).toContain("local-workflow-updated");
    });
  }, 15000);

  it("does not fall back to legacy source loading for packaged workflows when extension metadata is unavailable", async () => {
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
            name: "local-workflow",
            tool_name: "workflow_local_workflow",
            description: "Package-backed workflow",
            inputs: { file_path: { type: "string", description: "Workspace file", required: true } },
            requires_tools: ["read_file"],
            requires_skills: [],
            user_invocable: true,
            enabled: true,
            step_count: 1,
            file_path: "/tmp/workspace/extensions/seraph-test-installable/workflows/local-workflow.md",
            source: "manifest",
            extension_id: "seraph.test-installable",
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
      if (url.includes("/api/extensions") && !url.includes("/source")) {
        return Promise.resolve(mockResponse({ detail: "unavailable" }, false, 503));
      }
      if (url.includes("/api/workflows/local-workflow/source")) {
        return Promise.resolve(mockResponse({ content: "legacy workflow source" }));
      }
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={vi.fn()} />);

    await waitFor(() => expect(screen.getByRole("button", { name: "Extension studio" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Extension studio" }));

    const studio = await screen.findByLabelText("Extension studio");
    expect(await within(studio).findByText("Reload extension package metadata before editing local-workflow")).toBeInTheDocument();
    expect(within(studio).getByRole("button", { name: "Refresh validation" })).toBeDisabled();
    expect(within(studio).getByRole("button", { name: "Save draft" })).toBeDisabled();
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes("/api/workflows/local-workflow/source"))).toBe(false);
  }, 15000);

  it("clears stale extension package metadata after a failed package refresh", async () => {
    let extensionsHealthy = true;
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
            name: "local-workflow",
            tool_name: "workflow_local_workflow",
            description: "Package-backed workflow",
            inputs: { file_path: { type: "string", description: "Workspace file", required: true } },
            requires_tools: ["read_file"],
            requires_skills: [],
            user_invocable: true,
            enabled: true,
            step_count: 1,
            file_path: "/tmp/workspace/extensions/seraph-test-installable/workflows/local-workflow.md",
            source: "manifest",
            extension_id: "seraph.test-installable",
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
      if (url.includes("/api/extensions") && !url.includes("/source")) {
        if (!extensionsHealthy) {
          return Promise.resolve(mockResponse({ detail: "unavailable" }, false, 503));
        }
        return Promise.resolve(mockResponse({
          extensions: [{
            id: "seraph.test-installable",
            display_name: "Test Installable",
            version: "2026.3.21",
            kind: "capability-pack",
            trust: "local",
            source: "manifest",
            location: "workspace",
            status: "ready",
            summary: "Test installable package",
            studio_files: [
              {
                key: "seraph.test-installable:manifest",
                role: "manifest",
                reference: "manifest.yaml",
                resolved_path: "/tmp/workspace/extensions/seraph-test-installable/manifest.yaml",
                label: "manifest.yaml",
                display_type: "manifest",
                format: "yaml",
                editable: true,
                save_supported: true,
                validation_supported: true,
                loaded: true,
                name: "Test Installable",
              },
              {
                key: "seraph.test-installable:workflows:workflows/local-workflow.md",
                role: "contribution",
                reference: "workflows/local-workflow.md",
                resolved_path: "/tmp/workspace/extensions/seraph-test-installable/workflows/local-workflow.md",
                label: "local-workflow",
                display_type: "workflow",
                contribution_type: "workflows",
                format: "markdown",
                editable: true,
                save_supported: true,
                validation_supported: true,
                loaded: true,
                name: "local-workflow",
              },
            ],
          }],
        }));
      }
      if (url.includes("/api/extensions/seraph.test-installable/source")) {
        return Promise.resolve(mockResponse({
          content: "name: local-workflow\nsummary: package workflow draft",
          validation: { valid: true, workflow: { name: "local-workflow" } },
        }));
      }
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={vi.fn()} />);

    await waitFor(() => expect(screen.getByRole("button", { name: "Extension studio" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Extension studio" }));

    let studio = await screen.findByLabelText("Extension studio");
    expect(within(studio).getByText("Test Installable")).toBeInTheDocument();
    fireEvent.click(within(studio).getByRole("button", { name: "Close extension studio" }));

    extensionsHealthy = false;
    fireEvent.click(screen.getByRole("button", { name: "Extension studio" }));

    studio = await screen.findByLabelText("Extension studio");
    await waitFor(() => expect(within(studio).queryByText("Test Installable")).not.toBeInTheDocument());
    expect(within(studio).queryByText("manifest.yaml")).not.toBeInTheDocument();
    expect(within(studio).getByRole("button", { name: "Refresh validation" })).toBeDisabled();
    expect(within(studio).getByRole("button", { name: "Save draft" })).toBeDisabled();
  }, 15000);

  it("disables manifest actions when the backend marks the manifest read-only", async () => {
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
      if (url.includes("/api/workflows/runs")) return Promise.resolve(mockResponse({ runs: [] }));
      if (url.includes("/api/settings/tool-policy-mode")) return Promise.resolve(mockResponse({ mode: "balanced" }));
      if (url.includes("/api/settings/mcp-policy-mode")) return Promise.resolve(mockResponse({ mode: "approval" }));
      if (url.includes("/api/settings/approval-mode")) return Promise.resolve(mockResponse({ mode: "high_risk" }));
      if (url.includes("/api/extensions") && !url.includes("/source")) {
        return Promise.resolve(mockResponse({
          extensions: [{
            id: "seraph.test-installable",
            display_name: "Test Installable",
            version: "2026.3.21",
            kind: "capability-pack",
            trust: "bundled",
            source: "manifest",
            location: "bundled",
            status: "ready",
            summary: "Bundled manifest",
            studio_files: [{
              key: "seraph.test-installable:manifest",
              role: "manifest",
              reference: "manifest.yaml",
              resolved_path: "/tmp/workspace/extensions/seraph-test-installable/manifest.yaml",
              label: "manifest.yaml",
              display_type: "manifest",
              format: "yaml",
              editable: false,
              save_supported: false,
              validation_supported: false,
              loaded: true,
              name: "Test Installable",
            }],
          }],
        }));
      }
      if (url.includes("/api/extensions/seraph.test-installable/source")) {
        return Promise.resolve(mockResponse({
          content: "id: seraph.test-installable\nversion: 2026.3.21\n",
          validation: {
            valid: true,
            manifest: { id: "seraph.test-installable", version: "2026.3.21" },
          },
        }));
      }
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={vi.fn()} />);

    await waitFor(() => expect(screen.getByRole("button", { name: "Extension studio" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Extension studio" }));

    const studio = await screen.findByLabelText("Extension studio");
    expect(await within(studio).findByLabelText("manifest draft")).toBeInTheDocument();
    expect(within(studio).getByRole("button", { name: "Validate manifest" })).toBeDisabled();
    expect(within(studio).getByRole("button", { name: "Save manifest" })).toBeDisabled();
  }, 15000);

  it("installs extension packages from a local path in extension studio", async () => {
    let installed = false;
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
      if (url.includes("/api/workflows/runs")) return Promise.resolve(mockResponse({ runs: [] }));
      if (url.includes("/api/settings/tool-policy-mode")) return Promise.resolve(mockResponse({ mode: "balanced" }));
      if (url.includes("/api/settings/mcp-policy-mode")) return Promise.resolve(mockResponse({ mode: "approval" }));
      if (url.includes("/api/settings/approval-mode")) return Promise.resolve(mockResponse({ mode: "high_risk" }));
      if (url.includes("/api/extensions/install") && init?.method === "POST") {
        installed = true;
        return Promise.resolve(mockResponse({
          status: "installed",
          extension: {
            id: "seraph.test-installable",
            display_name: "Test Installable",
          },
        }));
      }
      if (url.includes("/api/extensions/validate") && init?.method === "POST") {
        return Promise.resolve(mockResponse({
          extension_id: "seraph.test-installable",
          display_name: "Test Installable",
          ok: true,
          results: [],
        }));
      }
      if (url.includes("/api/extensions") && !url.includes("/source")) {
        return Promise.resolve(mockResponse({
          extensions: installed
            ? [{
              id: "seraph.test-installable",
              display_name: "Test Installable",
              version: "2026.3.21",
              kind: "capability-pack",
              trust: "local",
              source: "manifest",
              location: "workspace",
              status: "ready",
              summary: "Installed package",
              issues: [],
              load_errors: [],
              toggle_targets: [],
              toggleable_contribution_types: [],
              passive_contribution_types: ["runbooks"],
              enable_supported: false,
              disable_supported: false,
              removable: true,
              enabled_scope: "none",
              configurable: false,
              metadata_supported: true,
              config_scope: "metadata_only",
              enabled: null,
              config: {},
              studio_files: [{
                key: "seraph.test-installable:manifest",
                role: "manifest",
                reference: "manifest.yaml",
                resolved_path: "/tmp/workspace/extensions/seraph-test-installable/manifest.yaml",
                label: "manifest.yaml",
                display_type: "manifest",
                format: "yaml",
                editable: true,
                save_supported: true,
                validation_supported: true,
                loaded: true,
                name: "Test Installable",
              }],
            }]
            : [],
        }));
      }
      if (url.includes("/api/extensions/seraph.test-installable/source")) {
        return Promise.resolve(mockResponse({
          content: "id: seraph.test-installable\nversion: 2026.3.21\n",
          validation: { valid: true, manifest: { id: "seraph.test-installable", version: "2026.3.21" } },
        }));
      }
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={vi.fn()} />);

    await waitFor(() => expect(screen.getByRole("button", { name: "Extension studio" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Extension studio" }));

    const studio = await screen.findByLabelText("Extension studio");
    fireEvent.change(within(studio).getByLabelText("Extension package path"), {
      target: { value: "/tmp/extensions/test-installable" },
    });
    fireEvent.click(within(studio).getByRole("button", { name: "Validate path" }));
    const installButton = await within(studio).findByRole("button", { name: "Install package" });
    await waitFor(() => expect(installButton).not.toBeDisabled());
    fireEvent.click(installButton);

    await waitFor(() => {
      const installCall = fetchMock.mock.calls.find(
        ([input, callInit]) => String(input).includes("/api/extensions/install") && (callInit as RequestInit | undefined)?.method === "POST",
      );
      expect(installCall).toBeDefined();
      const body = JSON.parse(String((installCall?.[1] as RequestInit | undefined)?.body ?? "{}")) as { path?: string };
      expect(body.path).toBe("/tmp/extensions/test-installable");
    });

    await waitFor(() => expect(within(studio).getAllByText("Test Installable").length).toBeGreaterThan(0));
  }, 15000);

  it("scaffolds a new skill pack from extension studio", async () => {
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
      if (url.includes("/api/workflows/runs")) return Promise.resolve(mockResponse({ runs: [] }));
      if (url.includes("/api/settings/tool-policy-mode")) return Promise.resolve(mockResponse({ mode: "balanced" }));
      if (url.includes("/api/settings/mcp-policy-mode")) return Promise.resolve(mockResponse({ mode: "approval" }));
      if (url.includes("/api/settings/approval-mode")) return Promise.resolve(mockResponse({ mode: "high_risk" }));
      if (url.includes("/api/extensions/scaffold") && init?.method === "POST") {
        return Promise.resolve(mockResponse({
          status: "scaffolded",
          path: "/tmp/seraph-test/extensions/research-pack",
          created_files: ["manifest.yaml", "skills/research-pack.md"],
          preview: {
            path: "/tmp/seraph-test/extensions/research-pack",
            extension_id: "seraph.research-pack",
            display_name: "Research Pack",
            ok: true,
            results: [],
          },
        }, true, 201));
      }
      if (url.includes("/api/extensions") && !url.includes("/source")) {
        return Promise.resolve(mockResponse({ extensions: [] }));
      }
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={vi.fn()} />);

    await waitFor(() => expect(screen.getByRole("button", { name: "Extension studio" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Extension studio" }));

    const studio = await screen.findByLabelText("Extension studio");
    fireEvent.change(within(studio).getByLabelText("New extension package name"), {
      target: { value: "research-pack" },
    });
    fireEvent.change(within(studio).getByLabelText("New extension display name"), {
      target: { value: "Research Pack" },
    });
    fireEvent.click(within(studio).getByRole("button", { name: "Scaffold skill pack" }));

    await waitFor(() => {
      const scaffoldCall = fetchMock.mock.calls.find(
        ([input, callInit]) => String(input).includes("/api/extensions/scaffold") && (callInit as RequestInit | undefined)?.method === "POST",
      );
      expect(scaffoldCall).toBeDefined();
      const body = JSON.parse(String((scaffoldCall?.[1] as RequestInit | undefined)?.body ?? "{}")) as { package_name?: string; display_name?: string; contributions?: string[] };
      expect(body.package_name).toBe("research-pack");
      expect(body.display_name).toBe("Research Pack");
      expect(body.contributions).toEqual(["skills"]);
    });

    await waitFor(() => {
      expect(within(studio).getByLabelText("Extension package path")).toHaveValue("/tmp/seraph-test/extensions/research-pack");
      expect(within(studio).getByText("Research Pack scaffolded with 2 files")).toBeInTheDocument();
    });
  }, 15000);

  it("surfaces scaffolded-invalid responses as warnings in extension studio", async () => {
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
      if (url.includes("/api/workflows/runs")) return Promise.resolve(mockResponse({ runs: [] }));
      if (url.includes("/api/settings/tool-policy-mode")) return Promise.resolve(mockResponse({ mode: "balanced" }));
      if (url.includes("/api/settings/mcp-policy-mode")) return Promise.resolve(mockResponse({ mode: "approval" }));
      if (url.includes("/api/settings/approval-mode")) return Promise.resolve(mockResponse({ mode: "high_risk" }));
      if (url.includes("/api/extensions/scaffold") && init?.method === "POST") {
        return Promise.resolve(mockResponse({
          status: "scaffolded_invalid",
          path: "/tmp/seraph-test/extensions/research-pack",
          created_files: ["manifest.yaml", "skills/research-pack.md"],
          preview: {
            path: "/tmp/seraph-test/extensions/research-pack",
            extension_id: "seraph.research-pack",
            display_name: "Research Pack",
            ok: false,
            results: [{ issues: [{ code: "invalid_frontmatter" }] }],
          },
        }, true, 201));
      }
      if (url.includes("/api/extensions") && !url.includes("/source")) {
        return Promise.resolve(mockResponse({ extensions: [] }));
      }
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={vi.fn()} />);

    await waitFor(() => expect(screen.getByRole("button", { name: "Extension studio" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Extension studio" }));

    const studio = await screen.findByLabelText("Extension studio");
    fireEvent.change(within(studio).getByLabelText("New extension package name"), {
      target: { value: "research-pack" },
    });
    fireEvent.change(within(studio).getByLabelText("New extension display name"), {
      target: { value: "Research Pack" },
    });
    fireEvent.click(within(studio).getByRole("button", { name: "Scaffold skill pack" }));

    await waitFor(() => {
      expect(within(studio).getByText("Research Pack scaffolded but needs fixes (1 issue)")).toBeInTheDocument();
    });
  }, 15000);

  it("surfaces approval-required install responses in extension studio", async () => {
    let approvalQueued = false;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/sessions")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/tree")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/dashboard")) {
        return Promise.resolve(mockResponse({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }));
      }
      if (url.includes("/api/observer/state")) return Promise.resolve(mockResponse({}));
      if (url.includes("/api/audit/events")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/approvals/pending")) {
        return Promise.resolve(mockResponse(
          approvalQueued
            ? [{
              id: "approval-extension-install",
              session_id: null,
              thread_id: null,
              thread_label: null,
              tool_name: "extension_install",
              risk_level: "high",
              status: "pending",
              summary: "Install Test Installable with high-risk capabilities",
              created_at: "2026-03-21T11:20:00Z",
              resume_message: null,
              extension_id: "seraph.test-installable",
              extension_display_name: "Test Installable",
              extension_action: "install",
              package_path: "/tmp/extensions/test-installable",
              lifecycle_boundaries: ["workspace_write"],
              permissions: { tool_names: ["write_file"] },
            }]
            : [],
        ));
      }
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
      if (url.includes("/api/workflows/runs")) return Promise.resolve(mockResponse({ runs: [] }));
      if (url.includes("/api/settings/tool-policy-mode")) return Promise.resolve(mockResponse({ mode: "balanced" }));
      if (url.includes("/api/settings/mcp-policy-mode")) return Promise.resolve(mockResponse({ mode: "approval" }));
      if (url.includes("/api/settings/approval-mode")) return Promise.resolve(mockResponse({ mode: "high_risk" }));
      if (url.includes("/api/extensions/install") && init?.method === "POST") {
        approvalQueued = true;
        return Promise.resolve(mockResponse({
          detail: {
            type: "approval_required",
            approval_id: "approval-extension-install",
            tool_name: "extension_install",
            risk_level: "high",
            message: "Install extension 'Test Installable' with access to high-risk capabilities. Approve it first, then retry the extension action.",
          },
        }, false, 409));
      }
      if (url.includes("/api/extensions/validate") && init?.method === "POST") {
        return Promise.resolve(mockResponse({
          extension_id: "seraph.test-installable",
          display_name: "Test Installable",
          ok: true,
          results: [],
        }));
      }
      if (url.includes("/api/extensions") && !url.includes("/source")) {
        return Promise.resolve(mockResponse({ extensions: [] }));
      }
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={vi.fn()} />);

    await waitFor(() => expect(screen.getByRole("button", { name: "Extension studio" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Extension studio" }));

    const studio = await screen.findByLabelText("Extension studio");
    fireEvent.change(within(studio).getByLabelText("Extension package path"), {
      target: { value: "/tmp/extensions/test-installable" },
    });
    fireEvent.click(within(studio).getByRole("button", { name: "Validate path" }));
    const installButton = await within(studio).findByRole("button", { name: "Install package" });
    await waitFor(() => expect(installButton).not.toBeDisabled());
    fireEvent.click(installButton);

    expect(await within(studio).findByText(/Review Pending approvals, then retry\./)).toBeInTheDocument();
    expect(await screen.findByText("Install Test Installable with high-risk capabilities")).toBeInTheDocument();
    expect(await screen.findByText("approval-extension-install")).toBeInTheDocument();
    expect(await screen.findByText("seraph.test-installable")).toBeInTheDocument();
    expect(await screen.findByText("/tmp/extensions/test-installable")).toBeInTheDocument();
    expect(await screen.findByText("lifecycle boundaries")).toBeInTheDocument();
    expect(await screen.findByText("permissions")).toBeInTheDocument();
  }, 15000);

  it("switches extension studio package action to update for installed workspace packages", async () => {
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
      if (url.includes("/api/workflows/runs")) return Promise.resolve(mockResponse({ runs: [] }));
      if (url.includes("/api/settings/tool-policy-mode")) return Promise.resolve(mockResponse({ mode: "balanced" }));
      if (url.includes("/api/settings/mcp-policy-mode")) return Promise.resolve(mockResponse({ mode: "approval" }));
      if (url.includes("/api/settings/approval-mode")) return Promise.resolve(mockResponse({ mode: "high_risk" }));
      if (url.includes("/api/extensions/validate") && init?.method === "POST") {
        return Promise.resolve(mockResponse({
          path: "/tmp/extensions/test-installable",
          extension_id: "seraph.test-installable",
          display_name: "Test Installable",
          version: "2026.4.01",
          ok: true,
          results: [],
          lifecycle_plan: {
            mode: "update_workspace",
            recommended_action: "update",
            install_allowed: false,
            update_supported: true,
            current_location: "workspace",
            current_version: "2026.3.21",
            current_source: "manifest",
            candidate_version: "2026.4.01",
            version_relation: "upgrade",
            package_changed: true,
          },
        }));
      }
      if (url.includes("/api/extensions/update") && init?.method === "POST") {
        return Promise.resolve(mockResponse({
          status: "updated",
          extension: {
            id: "seraph.test-installable",
            display_name: "Test Installable",
          },
        }));
      }
      if (url.includes("/api/extensions") && !url.includes("/source")) {
        return Promise.resolve(mockResponse({
          extensions: [{
            id: "seraph.test-installable",
            display_name: "Test Installable",
            version: "2026.4.01",
            kind: "capability-pack",
            trust: "local",
            source: "manifest",
            location: "workspace",
            status: "ready",
            summary: "Installed package",
            issues: [],
            load_errors: [],
            toggle_targets: [],
            toggleable_contribution_types: [],
            passive_contribution_types: ["runbooks"],
            enable_supported: false,
            disable_supported: false,
            removable: true,
            enabled_scope: "none",
            configurable: false,
            metadata_supported: true,
            config_scope: "metadata_only",
            enabled: null,
            config: {},
            studio_files: [{
              key: "seraph.test-installable:manifest",
              role: "manifest",
              reference: "manifest.yaml",
              resolved_path: "/tmp/workspace/extensions/seraph-test-installable/manifest.yaml",
              label: "manifest.yaml",
              display_type: "manifest",
              format: "yaml",
              editable: true,
              save_supported: true,
              validation_supported: true,
              loaded: true,
              name: "Test Installable",
            }],
          }],
        }));
      }
      if (url.includes("/api/extensions/seraph.test-installable/source")) {
        return Promise.resolve(mockResponse({
          content: "id: seraph.test-installable\nversion: 2026.4.01\n",
          validation: { valid: true, manifest: { id: "seraph.test-installable", version: "2026.4.01" } },
        }));
      }
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={vi.fn()} />);

    await waitFor(() => expect(screen.getByRole("button", { name: "Extension studio" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Extension studio" }));

    const studio = await screen.findByLabelText("Extension studio");
    fireEvent.change(within(studio).getByLabelText("Extension package path"), {
      target: { value: "/tmp/extensions/test-installable" },
    });
    fireEvent.click(within(studio).getByRole("button", { name: "Validate path" }));

    await waitFor(() => expect(within(studio).getByRole("button", { name: "Update package" })).toBeInTheDocument());
    fireEvent.click(within(studio).getByRole("button", { name: "Update package" }));

    await waitFor(() => {
      const updateCall = fetchMock.mock.calls.find(
        ([input, callInit]) => String(input).includes("/api/extensions/update") && (callInit as RequestInit | undefined)?.method === "POST",
      );
      expect(updateCall).toBeDefined();
      const body = JSON.parse(String((updateCall?.[1] as RequestInit | undefined)?.body ?? "{}")) as { path?: string };
      expect(body.path).toBe("/tmp/extensions/test-installable");
    });
  }, 15000);

  it("surfaces approval-required update responses in extension studio", async () => {
    let approvalQueued = false;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/sessions")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/tree")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/dashboard")) {
        return Promise.resolve(mockResponse({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }));
      }
      if (url.includes("/api/observer/state")) return Promise.resolve(mockResponse({}));
      if (url.includes("/api/audit/events")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/approvals/pending")) {
        return Promise.resolve(mockResponse(
          approvalQueued
            ? [{
              id: "approval-extension-update",
              session_id: null,
              thread_id: null,
              thread_label: null,
              tool_name: "extension_update",
              risk_level: "high",
              status: "pending",
              summary: "Update Test Installable with high-risk capabilities",
              created_at: "2026-03-21T11:21:00Z",
              resume_message: null,
            }]
            : [],
        ));
      }
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
      if (url.includes("/api/workflows/runs")) return Promise.resolve(mockResponse({ runs: [] }));
      if (url.includes("/api/settings/tool-policy-mode")) return Promise.resolve(mockResponse({ mode: "balanced" }));
      if (url.includes("/api/settings/mcp-policy-mode")) return Promise.resolve(mockResponse({ mode: "approval" }));
      if (url.includes("/api/settings/approval-mode")) return Promise.resolve(mockResponse({ mode: "high_risk" }));
      if (url.includes("/api/extensions/validate") && init?.method === "POST") {
        return Promise.resolve(mockResponse({
          path: "/tmp/extensions/test-installable",
          extension_id: "seraph.test-installable",
          display_name: "Test Installable",
          version: "2026.4.01",
          ok: true,
          results: [],
          lifecycle_plan: {
            mode: "update_workspace",
            recommended_action: "update",
            install_allowed: false,
            update_supported: true,
            current_location: "workspace",
            current_version: "2026.3.21",
            current_source: "manifest",
            candidate_version: "2026.4.01",
            version_relation: "upgrade",
            package_changed: true,
          },
        }));
      }
      if (url.includes("/api/extensions/update") && init?.method === "POST") {
        approvalQueued = true;
        return Promise.resolve(mockResponse({
          detail: {
            type: "approval_required",
            approval_id: "approval-extension-update",
            tool_name: "extension_update",
            risk_level: "high",
            message: "Update extension 'Test Installable' with access to high-risk capabilities. Approve it first, then retry the extension action.",
          },
        }, false, 409));
      }
      if (url.includes("/api/extensions") && !url.includes("/source")) {
        return Promise.resolve(mockResponse({
          extensions: [{
            id: "seraph.test-installable",
            display_name: "Test Installable",
            version: "2026.3.21",
            kind: "capability-pack",
            trust: "local",
            source: "manifest",
            location: "workspace",
            status: "ready",
            summary: "Installed package",
            issues: [],
            load_errors: [],
            toggle_targets: [],
            toggleable_contribution_types: [],
            passive_contribution_types: ["runbooks"],
            enable_supported: false,
            disable_supported: false,
            removable: true,
            enabled_scope: "none",
            configurable: false,
            metadata_supported: true,
            config_scope: "metadata_only",
            enabled: null,
            config: {},
            studio_files: [{
              key: "seraph.test-installable:manifest",
              role: "manifest",
              reference: "manifest.yaml",
              resolved_path: "/tmp/workspace/extensions/seraph-test-installable/manifest.yaml",
              label: "manifest.yaml",
              display_type: "manifest",
              format: "yaml",
              editable: true,
              save_supported: true,
              validation_supported: true,
              loaded: true,
              name: "Test Installable",
            }],
          }],
        }));
      }
      if (url.includes("/api/extensions/seraph.test-installable/source")) {
        return Promise.resolve(mockResponse({
          content: "id: seraph.test-installable\nversion: 2026.3.21\n",
          validation: { valid: true, manifest: { id: "seraph.test-installable", version: "2026.3.21" } },
        }));
      }
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={vi.fn()} />);

    await waitFor(() => expect(screen.getByRole("button", { name: "Extension studio" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Extension studio" }));

    const studio = await screen.findByLabelText("Extension studio");
    fireEvent.change(within(studio).getByLabelText("Extension package path"), {
      target: { value: "/tmp/extensions/test-installable" },
    });
    fireEvent.click(within(studio).getByRole("button", { name: "Validate path" }));

    await waitFor(() => expect(within(studio).getByRole("button", { name: "Update package" })).toBeInTheDocument());
    fireEvent.click(within(studio).getByRole("button", { name: "Update package" }));

    expect(await within(studio).findByText(/Review Pending approvals, then retry\./)).toBeInTheDocument();
    expect(await screen.findByText("Update Test Installable with high-risk capabilities")).toBeInTheDocument();
    expect(await screen.findByText("approval-extension-update")).toBeInTheDocument();
  }, 15000);

  it("disables package install and update actions for invalid extension previews", async () => {
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
      if (url.includes("/api/workflows/runs")) return Promise.resolve(mockResponse({ runs: [] }));
      if (url.includes("/api/settings/tool-policy-mode")) return Promise.resolve(mockResponse({ mode: "balanced" }));
      if (url.includes("/api/settings/mcp-policy-mode")) return Promise.resolve(mockResponse({ mode: "approval" }));
      if (url.includes("/api/settings/approval-mode")) return Promise.resolve(mockResponse({ mode: "high_risk" }));
      if (url.includes("/api/extensions/validate") && init?.method === "POST") {
        return Promise.resolve(mockResponse({
          path: "/tmp/extensions/test-installable",
          extension_id: "seraph.test-installable",
          display_name: "Test Installable",
          version: "2026.4.01",
          ok: false,
          results: [{ issues: [{ message: "broken workflow" }] }],
          load_errors: [{ source: "manifest.yaml", message: "manifest mismatch" }],
          lifecycle_plan: {
            mode: "update_workspace",
            recommended_action: "update",
            install_allowed: false,
            update_supported: true,
            current_location: "workspace",
            current_version: "2026.3.21",
            current_source: "manifest",
            candidate_version: "2026.4.01",
            version_relation: "upgrade",
            package_changed: true,
          },
        }));
      }
      if (url.includes("/api/extensions") && !url.includes("/source")) {
        return Promise.resolve(mockResponse({ extensions: [] }));
      }
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={vi.fn()} />);

    await waitFor(() => expect(screen.getByRole("button", { name: "Extension studio" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Extension studio" }));

    const studio = await screen.findByLabelText("Extension studio");
    fireEvent.change(within(studio).getByLabelText("Extension package path"), {
      target: { value: "/tmp/extensions/test-installable" },
    });
    fireEvent.click(within(studio).getByRole("button", { name: "Validate path" }));

    const actionButton = await within(studio).findByRole("button", { name: "Update package" });
    await waitFor(() => expect(actionButton).toBeDisabled());
    expect(within(studio).getByText(/failed validation/i)).toBeInTheDocument();
  }, 15000);

  it("exposes extension lifecycle controls for manifest entries", async () => {
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
            name: "local-workflow",
            tool_name: "workflow_local_workflow",
            description: "Package-backed workflow",
            inputs: { file_path: { type: "string", description: "Workspace file", required: true } },
            requires_tools: ["read_file"],
            requires_skills: [],
            user_invocable: true,
            enabled: true,
            step_count: 1,
            file_path: "/tmp/workspace/extensions/seraph-test-installable/workflows/local-workflow.md",
            source: "manifest",
            extension_id: "seraph.test-installable",
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
      if (url.includes("/api/extensions/seraph.test-installable/configure") && init?.method === "POST") {
        return Promise.resolve(mockResponse({
          status: "configured",
          extension: { id: "seraph.test-installable" },
        }));
      }
      if (url.includes("/api/extensions/seraph.test-installable/disable") && init?.method === "POST") {
        return Promise.resolve(mockResponse({
          status: "disabled",
          extension: { id: "seraph.test-installable", enabled: false },
          changed: [],
        }));
      }
      if (url.includes("/api/extensions") && !url.includes("/source")) {
        return Promise.resolve(mockResponse({
          extensions: [{
            id: "seraph.test-installable",
            display_name: "Test Installable",
            version: "2026.3.21",
            kind: "capability-pack",
            trust: "local",
            source: "manifest",
            location: "workspace",
            status: "ready",
            summary: "Managed package",
            issues: [{ code: "permission_mismatch", severity: "error", message: "Manifest permissions are missing required workflow tools" }],
            load_errors: [{ source: "/tmp/package/workflows/local-workflow.md", message: "workflow failed to load", phase: "manifest", details: [] }],
            toggle_targets: [{ type: "workflow", name: "local-workflow" }],
            toggleable_contribution_types: ["workflows"],
            passive_contribution_types: ["runbooks"],
            enable_supported: false,
            disable_supported: true,
            removable: true,
            enabled_scope: "toggleable_contributions",
            configurable: false,
            metadata_supported: true,
            config_scope: "metadata_only",
            enabled: true,
            config: { mode: "focus" },
            studio_files: [
              {
                key: "seraph.test-installable:manifest",
                role: "manifest",
                reference: "manifest.yaml",
                resolved_path: "/tmp/workspace/extensions/seraph-test-installable/manifest.yaml",
                label: "manifest.yaml",
                display_type: "manifest",
                format: "yaml",
                editable: true,
                save_supported: true,
                validation_supported: true,
                loaded: true,
                name: "Test Installable",
              },
              {
                key: "seraph.test-installable:workflows:workflows/local-workflow.md",
                role: "contribution",
                reference: "workflows/local-workflow.md",
                resolved_path: "/tmp/workspace/extensions/seraph-test-installable/workflows/local-workflow.md",
                label: "local-workflow",
                display_type: "workflow",
                contribution_type: "workflows",
                format: "markdown",
                editable: true,
                save_supported: true,
                validation_supported: true,
                loaded: true,
                name: "local-workflow",
              },
            ],
          }],
        }));
      }
      if (url.includes("/api/extensions/seraph.test-installable/source")) {
        return Promise.resolve(mockResponse({
          content: "id: seraph.test-installable\nversion: 2026.3.21\n",
          validation: { valid: true, manifest: { id: "seraph.test-installable", version: "2026.3.21" } },
        }));
      }
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={vi.fn()} />);

    await waitFor(() => expect(screen.getByRole("button", { name: "Extension studio" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Extension studio" }));

    const studio = await screen.findByLabelText("Extension studio");
    fireEvent.click(within(studio).getAllByText("manifest.yaml")[0].closest("button") as HTMLButtonElement);
    expect(await within(studio).findByLabelText("package metadata")).toBeInTheDocument();
    expect(within(studio).getByText("Manifest permissions are missing required workflow tools")).toBeInTheDocument();
    expect(within(studio).getByText("workflow failed to load")).toBeInTheDocument();

    fireEvent.change(within(studio).getByLabelText("package metadata"), {
      target: { value: "{\n  \"mode\": \"review\",\n  \"budget\": \"low\"\n}" },
    });
    fireEvent.click(within(studio).getByRole("button", { name: "Save metadata" }));

    await waitFor(() => {
      const configureCall = fetchMock.mock.calls.find(
        ([input, callInit]) => String(input).includes("/api/extensions/seraph.test-installable/configure") && (callInit as RequestInit | undefined)?.method === "POST",
      );
      expect(configureCall).toBeDefined();
      const body = JSON.parse(String((configureCall?.[1] as RequestInit | undefined)?.body ?? "{}")) as { config?: Record<string, unknown> };
      expect(body.config).toEqual({ mode: "review", budget: "low" });
    });

    fireEvent.click(within(studio).getByRole("button", { name: "Disable contributions" }));

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(
          ([input, callInit]) => String(input).includes("/api/extensions/seraph.test-installable/disable") && (callInit as RequestInit | undefined)?.method === "POST",
        ),
      ).toBe(true);
    });
  }, 15000);

  it("surfaces approval-required enable actions for manifest entries in extension studio", async () => {
    let approvalQueued = false;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/sessions")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/tree")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/dashboard")) {
        return Promise.resolve(mockResponse({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }));
      }
      if (url.includes("/api/observer/state")) return Promise.resolve(mockResponse({}));
      if (url.includes("/api/audit/events")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/approvals/pending")) {
        return Promise.resolve(mockResponse(
          approvalQueued
            ? [{
              id: "approval-extension-enable",
              session_id: null,
              thread_id: null,
              thread_label: null,
              tool_name: "extension_enable",
              risk_level: "high",
              status: "pending",
              summary: "Enable Test Installable contributions with high-risk capabilities",
              created_at: "2026-03-21T11:22:00Z",
              resume_message: null,
              extension_id: "seraph.test-installable",
              extension_display_name: "Test Installable",
              extension_action: "enable",
              package_path: "/tmp/workspace/extensions/seraph-test-installable",
              lifecycle_boundaries: ["workspace_write"],
              permissions: { tool_names: ["write_file"] },
            }]
            : [],
        ));
      }
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
            name: "local-workflow",
            tool_name: "workflow_local_workflow",
            description: "Package-backed workflow",
            inputs: { file_path: { type: "string", description: "Workspace file", required: true } },
            requires_tools: ["read_file"],
            requires_skills: [],
            user_invocable: true,
            enabled: true,
            step_count: 1,
            file_path: "/tmp/workspace/extensions/seraph-test-installable/workflows/local-workflow.md",
            source: "manifest",
            extension_id: "seraph.test-installable",
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
      if (url.includes("/api/extensions/seraph.test-installable/enable") && init?.method === "POST") {
        approvalQueued = true;
        return Promise.resolve(mockResponse({
          detail: {
            type: "approval_required",
            approval_id: "approval-extension-enable",
            tool_name: "extension_enable",
            risk_level: "high",
            message: "Enable extension 'Test Installable' with access to high-risk capabilities. Approve it first, then retry the extension action.",
          },
        }, false, 409));
      }
      if (url.includes("/api/extensions") && !url.includes("/source")) {
        return Promise.resolve(mockResponse({
          extensions: [{
            id: "seraph.test-installable",
            display_name: "Test Installable",
            version: "2026.3.21",
            kind: "capability-pack",
            trust: "local",
            source: "manifest",
            location: "workspace",
            status: "ready",
            summary: "Managed package",
            issues: [],
            load_errors: [],
            toggle_targets: [{ type: "workflow", name: "local-workflow" }],
            toggleable_contribution_types: ["workflows"],
            passive_contribution_types: ["runbooks"],
            enable_supported: true,
            disable_supported: false,
            removable: true,
            enabled_scope: "toggleable_contributions",
            configurable: false,
            metadata_supported: true,
            config_scope: "metadata_only",
            enabled: false,
            config: { mode: "focus" },
            studio_files: [
              {
                key: "seraph.test-installable:manifest",
                role: "manifest",
                reference: "manifest.yaml",
                resolved_path: "/tmp/workspace/extensions/seraph-test-installable/manifest.yaml",
                label: "manifest.yaml",
                display_type: "manifest",
                format: "yaml",
                editable: true,
                save_supported: true,
                validation_supported: true,
                loaded: true,
                name: "Test Installable",
              },
              {
                key: "seraph.test-installable:workflows:workflows/local-workflow.md",
                role: "contribution",
                reference: "workflows/local-workflow.md",
                resolved_path: "/tmp/workspace/extensions/seraph-test-installable/workflows/local-workflow.md",
                label: "local-workflow",
                display_type: "workflow",
                contribution_type: "workflows",
                format: "markdown",
                editable: true,
                save_supported: true,
                validation_supported: true,
                loaded: true,
                name: "local-workflow",
              },
            ],
          }],
        }));
      }
      if (url.includes("/api/extensions/seraph.test-installable/source")) {
        return Promise.resolve(mockResponse({
          content: "id: seraph.test-installable\nversion: 2026.3.21\n",
          validation: { valid: true, manifest: { id: "seraph.test-installable", version: "2026.3.21" } },
        }));
      }
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={vi.fn()} />);

    await waitFor(() => expect(screen.getByRole("button", { name: "Extension studio" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Extension studio" }));

    const studio = await screen.findByLabelText("Extension studio");
    fireEvent.click(within(studio).getAllByText("manifest.yaml")[0].closest("button") as HTMLButtonElement);
    fireEvent.click(await within(studio).findByRole("button", { name: "Enable contributions" }));

    expect(await within(studio).findByText(/Review Pending approvals, then retry\./)).toBeInTheDocument();
    expect(await screen.findByText("Enable Test Installable contributions with high-risk capabilities")).toBeInTheDocument();
    expect(await screen.findByText("approval-extension-enable")).toBeInTheDocument();
  }, 15000);

  it("preserves unsaved package metadata drafts across package refreshes", async () => {
    let extensionRefreshCount = 0;
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
      if (url.includes("/api/observer/continuity")) {
        return Promise.resolve(mockResponse({
          daemon: { connected: false, pending_notification_count: 0, capture_mode: "balanced" },
          notifications: [],
          queued_insights: [],
          queued_insight_count: 0,
          recent_interventions: [],
        }));
      }
      if (url.includes("/api/activity/ledger")) {
        return Promise.resolve(mockResponse({ items: [], summary: { total_items: 0, visible_groups: 0 } }));
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
            name: "local-workflow",
            tool_name: "workflow_local_workflow",
            description: "Package-backed workflow",
            inputs: {},
            requires_tools: [],
            requires_skills: [],
            user_invocable: true,
            enabled: true,
            step_count: 1,
            file_path: "/tmp/workspace/extensions/seraph-test-installable/workflows/local-workflow.md",
            source: "manifest",
            extension_id: "seraph.test-installable",
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
      if (url.includes("/api/extensions/seraph.test-installable/disable") && init?.method === "POST") {
        return Promise.resolve(mockResponse({
          status: "disabled",
          extension: { id: "seraph.test-installable", enabled: false },
          changed: [],
        }));
      }
      if (url.includes("/api/extensions") && !url.includes("/source")) {
        extensionRefreshCount += 1;
        return Promise.resolve(mockResponse({
          extensions: [{
            id: "seraph.test-installable",
            display_name: "Test Installable",
            version: "2026.3.21",
            kind: "capability-pack",
            trust: "local",
            source: "manifest",
            location: "workspace",
            status: "ready",
            summary: "Managed package",
            issues: [],
            load_errors: [],
            toggle_targets: [{ type: "workflow", name: "local-workflow" }],
            toggleable_contribution_types: ["workflows"],
            passive_contribution_types: [],
            enable_supported: true,
            disable_supported: true,
            removable: true,
            enabled_scope: "toggleable_contributions",
            configurable: false,
            metadata_supported: true,
            config_scope: "metadata_only",
            enabled: true,
            config: extensionRefreshCount >= 2 ? { mode: "server" } : { mode: "focus" },
            studio_files: [
              {
                key: "seraph.test-installable:manifest",
                role: "manifest",
                reference: "manifest.yaml",
                resolved_path: "/tmp/workspace/extensions/seraph-test-installable/manifest.yaml",
                label: "manifest.yaml",
                display_type: "manifest",
                format: "yaml",
                editable: true,
                save_supported: true,
                validation_supported: true,
                loaded: true,
                name: "Test Installable",
              },
            ],
          }],
        }));
      }
      if (url.includes("/api/extensions/seraph.test-installable/source")) {
        return Promise.resolve(mockResponse({
          content: "id: seraph.test-installable\nversion: 2026.3.21\n",
          validation: { valid: true, manifest: { id: "seraph.test-installable", version: "2026.3.21" } },
        }));
      }
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={vi.fn()} />);

    await waitFor(() => expect(screen.getByRole("button", { name: "Extension studio" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Extension studio" }));

    const studio = await screen.findByLabelText("Extension studio");
    fireEvent.click(within(studio).getAllByText("manifest.yaml")[0].closest("button") as HTMLButtonElement);

    const metadataField = await within(studio).findByLabelText("package metadata");
    fireEvent.change(metadataField, {
      target: { value: "{\n  \"mode\": \"draft\"\n}" },
    });

    fireEvent.click(within(studio).getByRole("button", { name: "Disable contributions" }));

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(
          ([input, callInit]) => String(input).includes("/api/extensions/seraph.test-installable/disable") && (callInit as RequestInit | undefined)?.method === "POST",
        ),
      ).toBe(true);
    });
    await waitFor(() => expect(extensionRefreshCount).toBeGreaterThanOrEqual(2));
    expect(within(studio).getByLabelText("package metadata")).toHaveValue("{\n  \"mode\": \"draft\"\n}");
  }, 15000);

  it("removes workspace extension packages from extension studio", async () => {
    let removed = false;
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
      if (url.includes("/api/workflows/runs")) return Promise.resolve(mockResponse({ runs: [] }));
      if (url.includes("/api/settings/tool-policy-mode")) return Promise.resolve(mockResponse({ mode: "balanced" }));
      if (url.includes("/api/settings/mcp-policy-mode")) return Promise.resolve(mockResponse({ mode: "approval" }));
      if (url.includes("/api/settings/approval-mode")) return Promise.resolve(mockResponse({ mode: "high_risk" }));
      if (url.includes("/api/extensions/seraph.test-installable") && init?.method === "DELETE") {
        removed = true;
        return Promise.resolve(mockResponse({ status: "removed", name: "seraph.test-installable" }));
      }
      if (url.includes("/api/extensions") && !url.includes("/source")) {
        return Promise.resolve(mockResponse({
          extensions: removed
            ? []
            : [{
              id: "seraph.test-installable",
              display_name: "Test Installable",
              version: "2026.3.21",
              kind: "capability-pack",
              trust: "local",
              source: "manifest",
              location: "workspace",
              status: "ready",
              summary: "Removable package",
              issues: [],
              load_errors: [],
              toggle_targets: [],
              toggleable_contribution_types: [],
              passive_contribution_types: [],
              enable_supported: false,
              disable_supported: false,
              removable: true,
              enabled_scope: "none",
              configurable: false,
              metadata_supported: true,
              config_scope: "metadata_only",
              enabled: null,
              config: {},
              studio_files: [{
                key: "seraph.test-installable:manifest",
                role: "manifest",
                reference: "manifest.yaml",
                resolved_path: "/tmp/workspace/extensions/seraph-test-installable/manifest.yaml",
                label: "manifest.yaml",
                display_type: "manifest",
                format: "yaml",
                editable: true,
                save_supported: true,
                validation_supported: true,
                loaded: true,
                name: "Test Installable",
              }],
            }],
        }));
      }
      if (url.includes("/api/extensions/seraph.test-installable/source")) {
        return Promise.resolve(mockResponse({
          content: "id: seraph.test-installable\nversion: 2026.3.21\n",
          validation: { valid: true, manifest: { id: "seraph.test-installable", version: "2026.3.21" } },
        }));
      }
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={vi.fn()} />);

    await waitFor(() => expect(screen.getByRole("button", { name: "Extension studio" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Extension studio" }));

    const studio = await screen.findByLabelText("Extension studio");
    fireEvent.click(within(studio).getAllByText("manifest.yaml")[0].closest("button") as HTMLButtonElement);
    fireEvent.click(await within(studio).findByRole("button", { name: "Remove package" }));

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(
          ([input, callInit]) => String(input).includes("/api/extensions/seraph.test-installable") && (callInit as RequestInit | undefined)?.method === "DELETE",
        ),
      ).toBe(true);
    });
    await waitFor(() => expect(within(studio).queryByText("Test Installable")).not.toBeInTheDocument());
  }, 15000);

  it("keeps the last successful activity ledger visible when a refresh fails", async () => {
    let activityLedgerCalls = 0;
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
      if (url.includes("/api/skills/reload")) return Promise.resolve(mockResponse({ ok: true }));
      if (url.includes("/api/activity/ledger")) {
        activityLedgerCalls += 1;
        if (activityLedgerCalls === 1) {
          return Promise.resolve(mockResponse({
            summary: {
              window_hours: 24,
              started_at: "2026-03-24T10:00:00Z",
              total_items: 1,
              visible_groups: 1,
              llm_calls: 1,
              spend_usd: 0.015,
              input_tokens: 200,
              output_tokens: 40,
              user_triggered_llm_calls: 1,
              autonomous_llm_calls: 0,
              llm_cost_by_runtime_path: [
                { key: "chat_agent", calls: 1, cost_usd: 0.015, input_tokens: 200, output_tokens: 40 },
              ],
              llm_cost_by_capability_family: [
                { key: "conversation", calls: 1, cost_usd: 0.015, input_tokens: 200, output_tokens: 40 },
              ],
              categories: { llm: 1, workflow: 0, approval: 0, guardian: 0, agent: 0, system: 0 },
            },
            items: [
              {
                id: "llm-1",
                kind: "llm_call",
                category: "llm",
                title: "Conversation reasoning for Session 1 using claude-sonnet-4",
                summary: "Initial activity snapshot",
                status: "success",
                created_at: "2026-03-24T10:01:00Z",
                updated_at: "2026-03-24T10:01:00Z",
                thread_id: "session-1",
                thread_label: "Session 1",
                source: "websocket_chat",
                model: "openrouter/anthropic/claude-sonnet-4",
                provider: "openrouter",
                prompt_tokens: 200,
                completion_tokens: 40,
                cost_usd: 0.015,
                duration_ms: 620,
                metadata: {
                  request_id: "agent-ws:session-1:refresh",
                  runtime_path: "chat_agent",
                  capability_family: "conversation",
                },
              },
            ],
          }));
        }
        return Promise.reject(new Error("activity ledger unavailable"));
      }
      if (url.includes("/api/workflows/runs")) return Promise.resolve(mockResponse({ runs: [] }));
      if (url.includes("/api/settings/tool-policy-mode")) return Promise.resolve(mockResponse({ mode: "balanced" }));
      if (url.includes("/api/settings/mcp-policy-mode")) return Promise.resolve(mockResponse({ mode: "approval" }));
      if (url.includes("/api/settings/approval-mode")) return Promise.resolve(mockResponse({ mode: "high_risk" }));
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={vi.fn()} />);

    await waitFor(() => expect(screen.getByText("Daily operator rhythm")).toBeInTheDocument());
    expect(await screen.findByText(/spend \$0\.015/)).toBeInTheDocument();
    expect(screen.getByText(/conversation \$0\.015/)).toBeInTheDocument();

    fireEvent.click(screen.getAllByRole("button", { name: "reload" })[0]);

    await waitFor(() => expect(activityLedgerCalls).toBeGreaterThan(1));
    expect(screen.getByText(/spend \$0\.015/)).toBeInTheDocument();
    expect(screen.getByText(/conversation \$0\.015/)).toBeInTheDocument();
    expect(screen.getByText("Daily operator rhythm")).toBeInTheDocument();
  }, 15000);

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
  }, 15000);

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

    await waitFor(() => expect(cockpitFetchCount).toBe(12));
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
  }, 15000);

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
