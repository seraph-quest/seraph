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
            reach: {
              route_statuses: [
                {
                  route: "live_delivery",
                  label: "Live delivery",
                  status: "fallback_active",
                  selected_transport: "native_notification",
                  repair_hint: "Keep a cockpit tab connected or route this delivery class to native notifications first.",
                  summary: "Live delivery is falling back to native notification because no live browser session is connected for websocket delivery.",
                },
                {
                  route: "alert_delivery",
                  label: "Alert delivery",
                  status: "ready",
                  selected_transport: "native_notification",
                  repair_hint: null,
                  summary: "Alert delivery will use native notification.",
                },
                {
                  route: "scheduled_delivery",
                  label: "Scheduled delivery",
                  status: "unavailable",
                  selected_transport: null,
                  repair_hint: "Reconnect the native daemon or route this delivery class to websocket first.",
                  summary: "Scheduled delivery has no available transport.",
                },
                {
                  route: "bundle_delivery",
                  label: "Bundle delivery",
                  status: "unavailable",
                  selected_transport: null,
                  repair_hint: "Reconnect the native daemon or route this delivery class to websocket first.",
                  summary: "Bundle delivery has no available transport.",
                },
              ],
            },
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
    expect(screen.getByText(/Bundle delivery: unavailable/i)).toBeInTheDocument();
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
    expect(screen.getAllByText(/web_search succeeded · 2 web results/)).not.toHaveLength(0);
    expect(screen.getByRole("button", { name: "Retry step" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Branch web_search" })).toBeInTheDocument();
    expect(screen.getAllByText("Use Output")).not.toHaveLength(0);
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

  it("surfaces active triage for approvals, workflows, queued guardian items, and reach failures", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/sessions")) {
        return Promise.resolve(mockResponse([
          { id: "session-1", title: "Session 1", created_at: "", updated_at: "", last_message: null, last_message_role: null },
          { id: "session-2", title: "Atlas thread", created_at: "", updated_at: "", last_message: null, last_message_role: null },
        ]));
      }
      if (url.includes("/api/goals/tree")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/dashboard")) {
        return Promise.resolve(mockResponse({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }));
      }
      if (url.includes("/api/observer/state")) return Promise.resolve(mockResponse({}));
      if (url.includes("/api/audit/events")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/approvals/approval-run/approve")) return Promise.resolve(mockResponse({ status: "approved" }));
      if (url.includes("/api/approvals/pending")) {
        return Promise.resolve(mockResponse([
          {
            id: "approval-run",
            session_id: "session-2",
            thread_id: "session-2",
            thread_label: "Atlas thread",
            tool_name: "shell_execute",
            risk_level: "high",
            status: "pending",
            summary: "Approve Atlas shell command",
            created_at: "2026-03-18T12:03:00Z",
            resume_message: "Continue Atlas shell approval",
          },
        ]));
      }
      if (url.includes("/api/observer/continuity")) {
        return Promise.resolve(mockResponse({
          daemon: { connected: false, pending_notification_count: 0, capture_mode: "balanced" },
          notifications: [],
          queued_insights: [
            {
              id: "queued-1",
              intervention_id: null,
              content_excerpt: "Draft Atlas follow-up",
              intervention_type: "advisory",
              urgency: 2,
              reasoning: "bundle_delivery",
              session_id: "session-2",
              thread_id: "session-2",
              thread_label: "Atlas thread",
              continuation_mode: "same_thread",
              resume_message: "Continue Atlas queued item",
              created_at: "2026-03-18T12:02:00Z",
            },
          ],
          queued_insight_count: 1,
          recent_interventions: [],
          summary: {
            continuity_health: "degraded",
            primary_surface: "reach",
            recommended_focus: "Bundle delivery",
            actionable_thread_count: 1,
            ambient_item_count: 0,
            pending_notification_count: 0,
            queued_insight_count: 1,
            recent_intervention_count: 0,
            degraded_route_count: 1,
            degraded_source_adapter_count: 1,
            attention_family_count: 1,
            presence_surface_count: 2,
            attention_presence_surface_count: 1,
          },
          imported_reach: {
            summary: {
              family_count: 1,
              active_family_count: 1,
              attention_family_count: 1,
              approval_family_count: 0,
            },
            families: [
              {
                type: "messaging_connectors",
                label: "messaging",
                total: 1,
                installed: 1,
                ready: 0,
                attention: 1,
                approval: 0,
                packages: ["Seraph Relay Pack"],
              },
            ],
          },
          source_adapters: {
            summary: {
              adapter_count: 1,
              ready_adapter_count: 0,
              degraded_adapter_count: 1,
              authenticated_adapter_count: 1,
              authenticated_ready_adapter_count: 0,
              authenticated_degraded_adapter_count: 1,
            },
            adapters: [
              {
                name: "github-managed",
                provider: "github",
                source_kind: "managed_connector",
                authenticated: true,
                runtime_state: "requires_runtime",
                adapter_state: "degraded",
                contracts: ["work_items.read", "code_activity.read"],
                degraded_reason: "runtime_adapter_missing",
                next_best_sources: [{ name: "web_search", reason: "fallback", description: "Use public context." }],
              },
            ],
          },
          presence_surfaces: {
            summary: {
              surface_count: 2,
              active_surface_count: 1,
              ready_surface_count: 1,
              attention_surface_count: 1,
            },
            surfaces: [
              {
                id: "messaging_connectors:seraph.relay:connectors/messaging/telegram.yaml",
                kind: "messaging_connector",
                label: "Telegram relay",
                package_label: "Seraph Relay Pack",
                package_id: "seraph.relay",
                status: "requires_config",
                active: false,
                ready: false,
                attention: true,
                detail: "Seraph Relay Pack exposes Telegram relay on telegram (requires config).",
                repair_hint: "Finish connector configuration in the operator surface before routing follow-through here.",
                follow_up_hint: null,
                follow_up_prompt: null,
                transport: null,
                source_type: null,
              },
              {
                id: "channel_adapters:seraph.native:channels/native.yaml",
                kind: "channel_adapter",
                label: "native notification channel",
                package_label: "Seraph Native Pack",
                package_id: "seraph.native",
                status: "ready",
                active: true,
                ready: true,
                attention: false,
                detail: "Seraph Native Pack exposes native notification channel for native notification delivery (ready).",
                repair_hint: null,
                follow_up_hint: "Use operator review before routing external follow-through through this surface.",
                follow_up_prompt: "Plan guarded follow-through for native notification channel. Confirm the audience, target reference, channel scope, and approval boundaries before acting.",
                transport: "native_notification",
                source_type: null,
              },
            ],
          },
          threads: [
            {
              id: "thread:session-2",
              thread_id: "session-2",
              thread_label: "Atlas thread",
              thread_source: "session",
              continuation_mode: "resume_thread",
              continue_message: "Continue Atlas queued item",
              item_count: 1,
              pending_notification_count: 0,
              queued_insight_count: 1,
              recent_intervention_count: 0,
              latest_updated_at: "2026-03-18T12:02:00Z",
              primary_surface: "bundle_queue",
              surfaces: ["bundle_queue"],
              summary: "1 continuity item across bundle queue for Atlas thread.",
              open_thread_available: true,
            },
          ],
          recovery_actions: [
            {
              id: "route:bundle_delivery",
              kind: "reach_repair",
              label: "Repair Bundle delivery",
              detail: "Bundle delivery waiting on browser reconnect",
              status: "unavailable",
              surface: "reach",
              route: "bundle_delivery",
              repair_hint: "Keep a cockpit tab connected.",
              thread_id: null,
              continue_message: null,
              open_thread_available: false,
            },
            {
              id: "followup:thread:session-2",
              kind: "thread_follow_up",
              label: "Continue Atlas thread",
              detail: "1 continuity item across bundle queue for Atlas thread.",
              status: "actionable",
              surface: "bundle_queue",
              route: null,
              repair_hint: null,
              thread_id: "session-2",
              continue_message: "Continue Atlas queued item",
              open_thread_available: true,
            },
            {
              id: "source:github-managed",
              kind: "source_adapter_repair",
              label: "Restore source adapter github-managed",
              detail: "github adapter is degraded (runtime_adapter_missing).",
              status: "degraded",
              surface: "source_adapter",
              route: null,
              repair_hint: "Next best: web_search.",
              thread_id: null,
              continue_message: null,
              open_thread_available: false,
            },
            {
              id: "presence:messaging_connectors:seraph.relay:connectors/messaging/telegram.yaml",
              kind: "presence_repair",
              label: "Review presence surface Telegram relay",
              detail: "Seraph Relay Pack exposes Telegram relay on telegram (requires config).",
              status: "requires_config",
              surface: "presence",
              route: "messaging_connector",
              repair_hint: "Finish connector configuration in the operator surface before routing follow-through here.",
              thread_id: null,
              continue_message: null,
              open_thread_available: false,
            },
            {
              id: "imported:messaging_connectors",
              kind: "imported_reach_attention",
              label: "Review imported messaging",
              detail: "1 imported contribution needs attention across 1 package.",
              status: "attention",
              surface: "imported_reach",
              route: null,
              repair_hint: "Inspect Seraph Relay Pack in the operator surface.",
              thread_id: null,
              continue_message: null,
              open_thread_available: false,
            },
            {
              id: "presence:observer_definitions:seraph.observer:observers/calendar.yaml",
              kind: "presence_repair",
              label: "Review presence surface Calendar observer",
              detail: "Seraph Observer Pack adds Calendar observer for calendar observation (planned).",
              status: "planned",
              surface: "presence",
              route: "observer_definition",
              repair_hint: "Enable the packaged contribution and confirm its runtime prerequisites in the operator surface.",
              thread_id: null,
              continue_message: null,
              open_thread_available: false,
            },
            {
              id: "imported:node_adapters",
              kind: "imported_reach_attention",
              label: "Review imported node adapters",
              detail: "1 imported contribution needs attention across 1 package.",
              status: "attention",
              surface: "imported_reach",
              route: null,
              repair_hint: "Inspect Node Pack in the operator surface.",
              thread_id: null,
              continue_message: null,
              open_thread_available: false,
            },
            {
              id: "presence-follow:channel_adapters:seraph.native:channels/native.yaml",
              kind: "presence_follow_up",
              label: "Plan follow-up via native notification channel",
              detail: "Seraph Native Pack exposes native notification channel for native notification delivery (ready).",
              status: "ready",
              surface: "presence",
              route: "channel_adapter",
              repair_hint: "Use operator review before routing external follow-through through this surface.",
              thread_id: null,
              continue_message: "Plan guarded follow-through for native notification channel. Confirm the audience, target reference, channel scope, and approval boundaries before acting.",
              open_thread_available: false,
            },
          ],
          reach: {
            route_statuses: [
              {
                route: "bundle_delivery",
                label: "Bundle delivery",
                status: "unavailable",
                summary: "Bundle delivery waiting on browser reconnect",
                selected_transport: "websocket",
                repair_hint: "Keep a cockpit tab connected.",
              },
            ],
          },
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
            workflows_ready: 2,
            workflows_total: 2,
            starter_packs_ready: 0,
            starter_packs_total: 0,
            mcp_servers_ready: 0,
            mcp_servers_total: 0,
          },
          native_tools: [],
          workflows: [
            {
              name: "web-brief-to-file",
              tool_name: "workflow_web_brief_to_file",
              description: "Write a brief into a workspace file",
              inputs: { file_path: { type: "string", description: "Workspace file", required: true } },
              requires_tools: ["write_file"],
              requires_skills: [],
              user_invocable: true,
              enabled: true,
              step_count: 1,
              file_path: "defaults/workflows/web-brief-to-file.md",
              policy_modes: ["balanced", "full"],
              execution_boundaries: ["workspace_read", "workspace_write"],
              risk_level: "medium",
              requires_approval: false,
              approval_behavior: "never",
              is_available: true,
              availability: "ready",
              missing_tools: [],
              missing_skills: [],
              output_surface_artifact_types: ["markdown_document"],
            },
            {
              name: "summarize-file",
              tool_name: "workflow_summarize_file",
              description: "Summarize a workspace file",
              inputs: {
                file_path: {
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
              file_path: "defaults/workflows/summarize-file.md",
              policy_modes: ["balanced", "full"],
              execution_boundaries: ["workspace_read"],
              risk_level: "low",
              requires_approval: false,
              approval_behavior: "never",
              is_available: true,
              availability: "ready",
              missing_tools: [],
              missing_skills: [],
            },
          ],
          skills: [],
          mcp_servers: [],
          starter_packs: [],
          catalog_items: [],
          recommendations: [],
          runbooks: [],
          extension_packages: [],
        }));
      }
      if (url.includes("/api/workflows/runs")) {
        return Promise.resolve(mockResponse({
          runs: [
            {
              id: "run-root",
              tool_name: "workflow_web_brief_to_file",
              workflow_name: "web-brief-to-file",
              session_id: "session-2",
              status: "degraded",
              started_at: "2026-03-18T12:00:00Z",
              updated_at: "2026-03-18T12:04:00Z",
              summary: "workflow_web_brief_to_file failed at write_file",
              step_tools: ["web_search", "write_file"],
              step_records: [
                {
                  id: "write_file",
                  index: 1,
                  tool: "write_file",
                  status: "failed",
                  argument_keys: ["file_path"],
                  artifact_paths: ["notes/brief.md"],
                  error_summary: "write_file blocked by approval",
                  recovery_hint: "Approve the pending write step and continue the workflow.",
                  recovery_actions: [{ type: "set_tool_policy", label: "Allow write_file", name: "write_file", mode: "full" }],
                  is_recoverable: true,
                },
              ],
              artifact_paths: ["notes/brief.md"],
              continued_error_steps: ["write_file"],
              risk_level: "medium",
              pending_approval_count: 0,
              pending_approval_ids: [],
              thread_id: "session-2",
              thread_label: "Atlas thread",
              replay_allowed: true,
              retry_from_step_draft: 'Retry step "write_file" for workflow "web-brief-to-file".',
              thread_continue_message: "Continue Atlas workflow",
              run_identity: "root-1",
              root_run_identity: "root-1",
              checkpoint_context_available: true,
            },
            {
              id: "run-branch",
              tool_name: "workflow_web_brief_to_file",
              workflow_name: "web-brief-to-file",
              session_id: "session-2",
              status: "running",
              started_at: "2026-03-18T12:05:00Z",
              updated_at: "2026-03-18T12:06:00Z",
              summary: "workflow_web_brief_to_file branch running",
              step_tools: ["write_file"],
              step_records: [],
              artifact_paths: ["notes/branch-brief.md"],
              continued_error_steps: [],
              risk_level: "medium",
              pending_approval_count: 0,
              pending_approval_ids: [],
              thread_id: "session-2",
              thread_label: "Atlas thread",
              replay_allowed: true,
              thread_continue_message: "Continue Atlas branch",
              run_identity: "branch-1",
              parent_run_identity: "root-1",
              root_run_identity: "root-1",
            },
            {
              id: "run-repair",
              tool_name: "workflow_atlas_repair",
              workflow_name: "atlas-repair",
              session_id: "session-2",
              status: "failed",
              started_at: "2026-03-18T11:55:00Z",
              updated_at: "2026-03-18T12:01:00Z",
              summary: "workflow_atlas_repair blocked before replay",
              step_tools: ["write_file"],
              step_records: [],
              artifact_paths: [],
              continued_error_steps: [],
              risk_level: "medium",
              pending_approval_count: 0,
              pending_approval_ids: [],
              thread_id: "session-2",
              thread_label: "Atlas thread",
              replay_allowed: false,
              replay_block_reason: "policy_boundary",
              replay_recommended_actions: [{ type: "set_tool_policy", label: "Allow write_file", name: "write_file", mode: "full" }],
              run_identity: "repair-1",
              root_run_identity: "repair-1",
            },
            {
              id: "run-complete",
              tool_name: "workflow_summarize_file",
              workflow_name: "summarize-file",
              session_id: "session-2",
              status: "succeeded",
              started_at: "2026-03-18T12:06:00Z",
              updated_at: "2026-03-18T12:07:00Z",
              summary: "workflow_summarize_file saved final note",
              step_tools: ["read_file"],
              step_records: [],
              artifact_paths: ["notes/final.md"],
              continued_error_steps: [],
              risk_level: "low",
              pending_approval_count: 0,
              pending_approval_ids: [],
              thread_id: "session-2",
              thread_label: "Atlas thread",
              replay_allowed: true,
              thread_continue_message: "Continue Atlas summary",
              run_identity: "complete-1",
              root_run_identity: "complete-1",
            },
          ],
        }));
      }
      if (url.includes("/api/operator/workflow-orchestration")) {
        return Promise.resolve(mockResponse({
          summary: {
            tracked_sessions: 2,
            workflow_count: 4,
            active_workflows: 1,
            blocked_workflows: 1,
            awaiting_approval_workflows: 0,
            recoverable_workflows: 1,
          },
          sessions: [
            {
              thread_id: "session-2",
              thread_label: "Atlas thread",
              workflow_count: 3,
              active_workflows: 1,
              blocked_workflows: 1,
              awaiting_approval_workflows: 0,
              recoverable_workflows: 1,
              latest_updated_at: "2026-03-18T12:06:00Z",
              lead_run_identity: "root-1",
              lead_workflow_name: "web-brief-to-file",
              lead_status: "degraded",
              lead_summary: "workflow_web_brief_to_file failed at write_file",
              continue_message: "Continue Atlas workflow",
              lead_step_focus: {
                kind: "failure",
                step_id: "write_file",
                tool: "write_file",
                status: "failed",
                error_summary: "write_file blocked by approval",
                recovery_hint: "Approve the pending write step and continue the workflow.",
                recovery_action_count: 1,
                is_recoverable: true,
              },
            },
            {
              thread_id: null,
              thread_label: null,
              workflow_count: 1,
              active_workflows: 0,
              blocked_workflows: 0,
              awaiting_approval_workflows: 0,
              recoverable_workflows: 0,
              latest_updated_at: "2026-03-18T12:01:00Z",
              lead_run_identity: null,
              lead_workflow_name: "cleanup",
              lead_status: "running",
              lead_summary: "Ambient cleanup scan",
              continue_message: null,
              lead_step_focus: {
                kind: "active",
                step_id: "scan",
                tool: "read_file",
                status: "running",
                summary: "Scanning workspace files",
                recovery_action_count: 0,
                is_recoverable: false,
              },
            },
          ],
          workflows: [
            {
              run_identity: "root-1",
              workflow_name: "web-brief-to-file",
              summary: "workflow_web_brief_to_file failed at write_file",
              status: "degraded",
              availability: "attention",
              updated_at: "2026-03-18T12:04:00Z",
              thread_id: "session-2",
              thread_label: "Atlas thread",
              continue_message: "Continue Atlas workflow",
              output_path: "notes/brief.md",
              pending_approval_count: 0,
              checkpoint_candidate_count: 1,
              retry_from_step_available: true,
              replay_block_reason: null,
              step_focus: {
                kind: "failure",
                step_id: "write_file",
                tool: "write_file",
                status: "failed",
                error_summary: "write_file blocked by approval",
                recovery_hint: "Approve the pending write step and continue the workflow.",
                recovery_action_count: 1,
                is_recoverable: true,
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

    const triage = await screen.findByLabelText("Active triage");
    await waitFor(() => {
      expect(within(triage).getByText("approval: shell_execute")).toBeInTheDocument();
      expect(within(triage).getByText("awaiting approval · Approve Atlas shell command")).toBeInTheDocument();
      expect(within(triage).getByText("workflow degraded: web-brief-to-file")).toBeInTheDocument();
      expect(within(triage).getByText("workflow running: web-brief-to-file")).toBeInTheDocument();
      expect(within(triage).getByText("workflow failed: atlas-repair")).toBeInTheDocument();
      expect(within(triage).getByText("degraded · workflow_web_brief_to_file failed at write_file")).toBeInTheDocument();
      expect(within(triage).getByText("queued: advisory")).toBeInTheDocument();
      expect(within(triage).getByText("queued follow-up · Draft Atlas follow-up")).toBeInTheDocument();
      expect(within(triage).getByText("reach: Bundle delivery")).toBeInTheDocument();
      expect(within(triage).getByText("unavailable · Bundle delivery waiting on browser reconnect")).toBeInTheDocument();
      expect(within(triage).getAllByRole("button", { name: /Inspect latest branch for workflow .*: web-brief-to-file/ })).toHaveLength(1);
    });
    expect(within(triage).queryByText("workflow: summarize-file")).not.toBeInTheDocument();

    const supervision = await screen.findByLabelText("Workflow supervision");
    await waitFor(() => {
      expect(within(supervision).getByText("workflow: summarize-file")).toBeInTheDocument();
      expect(within(supervision).getByText("completed · workflow_summarize_file saved final note")).toBeInTheDocument();
      expect(within(supervision).getByText(/history 1 outputs/i)).toBeInTheDocument();
    });

    const orchestration = await screen.findByLabelText("Workflow orchestration");
    await waitFor(() => {
      expect(within(orchestration).getByText("4 workflows · 2 sessions · 0 compacted")).toBeInTheDocument();
      expect(within(orchestration).getByText("Atlas thread")).toBeInTheDocument();
      expect(within(orchestration).getByText("web-brief-to-file · degraded · workflow_web_brief_to_file failed at write_file")).toBeInTheDocument();
      expect(within(orchestration).getByText("Ambient workflows")).toBeInTheDocument();
      expect(within(orchestration).getByText("cleanup · running · Ambient cleanup scan")).toBeInTheDocument();
    });

    const orchestrationRow = within(orchestration).getByText("Atlas thread").closest(".cockpit-operator-row--entry");
    expect(orchestrationRow).not.toBeNull();
    expect(within(orchestrationRow as HTMLElement).getByRole("button", { name: "Continue workflow orchestration for Atlas thread" })).toBeInTheDocument();
    expect(within(orchestrationRow as HTMLElement).getByRole("button", { name: "Use failure context for workflow orchestration Atlas thread" })).toBeInTheDocument();
    expect(within(orchestrationRow as HTMLElement).getByRole("button", { name: "Draft next step for workflow orchestration Atlas thread" })).toBeInTheDocument();
    fireEvent.click(within(orchestrationRow as HTMLElement).getByRole("button", { name: "Continue workflow orchestration for Atlas thread" }));
    await waitFor(() => expect(screen.getByDisplayValue("Continue Atlas workflow")).toBeInTheDocument());
    fireEvent.click(within(orchestrationRow as HTMLElement).getByRole("button", { name: "Use failure context for workflow orchestration Atlas thread" }));
    await waitFor(() =>
      expect(
        screen.getByDisplayValue(/Review workflow "web-brief-to-file" step "write_file"/),
      ).toBeInTheDocument(),
    );

    const approvalRow = within(triage).getByText("approval: shell_execute").closest(".cockpit-operator-row--entry");
    expect(approvalRow).not.toBeNull();
    fireEvent.click(within(approvalRow as HTMLElement).getByRole("button", { name: "Approve approval: shell_execute" }));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/approvals/approval-run/approve"),
        expect.objectContaining({ method: "POST" }),
      ),
    );

    const workflowRow = within(triage).getByText("workflow degraded: web-brief-to-file").closest(".cockpit-operator-row--entry");
    expect(workflowRow).not.toBeNull();
    expect(within(workflowRow as HTMLElement).getByRole("button", { name: "Use latest output for workflow degraded: web-brief-to-file" })).toBeInTheDocument();
    expect(within(workflowRow as HTMLElement).getByRole("button", { name: "Use failure context for workflow degraded: web-brief-to-file" })).toBeInTheDocument();
    expect(within(workflowRow as HTMLElement).getByRole("button", { name: "Retry step for workflow degraded: web-brief-to-file" })).toBeInTheDocument();
    expect(within(workflowRow as HTMLElement).getByRole("button", { name: "Repair step for workflow degraded: web-brief-to-file" })).toBeInTheDocument();
    expect(within(workflowRow as HTMLElement).getByRole("button", { name: "Open best continuation for workflow degraded: web-brief-to-file" })).toBeInTheDocument();
    expect(within(workflowRow as HTMLElement).getByRole("button", { name: "Continue best continuation for workflow degraded: web-brief-to-file" })).toBeInTheDocument();
    expect(within(workflowRow as HTMLElement).getByRole("button", { name: "Draft next step for workflow degraded: web-brief-to-file" })).toBeInTheDocument();
    expect(within(workflowRow as HTMLElement).getByRole("button", { name: "Compare best continuation for workflow degraded: web-brief-to-file" })).toBeInTheDocument();

    fireEvent.click(within(workflowRow as HTMLElement).getByRole("button", { name: "Use latest output for workflow degraded: web-brief-to-file" }));
    await waitFor(() => expect(screen.getByDisplayValue('Use the workspace file "notes/brief.md" as context for the next action.')).toBeInTheDocument());

    fireEvent.click(within(workflowRow as HTMLElement).getByRole("button", { name: "Use failure context for workflow degraded: web-brief-to-file" }));
    await waitFor(() =>
      expect(
        screen.getByDisplayValue(/Review workflow "web-brief-to-file" step "write_file"/),
      ).toBeInTheDocument(),
    );

    fireEvent.click(within(workflowRow as HTMLElement).getByRole("button", { name: "Retry step for workflow degraded: web-brief-to-file" }));
    await waitFor(() =>
      expect(screen.getByDisplayValue('Retry step "write_file" for workflow "web-brief-to-file".')).toBeInTheDocument(),
    );

    fireEvent.click(within(workflowRow as HTMLElement).getByRole("button", { name: "Repair step for workflow degraded: web-brief-to-file" }));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/settings/tool-policy-mode"),
        expect.objectContaining({ method: "PUT" }),
      ),
    );

    fireEvent.click(within(workflowRow as HTMLElement).getByRole("button", { name: "Compare best continuation for workflow degraded: web-brief-to-file" }));
    await waitFor(() =>
      expect(
        screen.getByDisplayValue(
          'Compare the workspace files "notes/brief.md" and "notes/branch-brief.md". Summarize the key differences, what changed between these workflow outputs, and whether the related branch improved the result.',
        ),
      ).toBeInTheDocument(),
    );

    fireEvent.click(within(workflowRow as HTMLElement).getByRole("button", { name: "Draft next step for workflow degraded: web-brief-to-file" }));
    await waitFor(() =>
      expect(
        screen.getByDisplayValue(
          'Review workflow family state for "web-brief-to-file". Current output: "notes/brief.md". Best continuation: "workflow_web_brief_to_file branch running" with latest output "notes/branch-brief.md" Latest family failure: "workflow_web_brief_to_file failed at write_file". Related reusable outputs: "notes/branch-brief.md". Recommend the best next step, whether to continue a branch, compare outputs, or reuse one of the related outputs.',
        ),
      ).toBeInTheDocument(),
    );

    fireEvent.click(within(workflowRow as HTMLElement).getByRole("button", { name: "Continue best continuation for workflow degraded: web-brief-to-file" }));
    await waitFor(() => expect(screen.getByDisplayValue("Continue Atlas branch")).toBeInTheDocument());

    fireEvent.click(within(workflowRow as HTMLElement).getByRole("button", { name: "Open best continuation for workflow degraded: web-brief-to-file" }));
    await waitFor(() =>
      expect((document.querySelector(".cockpit-inspector-body") as HTMLElement).textContent).toContain("workflow_web_brief_to_file branch running"),
    );
    expect(screen.getByText("parent run")).toBeInTheDocument();

    const runningWorkflowRow = within(triage).getByText("workflow running: web-brief-to-file").closest(".cockpit-operator-row--entry");
    expect(runningWorkflowRow).not.toBeNull();
    expect(within(runningWorkflowRow as HTMLElement).getByRole("button", { name: "Use failure context for workflow running: web-brief-to-file" })).toBeInTheDocument();
    expect(within(runningWorkflowRow as HTMLElement).queryByRole("button", { name: "Open best continuation for workflow running: web-brief-to-file" })).not.toBeInTheDocument();
    expect(within(runningWorkflowRow as HTMLElement).queryByRole("button", { name: "Retry step for workflow running: web-brief-to-file" })).not.toBeInTheDocument();

    const supervisionRow = within(supervision).getByText("workflow: summarize-file").closest(".cockpit-operator-row--entry");
    expect(supervisionRow).not.toBeNull();
    expect(within(supervisionRow as HTMLElement).getByRole("button", { name: "Use latest output for workflow supervision summarize-file" })).toBeInTheDocument();
    expect(within(supervisionRow as HTMLElement).getByRole("button", { name: "Draft next step for workflow supervision summarize-file" })).toBeInTheDocument();
    fireEvent.click(within(supervisionRow as HTMLElement).getByRole("button", { name: "Use latest output for workflow supervision summarize-file" }));
    await waitFor(() => expect(screen.getByDisplayValue('Use the workspace file "notes/final.md" as context for the next action.')).toBeInTheDocument());

    fireEvent.keyDown(window, { key: "F", shiftKey: true });
    await waitFor(() =>
      expect(
        screen.getByDisplayValue(/Current failure: write_file blocked by approval\./),
      ).toBeInTheDocument(),
    );

    fireEvent.keyDown(window, { key: "T", shiftKey: true });
    await waitFor(() =>
      expect(screen.getByDisplayValue('Retry step "write_file" for workflow "web-brief-to-file".')).toBeInTheDocument(),
    );

    fireEvent.keyDown(window, { key: "H", shiftKey: true });
    await waitFor(() =>
      expect((document.querySelector(".cockpit-inspector-body") as HTMLElement).textContent).toContain("workflow_web_brief_to_file branch running"),
    );

    fireEvent.keyDown(window, { key: "L", shiftKey: true });
    await waitFor(() =>
      expect((document.querySelector(".cockpit-inspector-body") as HTMLElement).textContent).toContain("workflow_web_brief_to_file branch running"),
    );
    expect(screen.getByText("parent run")).toBeInTheDocument();

    fireEvent.keyDown(window, { key: "B", shiftKey: true });
    await waitFor(() =>
      expect((document.querySelector(".cockpit-inspector-body") as HTMLElement).textContent).toContain("workflow_web_brief_to_file branch running"),
    );

    fireEvent.keyDown(window, { key: "N", shiftKey: true });
    await waitFor(() => expect(screen.getByDisplayValue("Continue Atlas branch")).toBeInTheDocument());

    fireEvent.keyDown(window, { key: "G", shiftKey: true });
    await waitFor(() =>
      expect(
        screen.getByDisplayValue(
          'Compare the workspace files "notes/brief.md" and "notes/branch-brief.md". Summarize the key differences, what changed between these workflow outputs, and whether the related branch improved the result.',
        ),
      ).toBeInTheDocument(),
    );

    const blockedWorkflowRow = within(triage).getByText("workflow failed: atlas-repair").closest(".cockpit-operator-row--entry");
    expect(blockedWorkflowRow).not.toBeNull();
    expect(within(blockedWorkflowRow as HTMLElement).getByRole("button", { name: "Repair replay for workflow failed: atlas-repair" })).toBeInTheDocument();
    expect(within(blockedWorkflowRow as HTMLElement).queryByRole("button", { name: "Retry step for workflow failed: atlas-repair" })).not.toBeInTheDocument();
    fireEvent.click(within(blockedWorkflowRow as HTMLElement).getByRole("button", { name: "Repair replay for workflow failed: atlas-repair" }));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/settings/tool-policy-mode"),
        expect.objectContaining({ method: "PUT" }),
      ),
    );

    const queuedRow = within(triage).getByText("queued: advisory").closest(".cockpit-operator-row--entry");
    expect(queuedRow).not.toBeNull();
    fireEvent.click(within(queuedRow as HTMLElement).getByRole("button", { name: "Continue queued: advisory" }));
    await waitFor(() => expect(screen.getByDisplayValue("Continue Atlas queued item")).toBeInTheDocument());

    const routeRow = within(triage).getByText("reach: Bundle delivery").closest(".cockpit-operator-row--entry");
    expect(routeRow).not.toBeNull();
    fireEvent.click(within(routeRow as HTMLElement).getByRole("button", { name: "Open desktop shell for reach: Bundle delivery" }));
    await waitFor(() => expect(screen.getByText("Desktop shell")).toBeInTheDocument());
    expect(screen.getByText(/continuity degraded · threads 1 · ambient 0/i)).toBeInTheDocument();
    expect(screen.getByText(/typed adapters 0\/1 ready · authenticated 0\/1/i)).toBeInTheDocument();
    expect(screen.getByText(/presence 1\/2 ready · 1 attention/i)).toBeInTheDocument();
    expect(screen.getByText(/imported reach 1\/1 active · 1 attention/i)).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Draft repair" }).length).toBeGreaterThan(0);
    expect(screen.getAllByRole("button", { name: "Continue" }).length).toBeGreaterThan(0);
    expect(screen.getAllByRole("button", { name: "Open Thread" }).length).toBeGreaterThan(0);

    const sourceRow = within(triage).getByText("reach: Restore source adapter github-managed").closest(".cockpit-operator-row--entry");
    expect(sourceRow).not.toBeNull();
    expect(within(sourceRow as HTMLElement).getByRole("button", { name: "Draft repair for reach: Restore source adapter github-managed" })).toBeInTheDocument();
    expect(within(sourceRow as HTMLElement).getByRole("button", { name: "Open operator surface for reach: Restore source adapter github-managed" })).toBeInTheDocument();
    fireEvent.click(within(sourceRow as HTMLElement).getByRole("button", { name: "Draft repair for reach: Restore source adapter github-managed" }));
    await waitFor(() =>
      expect(
        screen.getByDisplayValue(/Review restore source adapter github-managed/i),
      ).toBeInTheDocument(),
    );

    const followUpDesktopRow = screen.getByText("Plan follow-up via native notification channel").closest(".cockpit-row");
    expect(followUpDesktopRow).not.toBeNull();
    fireEvent.click(within(followUpDesktopRow as HTMLElement).getByRole("button", { name: "Draft follow-up" }));
    await waitFor(() =>
      expect(screen.getByDisplayValue(/Plan guarded follow-through for native notification channel/i)).toBeInTheDocument(),
    );
  }, 30000);

  it("keeps workflow-orchestration controls when the lead run only exists in the orchestration payload", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/sessions")) {
        return Promise.resolve(mockResponse([
          { id: "session-2", title: "Atlas thread", created_at: "", updated_at: "", last_message: null, last_message_role: null },
        ]));
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
          summary: {
            continuity_health: "healthy",
            primary_surface: "workspace",
            recommended_focus: "workflow orchestration",
            actionable_thread_count: 1,
            ambient_item_count: 0,
            pending_notification_count: 0,
            queued_insight_count: 0,
            recent_intervention_count: 0,
            degraded_route_count: 0,
            degraded_source_adapter_count: 0,
            attention_family_count: 0,
            presence_surface_count: 0,
            attention_presence_surface_count: 0,
          },
        }));
      }
      if (url.includes("/api/capabilities/overview")) {
        return Promise.resolve(mockResponse({
          tool_policy_mode: "balanced",
          mcp_policy_mode: "approval",
          approval_mode: "high_risk",
          summary: {},
          native_tools: [],
          workflows: [],
          skills: [],
          mcp_servers: [],
          starter_packs: [],
          catalog_items: [],
          recommendations: [],
          runbooks: [],
          extension_packages: [],
        }));
      }
      if (url.includes("/api/workflows/runs")) return Promise.resolve(mockResponse({ runs: [] }));
      if (url.includes("/api/extensions")) return Promise.resolve(mockResponse({ extensions: [], summary: {} }));
      if (url.includes("/api/activity/ledger")) {
        return Promise.resolve(mockResponse({ items: [], summary: { llm_call_count: 0, llm_cost_usd: 0, failure_count: 0 } }));
      }
      if (url.includes("/api/operator/control-plane")) {
        return Promise.resolve(mockResponse({
          governance: {
            workspace_mode: "single_operator_guarded_workspace",
            review_posture: "guarded",
            approval_mode: "high_risk",
            tool_policy_mode: "balanced",
            mcp_policy_mode: "approval",
            delegation_enabled: false,
            roles: [],
          },
          usage: {
            window_hours: 24,
            llm_call_count: 0,
            llm_cost_usd: 0,
            input_tokens: 0,
            output_tokens: 0,
            user_triggered_llm_calls: 0,
            autonomous_llm_calls: 0,
            failure_count: 0,
            pending_approvals: 0,
            active_workflows: 1,
            blocked_workflows: 0,
          },
          runtime_posture: {
            runtime: {
              version: "2026.4.10",
              build_id: "SERAPH_PRIME_v2026.4.10",
              provider: "openrouter",
              model: "openrouter/openai/gpt-4.1-mini",
              model_label: "gpt-4.1-mini",
            },
            extensions: {
              total: 0,
              ready: 0,
              degraded: 0,
              governed: 0,
              issue_count: 0,
              degraded_connector_count: 0,
            },
            continuity: {
              continuity_health: "healthy",
              primary_surface: "workspace",
              recommended_focus: "workflow orchestration",
              actionable_thread_count: 1,
              degraded_route_count: 0,
              degraded_source_adapter_count: 0,
              attention_presence_surface_count: 0,
            },
          },
          handoff: {
            pending_approvals: [],
            blocked_workflows: [],
            follow_ups: [],
            review_receipts: [],
          },
        }));
      }
      if (url.includes("/api/operator/workflow-orchestration")) {
        return Promise.resolve(mockResponse({
          summary: {
            tracked_sessions: 1,
            workflow_count: 2,
            active_workflows: 1,
            blocked_workflows: 0,
            awaiting_approval_workflows: 0,
            recoverable_workflows: 1,
            long_running_workflows: 1,
            compacted_workflows: 1,
            total_step_count: 5,
            compacted_step_count: 2,
            boundary_blocked_workflows: 0,
            repair_ready_workflows: 1,
            branch_ready_workflows: 2,
            stalled_workflows: 0,
            output_debugger_ready_workflows: 2,
            attention_sessions: 1,
          },
          sessions: [
            {
              thread_id: "session-2",
              thread_label: "Atlas thread",
              workflow_count: 2,
              active_workflows: 1,
              blocked_workflows: 0,
              awaiting_approval_workflows: 0,
              recoverable_workflows: 1,
              latest_updated_at: "2026-03-18T12:06:00Z",
              lead_run_identity: "root-1",
              lead_workflow_name: "web-brief-to-file",
              lead_status: "degraded",
              lead_summary: "workflow_web_brief_to_file failed at write_file",
              continue_message: "Continue Atlas workflow",
              total_step_count: 0,
              compacted_step_count: 0,
              compacted_workflow_count: 1,
              long_running_workflow_count: 0,
              artifact_count: 0,
              lead_state_capsule: null,
              boundary_blocked_workflows: 0,
              repair_ready_workflows: 1,
              branch_ready_workflows: 2,
              stalled_workflows: 0,
              output_debugger_ready_workflows: 2,
              queue_state: "repair_ready",
              queue_position: 1,
              queue_reason: "1 workflow exposes a recoverable failed step that can be repaired now.",
              attention_summary: "1 repair ready · 2 branch ready · 2 debugger ready",
              queue_draft: "Review the workflow queue for Atlas thread. Lead workflow \"web-brief-to-file\" is currently repair ready.",
              handoff_draft: "Prepare a workflow handoff for Atlas thread. Lead workflow \"web-brief-to-file\" is currently repair ready.",
              lead_recommended_recovery_path: "step_repair",
              lead_output_path: "notes/brief.md",
              lead_related_output_paths: ["notes/brief-branch.md"],
              lead_output_history: [
                {
                  path: "notes/brief-branch.md",
                  run_identity: "root-1:branch-1",
                  summary: "Branched repair completed",
                  status: "succeeded",
                  branch_kind: "branch_from_checkpoint",
                  updated_at: "2026-03-18T12:06:00Z",
                  is_primary: false,
                },
                {
                  path: "notes/brief.md",
                  run_identity: "root-1",
                  summary: "workflow_web_brief_to_file failed at write_file",
                  status: "degraded",
                  branch_kind: null,
                  updated_at: "2026-03-18T12:04:00Z",
                  is_primary: true,
                },
              ],
              lead_latest_branch_run_identity: "root-1:branch-1",
              lead_latest_branch_summary: "Branched repair completed",
              lead_step_focus: {
                kind: "failure",
                step_id: "write_file",
                tool: "write_file",
                status: "failed",
                error_summary: "write_file blocked by approval",
                recovery_hint: "Approve the pending write step and continue the workflow.",
                recovery_action_count: 1,
                is_recoverable: true,
              },
            },
          ],
          workflows: [
            {
              id: "run-root",
              tool_name: "workflow_web_brief_to_file",
              run_identity: "root-1",
              root_run_identity: "root-1",
              parent_run_identity: null,
              workflow_name: "web-brief-to-file",
              summary: "workflow_web_brief_to_file failed at write_file",
              status: "degraded",
              availability: "ready",
              session_id: "session-2",
              started_at: "2026-03-18T12:00:00Z",
              updated_at: "2026-03-18T12:04:00Z",
              thread_id: "session-2",
              thread_label: "Atlas thread",
              continue_message: "Continue Atlas workflow",
              thread_continue_message: "Continue Atlas workflow",
              output_path: "notes/brief.md",
              artifact_paths: ["notes/brief.md"],
              step_records: [
                {
                  id: "scope",
                  index: 0,
                  tool: "session_search",
                  status: "succeeded",
                  result_summary: "Scoped existing brief context",
                },
                {
                  id: "collect",
                  index: 1,
                  tool: "web_search",
                  status: "succeeded",
                  result_summary: "Collected source material",
                },
                {
                  id: "outline",
                  index: 2,
                  tool: "llm_plan",
                  status: "succeeded",
                  result_summary: "Outlined the brief",
                },
                {
                  id: "write_file",
                  index: 3,
                  tool: "write_file",
                  status: "failed",
                  error_summary: "write_file blocked by approval",
                  recovery_hint: "Approve the pending write step and continue the workflow.",
                  recovery_actions: [{ type: "set_tool_policy", label: "Allow write_file", mode: "full" }],
                  is_recoverable: true,
                },
                {
                  id: "notify",
                  index: 4,
                  tool: "notify_user",
                  status: "pending",
                  result_summary: "Queue follow-up notification",
                },
              ],
              pending_approval_count: 0,
              pending_approval_ids: [],
              checkpoint_candidate_count: 1,
              checkpoint_candidates: [{ step_id: "collect", label: "collect" }],
              retry_from_step_available: true,
              retry_from_step_draft: 'Retry step "write_file" for workflow "web-brief-to-file".',
              replay_allowed: true,
              replay_block_reason: null,
              replay_recommended_actions: [],
              step_focus: {
                kind: "failure",
                step_id: "write_file",
                tool: "write_file",
                status: "failed",
                error_summary: "write_file blocked by approval",
                recovery_hint: "Approve the pending write step and continue the workflow.",
                recovery_action_count: 1,
                is_recoverable: true,
              },
              is_long_running: true,
              is_compacted: true,
              duration_minutes: 37,
              step_count: 5,
              visible_step_count: 3,
              compacted_step_count: 2,
              artifact_count: 1,
              preserved_recovery_paths: ["retry_from_step", "checkpoint_branch", "step_repair"],
              recent_step_labels: [
                "outline / llm_plan / succeeded",
                "write_file / write_file / failed",
                "notify / notify_user / pending",
              ],
              state_capsule: "5 steps · 2 compacted · 1 artifact · preserves retry from step, checkpoint branch, step repair",
              recovery_density: {
                recommended_path: "step_repair",
                approval_pending: false,
                boundary_blocked: false,
                retry_ready: true,
                checkpoint_ready: true,
                repair_ready: true,
                branch_ready: true,
                replay_ready: true,
                stalled: false,
                checkpoint_candidate_count: 1,
                recovery_action_count: 1,
                repair_action_types: ["set_tool_policy"],
                repair_hint: "Approve the pending write step and continue the workflow.",
                failure_step_id: "write_file",
                failure_step_tool: "write_file",
              },
              output_debugger: {
                family_run_count: 2,
                branch_run_count: 1,
                history_output_count: 2,
                primary_output_path: "notes/brief.md",
                related_output_paths: ["notes/brief-branch.md"],
                history_outputs: [
                  {
                    path: "notes/brief-branch.md",
                    run_identity: "root-1:branch-1",
                    summary: "Branched repair completed",
                    status: "succeeded",
                    branch_kind: "branch_from_checkpoint",
                    updated_at: "2026-03-18T12:06:00Z",
                    is_primary: false,
                  },
                  {
                    path: "notes/brief.md",
                    run_identity: "root-1",
                    summary: "workflow_web_brief_to_file failed at write_file",
                    status: "degraded",
                    branch_kind: null,
                    updated_at: "2026-03-18T12:04:00Z",
                    is_primary: true,
                  },
                ],
                latest_branch_run_identity: "root-1:branch-1",
                latest_branch_summary: "Branched repair completed",
                latest_branch_status: "succeeded",
                latest_branch_output_path: "notes/brief-branch.md",
                comparison_ready: true,
                checkpoint_labels: ["collect"],
              },
            },
            {
              id: "run-branch",
              tool_name: "workflow_web_brief_to_file",
              run_identity: "root-1:branch-1",
              root_run_identity: "root-1",
              parent_run_identity: "root-1",
              workflow_name: "web-brief-to-file",
              summary: "Branched repair completed",
              status: "succeeded",
              availability: "ready",
              session_id: "session-2",
              started_at: "2026-03-18T12:05:00Z",
              updated_at: "2026-03-18T12:06:00Z",
              thread_id: "session-2",
              thread_label: "Atlas thread",
              continue_message: "Continue Atlas workflow branch",
              thread_continue_message: "Continue Atlas workflow branch",
              output_path: "notes/brief-branch.md",
              artifact_paths: ["notes/brief-branch.md"],
              step_records: [
                {
                  id: "repair",
                  index: 0,
                  tool: "write_file",
                  status: "succeeded",
                  result_summary: "Published repaired branch draft",
                },
              ],
              pending_approval_count: 0,
              pending_approval_ids: [],
              checkpoint_candidate_count: 0,
              checkpoint_candidates: [],
              retry_from_step_available: false,
              retry_from_step_draft: null,
              replay_allowed: true,
              replay_block_reason: null,
              replay_recommended_actions: [],
              step_focus: {
                kind: "latest",
                step_id: "repair",
                tool: "write_file",
                status: "succeeded",
                summary: "Published repaired branch draft",
                recovery_action_count: 0,
                is_recoverable: false,
              },
              is_long_running: false,
              is_compacted: false,
              duration_minutes: 1,
              step_count: 1,
              visible_step_count: 1,
              compacted_step_count: 0,
              artifact_count: 1,
              preserved_recovery_paths: [],
              recent_step_labels: ["repair / write_file / succeeded"],
              state_capsule: "1 steps · 1 artifact",
              recovery_density: {
                recommended_path: "branch_continue",
                approval_pending: false,
                boundary_blocked: false,
                retry_ready: false,
                checkpoint_ready: false,
                repair_ready: false,
                branch_ready: true,
                replay_ready: true,
                stalled: false,
                checkpoint_candidate_count: 0,
                recovery_action_count: 0,
                repair_action_types: [],
                repair_hint: null,
                failure_step_id: null,
                failure_step_tool: null,
              },
              output_debugger: {
                family_run_count: 2,
                branch_run_count: 1,
                history_output_count: 2,
                primary_output_path: "notes/brief-branch.md",
                related_output_paths: ["notes/brief.md"],
                history_outputs: [
                  {
                    path: "notes/brief-branch.md",
                    run_identity: "root-1:branch-1",
                    summary: "Branched repair completed",
                    status: "succeeded",
                    branch_kind: "branch_from_checkpoint",
                    updated_at: "2026-03-18T12:06:00Z",
                    is_primary: true,
                  },
                  {
                    path: "notes/brief.md",
                    run_identity: "root-1",
                    summary: "workflow_web_brief_to_file failed at write_file",
                    status: "degraded",
                    branch_kind: null,
                    updated_at: "2026-03-18T12:04:00Z",
                    is_primary: false,
                  },
                ],
                latest_branch_run_identity: null,
                latest_branch_summary: null,
                latest_branch_status: null,
                latest_branch_output_path: null,
                comparison_ready: false,
                checkpoint_labels: [],
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

    const orchestration = await screen.findByLabelText("Workflow orchestration");
    await waitFor(() => {
      expect(orchestration).toHaveTextContent(/2 workflows · 1 sessions · 1 compacted/i);
      expect(orchestration).toHaveTextContent(/1 active · 0 awaiting approval · 0 blocked · 1 recoverable · 1 long-running · 5 steps · 2 compacted · 1 repair-ready · 2 branch-ready · 2 debugger-ready · 1 attention sessions/i);
    });
    const row = (await within(orchestration).findByText("Atlas thread")).closest(".cockpit-operator-row--entry");
    expect(row).not.toBeNull();
    expect(row as HTMLElement).toHaveTextContent(/2 workflows · 1 active · 1 recoverable/i);
    expect(row as HTMLElement).toHaveTextContent(/queue #1 · queue repair ready · next step repair · 1 repair-ready · 2 branch-ready · 2 debugger-ready/i);
    expect(row as HTMLElement).toHaveTextContent(/1 repair ready · 2 branch ready · 2 debugger ready/i);
    expect(row as HTMLElement).toHaveTextContent(/1 workflow exposes a recoverable failed step that can be repaired now\./i);
    expect(row as HTMLElement).toHaveTextContent(/1 long-running · 5 steps · 2 compacted · 1 artifacts?/i);
    expect(row as HTMLElement).toHaveTextContent(/5 steps · 2 compacted · 1 artifact · preserves retry from step, checkpoint branch, step repair/i);
    expect(row as HTMLElement).toHaveTextContent(/visible steps 3\/5/i);
    expect(row as HTMLElement).toHaveTextContent(/outline \/ llm_plan \/ succeeded/i);
    expect(row as HTMLElement).toHaveTextContent(/notify \/ notify_user \/ pending/i);
    expect(row as HTMLElement).toHaveTextContent(/output notes\/brief\.md · related notes\/brief-branch\.md/i);
    expect(row as HTMLElement).toHaveTextContent(/2 history outputs · latest branch Branched repair completed/i);
    expect(
      within(row as HTMLElement).getAllByRole("button", { name: "Inspect workflow orchestration for Atlas thread" }),
    ).toHaveLength(2);
    expect(within(row as HTMLElement).getByRole("button", { name: "Use latest output for workflow orchestration Atlas thread" })).toBeInTheDocument();
    expect(within(row as HTMLElement).getByRole("button", { name: "Use failure context for workflow orchestration Atlas thread" })).toBeInTheDocument();
    expect(within(row as HTMLElement).getByRole("button", { name: "Repair workflow orchestration for Atlas thread" })).toBeInTheDocument();
    expect(within(row as HTMLElement).getByRole("button", { name: "Retry step for workflow orchestration Atlas thread" })).toBeInTheDocument();
    expect(within(row as HTMLElement).getByRole("button", { name: "Open latest branch for workflow orchestration Atlas thread" })).toBeInTheDocument();
    expect(within(row as HTMLElement).getByRole("button", { name: "Compare branch output for workflow orchestration Atlas thread" })).toBeInTheDocument();
    expect(within(row as HTMLElement).getByRole("button", { name: "Redirect workflow orchestration for Atlas thread" })).toBeInTheDocument();
    expect(within(row as HTMLElement).getByRole("button", { name: "Plan queue focus for workflow orchestration Atlas thread" })).toBeInTheDocument();
    expect(within(row as HTMLElement).getByRole("button", { name: "Draft handoff for workflow orchestration Atlas thread" })).toBeInTheDocument();
    expect(within(row as HTMLElement).getByRole("button", { name: "Draft next step for workflow orchestration Atlas thread" })).toBeInTheDocument();

    fireEvent.click(within(row as HTMLElement).getByRole("button", { name: "Use latest output for workflow orchestration Atlas thread" }));
    await waitFor(() => expect(screen.getByDisplayValue('Use the workspace file "notes/brief.md" as context for the next action.')).toBeInTheDocument());

    fireEvent.click(within(row as HTMLElement).getByRole("button", { name: "Use failure context for workflow orchestration Atlas thread" }));
    await waitFor(() =>
      expect(
        screen.getByDisplayValue(/Review workflow "web-brief-to-file" step "write_file"/),
      ).toBeInTheDocument(),
    );

    fireEvent.click(within(row as HTMLElement).getByRole("button", { name: "Compare branch output for workflow orchestration Atlas thread" }));
    await waitFor(() =>
      expect(
        screen.getByDisplayValue(/Compare the workspace files "notes\/brief\.md" and "notes\/brief-branch\.md"/),
      ).toBeInTheDocument(),
    );

    fireEvent.click(within(row as HTMLElement).getByRole("button", { name: "Plan queue focus for workflow orchestration Atlas thread" }));
    await waitFor(() =>
      expect(
        screen.getByDisplayValue(/Review the workflow queue for Atlas thread\./),
      ).toBeInTheDocument(),
    );

    fireEvent.click(within(row as HTMLElement).getByRole("button", { name: "Draft handoff for workflow orchestration Atlas thread" }));
    await waitFor(() =>
      expect(
        screen.getByDisplayValue(/Prepare a workflow handoff for Atlas thread\./),
      ).toBeInTheDocument(),
    );
  });

  it("surfaces background continuity supervision and handoff in the operator terminal", async () => {
    let backgroundSessionsUrl = "";
    let engineeringMemoryUrl = "";
    let continuityGraphUrl = "";

    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/sessions")) {
        return Promise.resolve(mockResponse([
          { id: "session-1", title: "Atlas background thread", created_at: "", updated_at: "", last_message: null, last_message_role: null },
        ]));
      }
      if (url.includes("/api/goals/tree")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/dashboard")) {
        return Promise.resolve(mockResponse({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }));
      }
      if (url.includes("/api/runtime/status")) {
        return Promise.resolve(mockResponse({
          version: "2026.4.10",
          build_id: "SERAPH_PRIME_v2026.4.10",
          provider: "openrouter",
          model: "openrouter/openai/gpt-4.1-mini",
          model_label: "gpt-4.1-mini",
          llm_logging_enabled: true,
        }));
      }
      if (url.includes("/api/observer/state")) return Promise.resolve(mockResponse({}));
      if (url.includes("/api/audit/events")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/approvals/pending")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/observer/continuity")) {
        return Promise.resolve(mockResponse({
          daemon: { connected: true, pending_notification_count: 0, capture_mode: "balanced" },
          notifications: [],
          queued_insights: [],
          queued_insight_count: 0,
          recent_interventions: [],
          summary: {
            continuity_health: "healthy",
            primary_surface: "workspace",
            recommended_focus: "Atlas background thread",
            actionable_thread_count: 1,
            ambient_item_count: 0,
            pending_notification_count: 0,
            queued_insight_count: 0,
            recent_intervention_count: 0,
            degraded_route_count: 0,
            degraded_source_adapter_count: 0,
            attention_family_count: 0,
            presence_surface_count: 0,
            attention_presence_surface_count: 0,
          },
        }));
      }
      if (url.includes("/api/capabilities/overview")) {
        return Promise.resolve(mockResponse({
          tool_policy_mode: "balanced",
          mcp_policy_mode: "approval",
          approval_mode: "high_risk",
          summary: {},
          native_tools: [],
          workflows: [],
          skills: [],
          mcp_servers: [],
          starter_packs: [],
          catalog_items: [],
          recommendations: [],
          runbooks: [],
          marketplace_flows: [],
        }));
      }
      if (url.includes("/api/extensions")) return Promise.resolve(mockResponse({ extensions: [], summary: {} }));
      if (url.includes("/api/activity/ledger")) {
        return Promise.resolve(mockResponse({ items: [], summary: { llm_call_count: 0, llm_cost_usd: 0, failure_count: 0 } }));
      }
      if (url.includes("/api/operator/control-plane")) {
        return Promise.resolve(mockResponse({
          governance: {
            workspace_mode: "single_operator_guarded_workspace",
            review_posture: "guarded",
            approval_mode: "high_risk",
            tool_policy_mode: "balanced",
            mcp_policy_mode: "approval",
            delegation_enabled: false,
            roles: [],
          },
          usage: {
            window_hours: 24,
            llm_call_count: 0,
            llm_cost_usd: 0,
            input_tokens: 0,
            output_tokens: 0,
            user_triggered_llm_calls: 0,
            autonomous_llm_calls: 0,
            failure_count: 0,
            pending_approvals: 0,
            active_workflows: 1,
            blocked_workflows: 0,
          },
          runtime_posture: {
            runtime: {
              version: "2026.4.10",
              build_id: "SERAPH_PRIME_v2026.4.10",
              provider: "openrouter",
              model: "openrouter/openai/gpt-4.1-mini",
              model_label: "gpt-4.1-mini",
            },
            extensions: {
              total: 0,
              ready: 0,
              degraded: 0,
              governed: 0,
              issue_count: 0,
              degraded_connector_count: 0,
            },
            continuity: {
              continuity_health: "healthy",
              primary_surface: "workspace",
              recommended_focus: "Atlas background thread",
              actionable_thread_count: 1,
              degraded_route_count: 0,
              degraded_source_adapter_count: 0,
              attention_presence_surface_count: 0,
            },
          },
          handoff: {
            pending_approvals: [],
            blocked_workflows: [],
            follow_ups: [],
            review_receipts: [],
          },
        }));
      }
      if (url.includes("/api/operator/workflow-orchestration")) {
        return Promise.resolve(mockResponse({
          summary: {
            tracked_sessions: 0,
            workflow_count: 0,
            active_workflows: 0,
            blocked_workflows: 0,
            awaiting_approval_workflows: 0,
            recoverable_workflows: 0,
          },
          sessions: [],
          workflows: [],
        }));
      }
      if (url.includes("/api/operator/background-sessions")) {
        backgroundSessionsUrl = url;
        return Promise.resolve(mockResponse({
          summary: {
            tracked_sessions: 1,
            background_process_count: 1,
            running_background_process_count: 1,
            sessions_with_branch_handoff: 1,
            sessions_with_active_workflows: 1,
          },
          sessions: [
            {
              session_id: "session-1",
              title: "Atlas background thread",
              latest_updated_at: "2026-04-10T11:05:00Z",
              last_message: "Review Atlas branch output.",
              workflow_count: 1,
              active_workflows: 1,
              blocked_workflows: 0,
              background_process_count: 1,
              running_background_process_count: 1,
              lead_workflow_name: "repo-review",
              lead_workflow_status: "running",
              continue_message: "Continue Atlas branch review.",
              branch_handoff_available: true,
              branch_handoff: {
                available: true,
                target_type: "workflow_branch",
                continue_message: "Continue Atlas branch review.",
                workflow_name: "repo-review",
                run_identity: "root-1",
                branch_kind: "branch_from_checkpoint",
                branch_depth: 1,
                artifact_paths: ["notes/atlas-review.md"],
                resume_checkpoint_label: "Draft review",
                summary: "Branch handoff is ready for review publishing.",
              },
              lead_process: {
                process_id: "proc-1",
                pid: 1234,
                command: "python3",
                args: ["worker.py"],
                cwd: "/workspace",
                status: "running",
                started_at: "2026-04-10T11:03:00Z",
                session_id: "session-1",
              },
              background_processes: [],
            },
          ],
        }));
      }
      if (url.includes("/api/operator/engineering-memory")) {
        engineeringMemoryUrl = url;
        return Promise.resolve(mockResponse({
          summary: {
            query: null,
            tracked_bundles: 1,
            repository_bundle_count: 0,
            pull_request_bundle_count: 1,
            work_item_bundle_count: 0,
            search_match_count: 1,
          },
          bundles: [
            {
              reference: "seraph-quest/seraph/pull/390",
              target_kind: "pull_request",
              repository_reference: "seraph-quest/seraph",
              latest_updated_at: "2026-04-10T11:04:00Z",
              workflow_count: 1,
              approval_count: 0,
              audit_event_count: 1,
              session_match_count: 1,
              thread_ids: ["session-1"],
              thread_labels: ["Atlas background thread"],
              artifact_paths: ["notes/atlas-review.md"],
              continue_message: "Continue review for seraph-quest/seraph/pull/390.",
              session_matches: [
                {
                  session_id: "session-1",
                  title: "Atlas background thread",
                  matched_at: "2026-04-10T11:03:00Z",
                  snippet: "Need to finish seraph-quest/seraph/pull/390 review.",
                  source: "message",
                },
              ],
            },
          ],
        }));
      }
      if (url.includes("/api/operator/continuity-graph")) {
        continuityGraphUrl = url;
        return Promise.resolve(mockResponse({
          summary: {
            continuity_health: "attention",
            primary_surface: "workspace",
            recommended_focus: "Atlas background thread",
            tracked_sessions: 1,
            workflow_count: 1,
            approval_count: 0,
            notification_count: 1,
            queued_insight_count: 0,
            intervention_count: 1,
            artifact_count: 1,
            edge_count: 4,
          },
          sessions: [
            {
              id: "session:session-1",
              kind: "session",
              title: "Atlas background thread",
              summary: "Atlas branch review is waiting.",
              updated_at: "2026-04-10T11:05:00Z",
              thread_id: "session-1",
              continue_message: "Continue Atlas branch review.",
              metadata: {
                pending_notification_count: 1,
                queued_insight_count: 0,
                recent_intervention_count: 1,
                item_count: 3,
                primary_surface: "native_notification",
                continuity_surface: "native_notification",
                workflow_count: 1,
                approval_count: 0,
                notification_count: 1,
                intervention_count: 1,
                artifact_count: 1,
                linked_item_count: 3,
              },
            },
          ],
          nodes: [],
          edges: [],
        }));
      }
      if (url.includes("/api/workflows/runs")) {
        return Promise.resolve(mockResponse({
          runs: [
            {
              id: "run-root",
              tool_name: "workflow_repo_review",
              run_identity: "root-1",
              root_run_identity: "root-1",
              parent_run_identity: null,
              workflow_name: "repo-review",
              summary: "Review Atlas branch output before publish.",
              status: "running",
              availability: "ready",
              session_id: "session-1",
              started_at: "2026-04-10T11:00:00Z",
              updated_at: "2026-04-10T11:05:00Z",
              thread_id: "session-1",
              thread_label: "Atlas background thread",
              continue_message: "Continue Atlas branch review.",
              thread_continue_message: "Continue Atlas branch review.",
              output_path: "notes/atlas-review.md",
              artifact_paths: ["notes/atlas-review.md"],
              step_records: [],
              pending_approval_count: 0,
              pending_approval_ids: [],
              checkpoint_candidate_count: 1,
              checkpoint_candidates: [],
              retry_from_step_available: false,
              replay_allowed: true,
              replay_recommended_actions: [],
              step_focus: null,
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

    const continuity = await screen.findByRole("region", { name: /background continuity/i });
    await waitFor(() => expect(continuity).toHaveTextContent(/1 sessions · 1\/1 running procs · 1 bundles · 4 edges/i));
    expect(continuity).toHaveTextContent(/1 handoff-ready · 1 active sessions · 0 repos · 1 prs · 0 work items · focus Atlas background thread/i);
    expect(continuity).toHaveTextContent(/Atlas background thread/);
    expect(continuity).toHaveTextContent(/repo-review · running/i);
    expect(continuity).toHaveTextContent(/seraph-quest\/seraph\/pull\/390/);
    expect(backgroundSessionsUrl).not.toContain("session_id=");
    expect(engineeringMemoryUrl).not.toContain("session_id=");
    expect(continuityGraphUrl).not.toContain("session_id=");

    fireEvent.click(within(continuity).getByRole("button", { name: /Continue background continuity for Atlas background thread/i }));
    await waitFor(() =>
      expect(screen.getByDisplayValue(/Continue Atlas branch review\./i)).toBeInTheDocument(),
    );
  }, 30000);

  it("surfaces evidence shortcuts and keyboard-first triage control", async () => {
    useChatStore.setState({
      messages: [{
        id: "trace-1",
        role: "step",
        content: "write_file saved notes/brief.md",
        timestamp: new Date("2026-03-18T12:06:30Z").getTime(),
        sessionId: "session-2",
        stepNumber: 2,
        toolUsed: "write_file",
      }],
      sessionId: "session-1",
      sessions: [
        { id: "session-1", title: "Session 1", created_at: "", updated_at: "", last_message: null, last_message_role: null },
        { id: "session-2", title: "Atlas thread", created_at: "", updated_at: "", last_message: null, last_message_role: null },
      ],
    });

    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/sessions")) {
        return Promise.resolve(mockResponse([
          { id: "session-1", title: "Session 1", created_at: "", updated_at: "", last_message: null, last_message_role: null },
          { id: "session-2", title: "Atlas thread", created_at: "", updated_at: "", last_message: null, last_message_role: null },
        ]));
      }
      if (url.includes("/api/goals/tree")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/dashboard")) {
        return Promise.resolve(mockResponse({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }));
      }
      if (url.includes("/api/observer/state")) return Promise.resolve(mockResponse({}));
      if (url.includes("/api/audit/events")) {
        return Promise.resolve(mockResponse([
          {
            id: "audit-write",
            session_id: "session-2",
            event_type: "tool_result",
            tool_name: "write_file",
            risk_level: "medium",
            policy_mode: "balanced",
            summary: "Saved brief draft",
            created_at: "2026-03-18T12:06:00Z",
            details: {
              arguments: { file_path: "notes/brief.md" },
            },
          },
        ]));
      }
      if (url.includes("/api/approvals/approval-run/approve")) {
        return Promise.resolve(mockResponse({ status: "approved" }));
      }
      if (url.includes("/api/approvals/pending")) {
        return Promise.resolve(mockResponse([
          {
            id: "approval-run",
            session_id: "session-2",
            thread_id: "session-2",
            thread_label: "Atlas thread",
            tool_name: "shell_execute",
            risk_level: "high",
            status: "pending",
            summary: "Approve Atlas shell command",
            created_at: "2026-03-18T12:03:00Z",
            resume_message: "Continue Atlas shell approval",
          },
        ]));
      }
      if (url.includes("/api/observer/continuity")) {
        return Promise.resolve(mockResponse({
          daemon: { connected: false, pending_notification_count: 0, capture_mode: "balanced" },
          notifications: [],
          queued_insights: [],
          queued_insight_count: 0,
          recent_interventions: [],
          reach: { route_statuses: [] },
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
          workflows: [],
          skills: [],
          mcp_servers: [],
          starter_packs: [],
          catalog_items: [],
          recommendations: [],
          runbooks: [],
          extension_packages: [],
        }));
      }
      if (url.includes("/api/workflows/runs")) {
        return Promise.resolve(mockResponse({
          runs: [
            {
              id: "run-root",
              tool_name: "workflow_web_brief_to_file",
              workflow_name: "web-brief-to-file",
              session_id: "session-2",
              status: "degraded",
              started_at: "2026-03-18T12:00:00Z",
              updated_at: "2026-03-18T12:04:00Z",
              summary: "workflow_web_brief_to_file failed at write_file",
              step_tools: ["web_search", "write_file"],
              step_records: [
                {
                  id: "write_file",
                  index: 1,
                  tool: "write_file",
                  status: "failed",
                  argument_keys: ["file_path"],
                  artifact_paths: ["notes/brief.md"],
                  error_summary: "write_file blocked by approval",
                },
              ],
              artifact_paths: ["notes/brief.md"],
              continued_error_steps: ["write_file"],
              risk_level: "medium",
              pending_approval_count: 0,
              pending_approval_ids: [],
              thread_id: "session-2",
              thread_label: "Atlas thread",
              replay_allowed: true,
              thread_continue_message: "Continue Atlas workflow",
              run_identity: "root-1",
              root_run_identity: "root-1",
              checkpoint_context_available: true,
            },
            {
              id: "run-branch",
              tool_name: "workflow_web_brief_to_file",
              workflow_name: "web-brief-to-file",
              session_id: "session-2",
              status: "running",
              started_at: "2026-03-18T12:05:00Z",
              updated_at: "2026-03-18T12:06:00Z",
              summary: "workflow_web_brief_to_file branch running",
              step_tools: ["write_file"],
              step_records: [],
              artifact_paths: ["notes/branch-brief.md"],
              continued_error_steps: [],
              risk_level: "medium",
              pending_approval_count: 0,
              pending_approval_ids: [],
              thread_id: "session-2",
              thread_label: "Atlas thread",
              replay_allowed: true,
              thread_continue_message: "Continue Atlas branch",
              run_identity: "branch-1",
              parent_run_identity: "root-1",
              root_run_identity: "root-1",
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

    const evidence = await screen.findByLabelText("Evidence shortcuts");
    expect(await within(evidence).findByText("artifact: notes/brief.md")).toBeInTheDocument();
    expect(await within(evidence).findByText("trace: write_file")).toBeInTheDocument();
    expect(await within(evidence).findByText("approval context: shell_execute")).toBeInTheDocument();

    fireEvent.click(within(evidence).getByRole("button", { name: "Draft next step for artifact: notes/brief.md" }));
    await waitFor(() =>
      expect(screen.getByDisplayValue(/Review next steps for artifact "notes\/brief\.md"\./)).toBeInTheDocument(),
    );

    fireEvent.keyDown(window, { key: "I", shiftKey: true });
    await waitFor(() => expect(screen.getByText("approval-run")).toBeInTheDocument());

    fireEvent.keyDown(window, { key: "A", shiftKey: true });
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/approvals/approval-run/approve"),
        expect.objectContaining({ method: "POST" }),
      ),
    );

    fireEvent.keyDown(window, { key: "C", shiftKey: true });
    await waitFor(() => expect(screen.getByDisplayValue("Continue Atlas shell approval")).toBeInTheDocument());

    fireEvent.keyDown(window, { key: "R", shiftKey: true });
    await waitFor(() =>
      expect(screen.getByDisplayValue(/Redirect workflow "web-brief-to-file" from its current state\./)).toBeInTheDocument(),
    );

    fireEvent.keyDown(window, { key: "E", shiftKey: true });
    await waitFor(() => expect(screen.getByRole("button", { name: "Use In Command Bar" })).toBeInTheDocument());

    fireEvent.keyDown(window, { key: "J", shiftKey: true });
    await waitFor(() =>
      expect(screen.getByDisplayValue(/Review next steps for artifact "notes\/brief\.md"\./)).toBeInTheDocument(),
    );

    fireEvent.keyDown(window, { key: "W", shiftKey: true });
    await waitFor(() =>
      expect(screen.getAllByText("workflow_web_brief_to_file failed at write_file").length).toBeGreaterThan(0),
    );

    fireEvent.keyDown(window, { key: "M", shiftKey: true });
    await waitFor(() => expect(screen.getByRole("button", { name: "Open Source Run" })).toBeInTheDocument());

    fireEvent.keyDown(window, { key: "S", shiftKey: true });
    await waitFor(() =>
      expect((document.querySelector(".cockpit-inspector-body") as HTMLElement).textContent).toContain("workflow_web_brief_to_file failed at write_file"),
    );

    fireEvent.keyDown(window, { key: "D", shiftKey: true });
    await waitFor(() => expect(screen.getByDisplayValue("Continue Atlas workflow")).toBeInTheDocument());

    fireEvent.keyDown(window, { key: "Q", shiftKey: true });
    await waitFor(() =>
      expect(
        screen.getByDisplayValue(/Review workflow "web-brief-to-file" step "write_file"/),
      ).toBeInTheDocument(),
    );

    fireEvent.keyDown(window, { key: "X", shiftKey: true });
    await waitFor(() =>
      expect(
        screen.getByDisplayValue(
          'Compare the workspace files "notes/brief.md" and "notes/branch-brief.md". Summarize the key differences, what changed between these artifact outputs, and which file is the better base for the next step.',
        ),
      ).toBeInTheDocument(),
    );

    fireEvent.keyDown(window, { key: "U", shiftKey: true });
    await waitFor(() =>
      expect(screen.getByDisplayValue('Use the workspace file "notes/brief.md" as context for the next action.')).toBeInTheDocument(),
    );
  }, 30000);

  it("prefers the newest artifact across workflow runs for evidence shortcuts", async () => {
    useChatStore.setState({
      messages: [],
      sessionId: "session-1",
      sessions: [
        { id: "session-1", title: "Atlas thread", created_at: "", updated_at: "", last_message: null, last_message_role: null },
      ],
    });

    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/sessions")) {
        return Promise.resolve(mockResponse([
          { id: "session-1", title: "Atlas thread", created_at: "", updated_at: "", last_message: null, last_message_role: null },
        ]));
      }
      if (url.includes("/api/goals/tree")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/dashboard")) {
        return Promise.resolve(mockResponse({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }));
      }
      if (url.includes("/api/observer/state")) return Promise.resolve(mockResponse({}));
      if (url.includes("/api/audit/events")) {
        return Promise.resolve(mockResponse([
          {
            id: "audit-older-artifact",
            session_id: "session-1",
            event_type: "tool_result",
            tool_name: "write_file",
            risk_level: "medium",
            policy_mode: "balanced",
            summary: "Saved older notes artifact",
            created_at: "2026-03-18T12:02:00Z",
            details: {
              arguments: { file_path: "notes/older.md" },
            },
          },
          {
            id: "audit-newer-artifact",
            session_id: "session-1",
            event_type: "tool_result",
            tool_name: "write_file",
            risk_level: "low",
            policy_mode: "balanced",
            summary: "Saved newer notes artifact",
            created_at: "2026-03-18T12:05:30Z",
            details: {
              arguments: { file_path: "notes/newer.md" },
            },
          },
        ]));
      }
      if (url.includes("/api/approvals/pending")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/observer/continuity")) {
        return Promise.resolve(mockResponse({
          daemon: { connected: false, pending_notification_count: 0, capture_mode: "balanced" },
          notifications: [],
          queued_insights: [],
          queued_insight_count: 0,
          recent_interventions: [],
          reach: { route_statuses: [] },
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
          workflows: [],
          skills: [],
          mcp_servers: [],
          starter_packs: [],
          catalog_items: [],
          recommendations: [],
          runbooks: [],
          extension_packages: [],
        }));
      }
      if (url.includes("/api/workflows/runs")) {
        return Promise.resolve(mockResponse({
          runs: [
            {
              id: "run-older-artifact",
              tool_name: "workflow_web_brief_to_file",
              workflow_name: "web-brief-to-file",
              session_id: "session-1",
              status: "degraded",
              started_at: "2026-03-18T12:00:00Z",
              updated_at: "2026-03-18T12:10:00Z",
              summary: "older artifact with newer workflow metadata",
              step_tools: ["write_file"],
              step_records: [],
              artifact_paths: ["notes/older.md"],
              continued_error_steps: [],
              risk_level: "medium",
              pending_approval_count: 0,
              pending_approval_ids: [],
              thread_id: "session-1",
              thread_label: "Atlas thread",
              replay_allowed: true,
              run_identity: "run-older-artifact",
              root_run_identity: "run-older-artifact",
            },
            {
              id: "run-newer-artifact",
              tool_name: "workflow_daily_summary",
              workflow_name: "daily-summary",
              session_id: "session-1",
              status: "succeeded",
              started_at: "2026-03-18T12:01:00Z",
              updated_at: "2026-03-18T12:09:00Z",
              summary: "newer artifact with older workflow metadata",
              step_tools: ["write_file"],
              step_records: [],
              artifact_paths: ["notes/newer.md"],
              continued_error_steps: [],
              risk_level: "low",
              pending_approval_count: 0,
              pending_approval_ids: [],
              thread_id: "session-1",
              thread_label: "Atlas thread",
              replay_allowed: true,
              run_identity: "run-newer-artifact",
              root_run_identity: "run-newer-artifact",
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

    const evidence = await screen.findByLabelText("Evidence shortcuts");
    await waitFor(
      () => expect(within(evidence).getByText("artifact: notes/newer.md")).toBeInTheDocument(),
      { timeout: 5000 },
    );
    expect(within(evidence).queryByText("artifact: notes/older.md")).not.toBeInTheDocument();

    fireEvent.keyDown(window, { key: "E", shiftKey: true });
    const useButton = await screen.findByRole("button", { name: "Use In Command Bar" });
    fireEvent.click(useButton);
    await waitFor(() =>
      expect(screen.getByDisplayValue('Use the workspace file "notes/newer.md" as context for the next action.')).toBeInTheDocument(),
    );
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

  it("surfaces anticipatory repair and backup branch choices in workflow orchestration", async () => {
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
        return Promise.resolve(mockResponse({ daemon: {}, notifications: [], queued_insights: [], recent_interventions: [] }));
      }
      if (url.includes("/api/workflows") && !url.includes("/api/workflows/runs")) return Promise.resolve(mockResponse({ workflows: [] }));
      if (url.includes("/api/skills")) return Promise.resolve(mockResponse({ skills: [] }));
      if (url.includes("/api/mcp/servers")) return Promise.resolve(mockResponse({ servers: [] }));
      if (url.includes("/api/tools")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/operator/control-plane")) {
        return Promise.resolve(mockResponse({
          governance: { workspace_mode: "governed", review_posture: "required", approval_mode: "high_risk", tool_policy_mode: "balanced", mcp_policy_mode: "approval", delegation_enabled: true, roles: [] },
          usage: { window_hours: 24, llm_call_count: 0, llm_cost_usd: 0, input_tokens: 0, output_tokens: 0, user_triggered_llm_calls: 0, autonomous_llm_calls: 0, failure_count: 0, pending_approvals: 0, active_workflows: 1, blocked_workflows: 0 },
          runtime_posture: { runtime: { provider: "openrouter", model: "test", model_label: "test", build_id: "test" }, extensions: { total: 0, ready: 0, degraded: 0, governed: 0, issue_count: 0, degraded_connector_count: 0 }, continuity: { continuity_health: "healthy", primary_surface: "workspace", recommended_focus: "workflow orchestration", actionable_thread_count: 1, degraded_route_count: 0, degraded_source_adapter_count: 0, attention_presence_surface_count: 0 } },
          handoff: { pending_approvals: [], blocked_workflows: [], follow_ups: [], review_receipts: [] },
        }));
      }
      if (url.includes("/api/operator/benchmark-proof")) {
        return Promise.resolve(mockResponse({
          summary: { suite_count: 8, scenario_count: 45, benchmark_posture: "deterministic_proof_backed", operator_status: "operator_visible", remaining_gap: "live_provider_and_real_computer_use_depth", governed_improvement_status: "review_gated", memory_benchmark_posture: "ci_gated_operator_visible", user_model_benchmark_posture: "ci_gated_operator_visible", workflow_endurance_benchmark_posture: "ci_gated_operator_visible", trust_boundary_benchmark_posture: "ci_gated_operator_visible" },
          suites: [],
          memory_benchmark: null,
          user_model_benchmark: null,
          workflow_endurance_benchmark: null,
          trust_boundary_benchmark: null,
          governed_improvement: { target_count: 0, target_types: [], required_suite_count: 8, gate_policy: { min_review_ready_score: 0.7, min_strong_score: 0.9, requires_human_review: true, blocks_on_constraint_failure: true, required_benchmark_suites: [], proof_contract: "deterministic_benchmark_suites_plus_review_receipts" } },
        }));
      }
      if (url.includes("/api/operator/workflow-orchestration")) {
        return Promise.resolve(mockResponse({
          summary: {
            tracked_sessions: 1,
            workflow_count: 1,
            active_workflows: 1,
            blocked_workflows: 0,
            awaiting_approval_workflows: 0,
            recoverable_workflows: 0,
            long_running_workflows: 1,
            compacted_workflows: 1,
            total_step_count: 4,
            compacted_step_count: 1,
            boundary_blocked_workflows: 0,
            repair_ready_workflows: 0,
            branch_ready_workflows: 1,
            anticipatory_ready_workflows: 1,
            backup_branch_ready_workflows: 1,
            fidelity_watch_workflows: 0,
            stalled_workflows: 0,
            output_debugger_ready_workflows: 1,
            attention_sessions: 1,
          },
          sessions: [
            {
              thread_id: "session-1",
              thread_label: "Release thread",
              workflow_count: 1,
              active_workflows: 1,
              blocked_workflows: 0,
              awaiting_approval_workflows: 0,
              recoverable_workflows: 0,
              latest_updated_at: "2026-04-11T08:45:00Z",
              lead_run_identity: "release-root",
              lead_workflow_name: "release-brief",
              lead_status: "running",
              lead_summary: "Preparing release publication.",
              continue_message: "Continue release brief.",
              total_step_count: 4,
              compacted_step_count: 1,
              compacted_workflow_count: 1,
              long_running_workflow_count: 1,
              artifact_count: 1,
              lead_state_capsule: "4 steps · 1 compacted · 1 artifact · preserves checkpoint branch",
              boundary_blocked_workflows: 0,
              repair_ready_workflows: 0,
              branch_ready_workflows: 1,
              anticipatory_ready_workflows: 1,
              backup_branch_ready_workflows: 1,
              fidelity_watch_workflows: 0,
              stalled_workflows: 0,
              output_debugger_ready_workflows: 1,
              queue_state: "active",
              queue_position: 1,
              queue_reason: "1 workflow is actively progressing.",
              attention_summary: "1 anticipatory ready · 1 backup branch ready · 1 debugger ready",
              queue_draft: "Review the workflow queue for Release thread.",
              handoff_draft: "Prepare a workflow handoff for Release thread.",
              lead_recommended_recovery_path: "continue_thread",
              lead_anticipatory_risk_level: "elevated",
              lead_anticipatory_summary: "anticipatory repair ready · backup branch from draft (write_file)",
              lead_backup_branch_label: "draft (write_file)",
              lead_backup_branch_draft: 'Run workflow "release-brief" with _seraph_resume_from_step="draft".',
              lead_anticipatory_repair_draft: 'Before continuing workflow "release-brief", review the next risky step and prepare a safer continuation path.',
              lead_condensation_fidelity_state: "strong",
              lead_condensation_fidelity_summary: "visible 3/4 steps · preserves checkpoint branch · 1 output histories",
              lead_output_path: "notes/release-brief.md",
              lead_related_output_paths: [],
              lead_output_history: [{ path: "notes/release-brief.md", run_identity: "release-root", summary: "Preparing release publication.", status: "running", branch_kind: null, updated_at: "2026-04-11T08:45:00Z", is_primary: true }],
              lead_latest_branch_run_identity: null,
              lead_latest_branch_summary: null,
              lead_step_focus: { kind: "active", step_id: "review", tool: "diff_compare", status: "running", summary: "Comparing candidate release notes", recovery_action_count: 0, is_recoverable: false },
            },
          ],
          workflows: [
            {
              id: "run-1",
              tool_name: "workflow_release_brief",
              run_identity: "release-root",
              root_run_identity: "release-root",
              parent_run_identity: null,
              workflow_name: "release-brief",
              summary: "Preparing release publication.",
              status: "running",
              availability: "ready",
              session_id: "session-1",
              started_at: "2026-04-11T08:00:00Z",
              updated_at: "2026-04-11T08:45:00Z",
              thread_id: "session-1",
              thread_label: "Release thread",
              continue_message: "Continue release brief.",
              thread_continue_message: "Continue release brief.",
              output_path: "notes/release-brief.md",
              artifact_paths: ["notes/release-brief.md"],
              step_records: [
                { id: "collect", index: 1, tool: "web_search", status: "succeeded", result_summary: "Collected release notes" },
                { id: "draft", index: 2, tool: "write_file", status: "succeeded", result_summary: "Drafted release brief" },
                { id: "review", index: 3, tool: "diff_compare", status: "running", result_summary: "Comparing candidate release notes" },
              ],
              pending_approval_count: 0,
              pending_approval_ids: [],
              checkpoint_candidate_count: 1,
              checkpoint_candidates: [{ step_id: "draft", label: "draft (write_file)", status: "succeeded", resume_draft: 'Run workflow "release-brief" with _seraph_resume_from_step="draft".', resume_supported: true }],
              retry_from_step_available: false,
              retry_from_step_draft: null,
              replay_allowed: true,
              replay_block_reason: null,
              replay_recommended_actions: [],
              step_focus: { kind: "active", step_id: "review", tool: "diff_compare", status: "running", summary: "Comparing candidate release notes", recovery_action_count: 0, is_recoverable: false },
              is_long_running: true,
              is_compacted: true,
              duration_minutes: 45,
              step_count: 4,
              visible_step_count: 3,
              compacted_step_count: 1,
              artifact_count: 1,
              preserved_recovery_paths: ["checkpoint_branch"],
              recent_step_labels: ["collect / web_search / succeeded", "draft / write_file / succeeded", "review / diff_compare / running"],
              state_capsule: "4 steps · 1 compacted · 1 artifact · preserves checkpoint branch",
              recovery_density: { recommended_path: "continue_thread", approval_pending: false, boundary_blocked: false, retry_ready: false, checkpoint_ready: true, repair_ready: false, branch_ready: true, replay_ready: true, stalled: false, checkpoint_candidate_count: 1, recovery_action_count: 0, repair_action_types: [], repair_hint: null, failure_step_id: null, failure_step_tool: null },
              output_debugger: { family_run_count: 1, branch_run_count: 0, history_output_count: 1, primary_output_path: "notes/release-brief.md", related_output_paths: [], history_outputs: [{ path: "notes/release-brief.md", run_identity: "release-root", summary: "Preparing release publication.", status: "running", branch_kind: null, updated_at: "2026-04-11T08:45:00Z", is_primary: true }], latest_branch_run_identity: null, latest_branch_summary: null, latest_branch_status: null, latest_branch_output_path: null, comparison_ready: false, checkpoint_labels: ["draft (write_file)"] },
              condensation_fidelity: { state: "strong", watch_required: false, visible_step_count: 3, total_step_count: 4, preserved_path_count: 1, history_output_count: 1, branch_run_count: 0, summary: "visible 3/4 steps · preserves checkpoint branch · 1 output histories" },
              anticipatory_plan: { risk_level: "elevated", anticipatory_ready: true, signal_count: 3, signals: ["long_running", "active_step", "checkpoint_branch_available"], summary: "anticipatory repair ready · backup branch from draft (write_file)", backup_branch_ready: true, backup_branch_step_id: "draft", backup_branch_label: "draft (write_file)", backup_branch_draft: 'Run workflow "release-brief" with _seraph_resume_from_step="draft".', anticipatory_repair_draft: 'Before continuing workflow "release-brief", review the next risky step and prepare a safer continuation path.', family_failure_count: 0 },
            },
          ],
        }));
      }
      if (url.includes("/api/operator/background-sessions")) return Promise.resolve(mockResponse({ summary: { tracked_sessions: 0, background_process_count: 0, running_background_process_count: 0, sessions_with_branch_handoff: 0, sessions_with_active_workflows: 0 }, sessions: [] }));
      if (url.includes("/api/operator/engineering-memory")) return Promise.resolve(mockResponse({ summary: { query: null, tracked_bundles: 0, repository_bundle_count: 0, pull_request_bundle_count: 0, work_item_bundle_count: 0, search_match_count: 0 }, bundles: [] }));
      if (url.includes("/api/operator/continuity-graph")) return Promise.resolve(mockResponse({ summary: { continuity_health: null, primary_surface: null, recommended_focus: null, tracked_sessions: 0, workflow_count: 0, approval_count: 0, notification_count: 0, queued_insight_count: 0, intervention_count: 0, artifact_count: 0, edge_count: 0 }, sessions: [], nodes: [], edges: [] }));
      if (url.includes("/api/settings/tool-policy-mode")) return Promise.resolve(mockResponse({ mode: "balanced" }));
      if (url.includes("/api/settings/mcp-policy-mode")) return Promise.resolve(mockResponse({ mode: "approval" }));
      if (url.includes("/api/settings/approval-mode")) return Promise.resolve(mockResponse({ mode: "high_risk" }));
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={() => {}} />);

    const orchestration = await screen.findByLabelText("Workflow orchestration");
    const row = (await within(orchestration).findByText("Release thread")).closest(".cockpit-operator-row--entry");
    expect(row).not.toBeNull();
    expect(row as HTMLElement).toHaveTextContent(/1 anticipatory-ready · 1 backup-branch ready/i);
    expect(row as HTMLElement).toHaveTextContent(/anticipatory elevated · anticipatory repair ready · backup branch from draft \(write_file\)/i);
    expect(row as HTMLElement).toHaveTextContent(/fidelity strong · visible 3\/4 steps · preserves checkpoint branch/i);
    expect(within(row as HTMLElement).getByRole("button", { name: "Draft backup branch for workflow orchestration Release thread" })).toBeInTheDocument();
    expect(within(row as HTMLElement).getByRole("button", { name: "Draft anticipatory repair for workflow orchestration Release thread" })).toBeInTheDocument();

    fireEvent.click(within(row as HTMLElement).getByRole("button", { name: "Draft backup branch for workflow orchestration Release thread" }));
    await waitFor(() =>
      expect(screen.getByDisplayValue('Run workflow "release-brief" with _seraph_resume_from_step="draft".')).toBeInTheDocument(),
    );

    fireEvent.click(within(row as HTMLElement).getByRole("button", { name: "Draft anticipatory repair for workflow orchestration Release thread" }));
    await waitFor(() =>
      expect(screen.getByDisplayValue(/Before continuing workflow "release-brief"/)).toBeInTheDocument(),
    );
  });

  it("drafts the starter-pack command after a successful bootstrap", async () => {
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
            marketplace_flows_ready: 1,
            marketplace_flows_total: 2,
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
    expect((await screen.findAllByText("Enable Research Pack contributions with high-risk capabilities")).length).toBeGreaterThan(0);
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
    const operatorPane = screen.getByText("Operator terminal").closest("section");
    expect(within(operatorPane as HTMLElement).getByText("browser providers")).toBeInTheDocument();
    expect(within(operatorPane as HTMLElement).getAllByText("extension boundaries").length).toBeGreaterThan(0);
    expect(within(operatorPane as HTMLElement).getAllByText("Browser Ops Pack").length).toBeGreaterThan(0);
    expect(within(operatorPane as HTMLElement).getAllByText(/lifecycle approval network/).length).toBeGreaterThan(0);
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

  it("surfaces guardian confidence and judgment proof in the guardian pane", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/sessions")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/tree")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/dashboard")) {
        return Promise.resolve(mockResponse({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }));
      }
      if (url.includes("/api/observer/state")) {
        return Promise.resolve(mockResponse({
          user_state: "focused",
          interruption_mode: "minimal",
          active_window: "VS Code",
          is_working_hours: true,
          screen_context: "Reviewing Atlas release notes",
          active_goals_summary: "Ship Atlas safely",
          upcoming_events: [],
        }));
      }
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
          marketplace_flows: [],
        }));
      }
      if (url.includes("/api/extensions")) return Promise.resolve(mockResponse({ extensions: [], summary: {} }));
      if (url.includes("/api/activity/ledger")) {
        return Promise.resolve(mockResponse({ items: [], summary: { llm_call_count: 0, llm_cost_usd: 0, failure_count: 0 } }));
      }
      if (url.includes("/api/operator/control-plane")) {
        return Promise.resolve(mockResponse({
          governance: { workspace_mode: "single_operator_guarded_workspace", review_posture: "", approval_mode: "high_risk", tool_policy_mode: "balanced", mcp_policy_mode: "approval", delegation_enabled: true, roles: [] },
          usage: { window_hours: 24, llm_call_count: 0, llm_cost_usd: 0, input_tokens: 0, output_tokens: 0, user_triggered_llm_calls: 0, autonomous_llm_calls: 0, failure_count: 0, pending_approvals: 0, active_workflows: 0, blocked_workflows: 0 },
          runtime_posture: {
            runtime: { version: "2026.4.10", build_id: "SERAPH_TEST", provider: "openrouter", model: "openrouter/openai/gpt-4.1-mini", model_label: "gpt-4.1-mini" },
            extensions: { total: 0, ready: 0, degraded: 0, governed: 0, issue_count: 0, degraded_connector_count: 0 },
            continuity: { continuity_health: "ready", primary_surface: "presence", recommended_focus: null, actionable_thread_count: 0, degraded_route_count: 0, degraded_source_adapter_count: 0, attention_presence_surface_count: 0 },
          },
          handoff: { pending_approvals: [], blocked_workflows: [], follow_ups: [], review_receipts: [] },
        }));
      }
      if (url.includes("/api/operator/benchmark-proof")) {
        return Promise.resolve(mockResponse({
          summary: {
            suite_count: 8,
            scenario_count: 45,
            benchmark_posture: "deterministic_proof_backed",
            operator_status: "operator_visible",
            remaining_gap: "live_provider_and_real_computer_use_depth",
            governed_improvement_status: "review_gated",
            memory_benchmark_posture: "ci_gated_operator_visible",
            user_model_benchmark_posture: "ci_gated_operator_visible",
            workflow_endurance_benchmark_posture: "ci_gated_operator_visible",
            trust_boundary_benchmark_posture: "ci_gated_operator_visible",
          },
          memory_benchmark: {
            summary: {
              suite_name: "guardian_memory_quality",
              benchmark_posture: "ci_gated_operator_visible",
              operator_status: "memory_proof_visible",
              scenario_count: 8,
              dimension_count: 5,
              failure_mode_count: 5,
              active_failure_count: 2,
              contradiction_state: "conflict_reconciled",
              selective_forgetting_state: "active",
            },
            failure_report: [
              {
                type: "contradiction_reconciled",
                summary: "Atlas release date corrected after contradictory note.",
                reason: "contradiction",
              },
              {
                type: "selective_forgetting_archive",
                summary: "Archived obsolete Hermes coordination draft.",
                reason: "archived",
              },
            ],
            policy: {
              retrieval_ranking_policy: "contradiction_aware_query_and_project_weighted",
              ci_gate_mode: "required_benchmark_suite",
            },
          },
          user_model_benchmark: {
            summary: {
              suite_name: "guardian_user_model_restraint",
              benchmark_posture: "ci_gated_operator_visible",
              operator_status: "guardian_state_visible",
              scenario_count: 4,
              dimension_count: 5,
              failure_mode_count: 5,
              active_failure_count: 0,
              clarification_policy_state: "required_on_high_ambiguity",
              restraint_policy_state: "clarify_or_wait_before_unverified_personalization",
            },
            failure_report: [],
            policy: {
              canonical_authority: "guardian_world_model",
              clarify_before_action_policy: "required_on_high_ambiguity",
              personalization_override_policy: "forbidden_without_canonical_receipt",
              operator_visibility: "facet_evidence_watchpoints_and_restraint_receipts",
              ci_gate_mode: "required_benchmark_suite",
            },
          },
          workflow_endurance_benchmark: {
            summary: {
              suite_name: "workflow_endurance_and_repair",
              benchmark_posture: "ci_gated_operator_visible",
              operator_status: "workflow_orchestration_visible",
              scenario_count: 4,
              dimension_count: 5,
              failure_mode_count: 5,
              active_failure_count: 0,
              anticipatory_repair_state: "checkpoint_and_pre_repair_visible",
              condensation_fidelity_state: "recovery_paths_and_output_history_retained",
              branch_continuity_state: "backup_branch_operator_selectable",
            },
            failure_report: [],
            policy: {
              anticipatory_repair_policy: "prepare_repair_and_backup_branch_before_obvious_failure_points",
              backup_branch_policy: "checkpoint_backed_branch_receipts_must_remain_operator_selectable",
              condensation_fidelity_policy: "compaction_must_preserve_recovery_paths_and_output_lineage",
              operator_visibility: "workflow_orchestration_and_benchmark_visible",
              ci_gate_mode: "required_benchmark_suite",
            },
          },
          trust_boundary_benchmark: {
            summary: {
              suite_name: "trust_boundary_and_safety_receipts",
              benchmark_posture: "ci_gated_operator_visible",
              operator_status: "safety_receipts_visible",
              scenario_count: 7,
              dimension_count: 5,
              failure_mode_count: 6,
              active_failure_count: 1,
              secret_egress_state: "field_scoped_egress_allowlist_required",
              delegation_partition_state: "vault_and_background_partitioned",
              workflow_replay_state: "boundary_drift_blocks_replay",
              operator_receipt_state: "benchmark_and_runtime_visible",
            },
            failure_report: [
              {
                type: "benchmark_regression",
                summary: "secret ref egress regression",
                reason: "deterministic_eval_failure",
              },
            ],
            policy: {
              secret_egress_policy: "field_scoped_secret_refs_plus_required_credential_egress_allowlist",
              delegation_partition_policy: "vault_operations_route_to_vault_keeper",
              background_execution_policy: "session_partitioned_managed_process_recovery",
              workflow_replay_policy: "trust_boundary_drift_blocks_replay_and_resume",
              operator_visibility: "benchmark_proof_plus_runtime_receipts_visible",
              receipt_surfaces: [
                "/api/operator/benchmark-proof",
                "/api/operator/trust-boundary-benchmark",
                "/api/operator/workflow-orchestration",
                "/api/operator/background-sessions",
                "/api/activity/ledger",
              ],
              ci_gate_mode: "required_benchmark_suite",
            },
          },
          suites: [
            {
              name: "guardian_memory_quality",
              label: "Guardian memory benchmark",
              description: "Contradiction-aware memory benchmark suite",
              benchmark_axis: "guardian_memory_quality",
              operator_summary: "Guardian memory quality stays operator-visible and CI-gated.",
              remaining_gap: "Broader live-provider and computer-use proof still remains.",
              scenario_count: 8,
              scenario_names: ["memory_contradiction_ranking_behavior"],
            },
            {
              name: "guardian_user_model_restraint",
              label: "Guardian user-model and restraint benchmark",
              description: "Clarification and restraint benchmark",
              benchmark_axis: "guardian_judgment_and_restraint",
              operator_summary: "User modeling now tightens clarification and restraint behavior through explicit receipts.",
              remaining_gap: "Longer-horizon live replay remains.",
              scenario_count: 4,
              scenario_names: ["guardian_clarification_restraint_behavior"],
            },
            {
              name: "memory_continuity_workflows",
              label: "Memory, continuity, and workflows",
              description: "Memory and workflow endurance suite",
              benchmark_axis: "memory_and_workflow_endurance",
              operator_summary: "Guardian memory and workflow continuity retain recoverable state.",
              remaining_gap: "Broader live-provider and production-like replay is still missing.",
              scenario_count: 14,
              scenario_names: ["workflow_operating_layer_behavior"],
            },
            {
              name: "workflow_endurance_and_repair",
              label: "Workflow endurance, anticipatory repair, and backup branches",
              description: "Workflow endurance and anticipatory repair suite",
              benchmark_axis: "workflow_endurance_and_repair",
              operator_summary: "Long-running workflows surface backup branches and pre-action repair choices.",
              remaining_gap: "Broader live workload replay still remains.",
              scenario_count: 4,
              scenario_names: ["workflow_anticipatory_repair_behavior"],
            },
            {
              name: "trust_boundary_and_safety_receipts",
              label: "Trust boundaries and safety receipts",
              description: "Trust-boundary and safety-receipt suite",
              benchmark_axis: "trust_boundary_and_safety_receipts",
              operator_summary: "Trust posture now has one explicit benchmark lane for secret egress, replay drift, delegation boundaries, and operator safety receipts.",
              remaining_gap: "Broader live hostile-environment replay still remains.",
              scenario_count: 7,
              scenario_names: ["secret_ref_egress_boundary_behavior"],
            },
            {
              name: "computer_use_browser_desktop",
              label: "Computer-use, browser, and desktop execution",
              description: "Browser and desktop suite",
              benchmark_axis: "computer_use_execution",
              operator_summary: "Browser and desktop continuity paths stay visible and auditable.",
              remaining_gap: "A fuller real browser-task harness still remains.",
              scenario_count: 6,
              scenario_names: ["browser_runtime_audit"],
            },
            {
              name: "planning_retrieval_reporting",
              label: "Planning, retrieval, and reporting",
              description: "Planning and reporting suite",
              benchmark_axis: "planning_and_reporting",
              operator_summary: "Planning and report publication paths stay explicit and reviewable.",
              remaining_gap: "Broader live integration proof still remains.",
              scenario_count: 4,
              scenario_names: ["source_report_action_workflow_behavior"],
            },
            {
              name: "governed_improvement",
              label: "Governed self-improvement",
              description: "Governed self-improvement suite",
              benchmark_axis: "governed_improvement",
              operator_summary: "Self-improvement stays review-gated and benchmark-scored.",
              remaining_gap: "Broader candidate classes still remain.",
              scenario_count: 3,
              scenario_names: ["governed_self_evolution_behavior"],
            },
          ],
          governed_improvement: {
            target_count: 2,
            target_types: ["prompt_pack", "skill"],
            required_suite_count: 8,
            gate_policy: {
              min_review_ready_score: 0.7,
              min_strong_score: 0.9,
              requires_human_review: true,
              blocks_on_constraint_failure: true,
              required_benchmark_suites: [
                "guardian_memory_quality",
                "guardian_user_model_restraint",
                "memory_continuity_workflows",
                "workflow_endurance_and_repair",
                "trust_boundary_and_safety_receipts",
                "computer_use_browser_desktop",
                "planning_retrieval_reporting",
                "governed_improvement",
              ],
              proof_contract: "deterministic_benchmark_suites_plus_review_receipts",
            },
          },
        }));
      }
      if (url.includes("/api/operator/guardian-state")) {
        return Promise.resolve(mockResponse({
          summary: {
            overall_confidence: "partial",
            observer_confidence: "grounded",
            world_model_confidence: "partial",
            memory_confidence: "grounded",
            current_session_confidence: "grounded",
            recent_sessions_confidence: "partial",
            intent_uncertainty_level: "high",
            intent_resolution: "clarify_first",
            action_posture: "clarify_first",
            current_focus: "Atlas release planning",
            focus_source: "observer_goal_window",
            focus_alignment: "aligned",
            intervention_receptivity: "guarded",
            dominant_thread: "Atlas launch thread",
            user_model_confidence: "grounded",
          },
          explanation: {
            judgment_proof_lines: [
              "Project-target proof: Atlas remains the strongest active project anchor.",
              "Referent proof: the user message contains an unresolved referent.",
            ],
            intent_uncertainty_diagnostics: [],
            judgment_risks: ["Competing project anchors still require conservative judgment."],
            corroboration_sources: ["observer", "memory"],
            preference_inference_diagnostics: [],
            learning_diagnostics: ["Fresh live outcomes are overruling older procedural guidance."],
            restraint_reasons: ["Intent remains weakly grounded, so clarification is safer than taking a confident action."],
            user_model_benchmark_diagnostics: ["User-model benchmark state: confidence=grounded, restraint_posture=clarify_before_personalizing, action_posture=clarify_first."],
            memory_provider_diagnostics: [],
            memory_reconciliation_diagnostics: [],
          },
          user_model: {
            confidence: "grounded",
            restraint_posture: "clarify_before_personalizing",
            continuity_strategy: "prefer_existing_thread",
            clarification_watchpoints: ["Clarify interaction style when live and procedural preference evidence disagree."],
            restraint_reasons: ["Preference evidence is split, so Seraph should explain uncertainty first."],
            evidence_store: ["Prefers concise updates during Atlas launch work."],
            facets: [
              {
                key: "communication_style",
                label: "Communication preference",
                value: "brief literal",
                confidence: "grounded",
                evidence_sources: ["preference_memory", "live_learning"],
                evidence_lines: ["Prefers concise updates during Atlas launch work."],
              },
            ],
          },
          operator_guidance: {
            active_projects: ["Atlas"],
            active_commitments: ["Ship Atlas release notes"],
            active_blockers: ["Pending release approval"],
            next_up: ["Clarify whether the user meant Atlas or Hermes"],
            learning_guidance: "Prefer clarification before interrupting.",
            recent_execution_summary: "- Atlas deploy failed recently",
          },
          observer: {
            user_state: "focused",
            interruption_mode: "minimal",
            active_window: "VS Code",
            active_project: "Atlas",
            active_goals_summary: "Ship Atlas safely",
            screen_context: "Reviewing Atlas release notes",
            data_quality: "good",
            is_working_hours: true,
          },
        }));
      }
      if (url.includes("/api/operator/workflow-orchestration")) {
        return Promise.resolve(mockResponse({ summary: { tracked_sessions: 0, workflow_count: 0, active_workflows: 0, blocked_workflows: 0, awaiting_approval_workflows: 0, recoverable_workflows: 0 }, sessions: [], workflows: [] }));
      }
      if (url.includes("/api/operator/background-sessions")) {
        return Promise.resolve(mockResponse({ summary: { tracked_sessions: 0, background_process_count: 0, running_background_process_count: 0, sessions_with_branch_handoff: 0, sessions_with_active_workflows: 0 }, sessions: [] }));
      }
      if (url.includes("/api/operator/engineering-memory")) {
        return Promise.resolve(mockResponse({ summary: { query: null, tracked_bundles: 0, repository_bundle_count: 0, pull_request_bundle_count: 0, work_item_bundle_count: 0, search_match_count: 0 }, bundles: [] }));
      }
      if (url.includes("/api/operator/continuity-graph")) {
        return Promise.resolve(mockResponse({ summary: { continuity_health: null, primary_surface: null, recommended_focus: null, tracked_sessions: 0, workflow_count: 0, approval_count: 0, notification_count: 0, queued_insight_count: 0, intervention_count: 0, artifact_count: 0, edge_count: 0 }, sessions: [], nodes: [], edges: [] }));
      }
      if (url.includes("/api/workflows/runs")) return Promise.resolve(mockResponse({ runs: [] }));
      if (url.includes("/api/settings/tool-policy-mode")) return Promise.resolve(mockResponse({ mode: "balanced" }));
      if (url.includes("/api/settings/mcp-policy-mode")) return Promise.resolve(mockResponse({ mode: "approval" }));
      if (url.includes("/api/settings/approval-mode")) return Promise.resolve(mockResponse({ mode: "high_risk" }));
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={vi.fn()} />);

    const guardianTitle = await screen.findByText("Guardian state", { selector: ".cockpit-window-title" });
    const guardianWindow = guardianTitle.closest(".cockpit-window") as HTMLElement;
    const operatorTitle = await screen.findByText("Operator terminal", { selector: ".cockpit-window-title" });
    const operatorWindow = operatorTitle.closest(".cockpit-window") as HTMLElement;

    expect(within(guardianWindow).getByText("overall confidence")).toBeInTheDocument();
    expect(within(guardianWindow).getAllByText("clarify first").length).toBeGreaterThan(0);
    await within(guardianWindow).findByText("Atlas release planning");
    await within(guardianWindow).findByText("Prefer clarification before interrupting.");
    expect(within(guardianWindow).getAllByText("partial").length).toBeGreaterThan(0);
    expect(within(guardianWindow).getByText(/Project-target proof:/)).toBeInTheDocument();
    expect(within(guardianWindow).getByText(/Competing project anchors still require conservative judgment\./)).toBeInTheDocument();
    expect(within(guardianWindow).getByText(/clarify before personalizing/)).toBeInTheDocument();
    expect(
      within(guardianWindow).getAllByText(/Prefers concise updates during Atlas launch work\./).length,
    ).toBeGreaterThanOrEqual(2);
    expect(within(guardianWindow).getByText(/Communication preference · brief literal · grounded · Prefers concise updates during Atlas launch work\./)).toBeInTheDocument();
    await within(operatorWindow).findByText("benchmark proof");
    await within(operatorWindow).findByText(/8 suites · 45 scenarios · deterministic proof backed · 2 evolution targets/);
    expect(within(operatorWindow).getAllByText(/Guardian memory benchmark/).length).toBeGreaterThan(0);
    expect(within(operatorWindow).getAllByText(/Guardian user-model benchmark/).length).toBeGreaterThan(0);
    expect(within(operatorWindow).getAllByText(/Workflow endurance benchmark/).length).toBeGreaterThan(0);
    expect(within(operatorWindow).getAllByText(/Trust-boundary benchmark/).length).toBeGreaterThan(0);
    expect(within(operatorWindow).getByText(/ci gated operator visible · 2 active failures · 5 dimensions/)).toBeInTheDocument();
    expect(within(operatorWindow).getByText(/contradiction reconciled · Atlas release date corrected after contradictory note\./)).toBeInTheDocument();
    expect(within(operatorWindow).getByText(/required on high ambiguity · clarify or wait before unverified personalization · guardian world model/)).toBeInTheDocument();
    expect(within(operatorWindow).getByText(/checkpoint and pre repair visible · recovery paths and output history retained · backup branch operator selectable/)).toBeInTheDocument();
    expect(within(operatorWindow).getByText(/field scoped egress allowlist required · vault and background partitioned · boundary drift blocks replay · benchmark and runtime visible/)).toBeInTheDocument();
    expect(within(operatorWindow).getByText(/field scoped secret refs plus required credential egress allowlist · trust boundary drift blocks replay and resume · 5 receipt surfaces/)).toBeInTheDocument();
    expect(within(operatorWindow).getByText(/benchmark regression · secret ref egress regression · deterministic eval failure/)).toBeInTheDocument();
    expect(within(operatorWindow).getByText(/Memory, continuity, and workflows/)).toBeInTheDocument();
    expect(within(operatorWindow).getByText(/Workflow endurance, anticipatory repair, and backup branches/)).toBeInTheDocument();
    expect(within(operatorWindow).getByText(/Trust boundaries and safety receipts/)).toBeInTheDocument();
    expect(within(operatorWindow).getByText(/Planning, retrieval, and reporting/)).toBeInTheDocument();
    expect(within(operatorWindow).getByText(/Governed self-improvement/)).toBeInTheDocument();
    expect(within(operatorWindow).getByText(/review gate >= 0.7 · strong >= 0.9/)).toBeInTheDocument();
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
    expect(within(workflowRow as HTMLElement).getAllByText(/checkpoint review_checkpoint/i).length).toBeGreaterThan(0);
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
              artifact_paths: ["notes/root-review.md"],
              continued_error_steps: [],
              risk_level: "low",
              thread_id: "session-1",
              thread_label: "Session 1",
              run_identity: "resume-root-run",
              root_run_identity: "resume-root-run",
              branch_kind: "replay_from_start",
              branch_depth: 0,
              checkpoint_context_available: true,
              checkpoint_candidates: [
                {
                  step_id: "review_checkpoint",
                  resume_draft:
                    'Run workflow "resume-review" with file_path="notes/review.md", _seraph_resume_from_step="review_checkpoint".',
                  kind: "branch_from_checkpoint",
                },
              ],
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
              step_records: [
                {
                  id: "review_checkpoint",
                  index: 0,
                  tool: "read_file",
                  status: "failed",
                  argument_keys: ["file_path"],
                  artifact_paths: ["notes/branch-review.md"],
                  error_summary: "review checkpoint needs continuation",
                  recovery_actions: [
                    {
                      type: "set_tool_policy",
                      label: "Allow read_file",
                      mode: "full",
                    },
                  ],
                  is_recoverable: true,
                },
              ],
              artifact_paths: ["notes/branch-review.md"],
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
              checkpoint_candidates: [
                {
                  step_id: "review_checkpoint",
                  resume_draft:
                    'Run workflow "resume-review" with file_path="notes/review.md", _seraph_resume_from_step="review_checkpoint".',
                  kind: "retry_failed_step",
                },
              ],
              replay_allowed: true,
              thread_continue_message: "Continue child branch from the review checkpoint.",
              retry_from_step_draft:
                'Run workflow "resume-review" with file_path="notes/review.md", _seraph_resume_from_step="review_checkpoint".',
              timeline: [
                { kind: "workflow_started", at: "2026-03-20T09:06:00Z", summary: "Branch workflow started" },
                { kind: "workflow_degraded", at: "2026-03-20T09:08:00Z", summary: "branch review needs continuation" },
              ],
            },
            {
              id: "run-peer",
              tool_name: "workflow_resume_review",
              workflow_name: "resume-review",
              session_id: "session-1",
              status: "succeeded",
              started_at: "2026-03-20T09:07:00Z",
              updated_at: "2026-03-20T09:07:30Z",
              summary: "peer review branch completed",
              step_tools: ["read_file"],
              artifact_paths: ["notes/peer-review.md"],
              continued_error_steps: [],
              risk_level: "low",
              thread_id: "session-1",
              thread_label: "Session 1",
              run_identity: "resume-peer-run",
              parent_run_identity: "resume-root-run",
              root_run_identity: "resume-root-run",
              branch_kind: "branch_from_checkpoint",
              branch_depth: 1,
              resume_checkpoint_label: "peer_checkpoint",
              checkpoint_context_available: true,
              checkpoint_candidates: [
                {
                  step_id: "peer_checkpoint",
                  resume_draft:
                    'Run workflow "resume-review" with file_path="notes/review.md", _seraph_resume_from_step="peer_checkpoint".',
                  kind: "branch_from_checkpoint",
                },
              ],
              replay_allowed: true,
              thread_continue_message: "Continue peer branch from the peer checkpoint.",
              timeline: [
                { kind: "workflow_started", at: "2026-03-20T09:07:00Z", summary: "Peer branch started" },
                { kind: "workflow_succeeded", at: "2026-03-20T09:07:30Z", summary: "peer review branch completed" },
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
    expect(within(rootRow as HTMLElement).getByText(/2 child branches/i)).toBeInTheDocument();
    expect(within(rootRow as HTMLElement).getByText(/root branch/i)).toBeInTheDocument();
    expect(within(rootRow as HTMLElement).getByText(/continue resume-review/i)).toBeInTheDocument();
    expect(within(rootRow as HTMLElement).getByText(/latest failure branch review needs continuation/i)).toBeInTheDocument();
    expect(within(rootRow as HTMLElement).getByText(/history 3 outputs/i)).toBeInTheDocument();
    expect(within(rootRow as HTMLElement).getByText(/2 checkpoints/i)).toBeInTheDocument();
    expect(within(rootRow as HTMLElement).getByText(/4 recovery paths/i)).toBeInTheDocument();
    expect(within(rootRow as HTMLElement).getByText(/6 lineage events/i)).toBeInTheDocument();

    fireEvent.click(rootSummary);

    const inspector = document.querySelector(".cockpit-inspector") as HTMLElement;
    expect(within(inspector).getByRole("button", { name: "Open Latest Branch" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Continue Latest Branch" })).toBeInTheDocument();
    expect(within(inspector).getAllByText("child branch")).toHaveLength(2);
    expect(within(inspector).getByText("branch origin")).toBeInTheDocument();
    expect(within(inspector).getByText("best continuation")).toBeInTheDocument();
    expect(within(inspector).getByText("failure lineage")).toBeInTheDocument();
    expect(within(inspector).getAllByText("family output").length).toBeGreaterThan(0);
    expect(within(inspector).getByRole("button", { name: "Open best continuation for resume-review" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Continue best continuation for resume-review" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Retry review_checkpoint from best continuation resume-review" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Retry step for best continuation resume-review" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Repair step review_checkpoint for best continuation resume-review" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Draft next step from workflow family for resume-review" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Use family output notes/branch-review.md from resume-c" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Continue workflow for family output notes/branch-review.md from resume-c" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Retry review_checkpoint from family output notes/branch-review.md resume-review" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Repair step review_checkpoint for family output notes/branch-review.md resume-review" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Compare child branch output notes/branch-review.md" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Compare family output notes/branch-review.md from resume-c" })).toBeInTheDocument();
    expect(within(inspector).getByText("output history")).toBeInTheDocument();
    expect(within(inspector).getByText("checkpoint history")).toBeInTheDocument();
    expect(within(inspector).getByText("lineage event")).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Use current run for output history notes/root-review.md" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Use best continuation for output history notes/branch-review.md" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Branch review_checkpoint from current run for checkpoint history review_checkpoint" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Use output from current run lineage event workflow_succeeded" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Use failure from best continuation lineage event workflow_degraded" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Draft retry from best continuation lineage event workflow_degraded" })).toBeInTheDocument();
    expect(within(inspector).queryByRole("button", { name: "Use failure from current run lineage event workflow_succeeded" })).not.toBeInTheDocument();
    expect(within(inspector).getAllByText(/recovery ready/i).length).toBeGreaterThan(0);

    fireEvent.click(within(inspector).getByRole("button", { name: "Retry review_checkpoint from best continuation resume-review" }));
    await waitFor(() =>
      expect(
        screen.getByDisplayValue(
          'Run workflow "resume-review" with file_path="notes/review.md", _seraph_resume_from_step="review_checkpoint".',
        ),
      ).toBeInTheDocument(),
    );
    fireEvent.click(within(inspector).getByRole("button", { name: "Retry step for best continuation resume-review" }));
    await waitFor(() =>
      expect(
        screen.getByDisplayValue(
          'Run workflow "resume-review" with file_path="notes/review.md", _seraph_resume_from_step="review_checkpoint".',
        ),
      ).toBeInTheDocument(),
    );
    fireEvent.click(within(inspector).getByRole("button", { name: "Use output from current run lineage event workflow_succeeded" }));
    await waitFor(() =>
      expect(screen.getByDisplayValue('Use the workspace file "notes/root-review.md" as context for the next action.')).toBeInTheDocument(),
    );
    fireEvent.click(within(inspector).getByRole("button", { name: "Use failure from best continuation lineage event workflow_degraded" }));
    await waitFor(() =>
      expect(screen.getByDisplayValue(/Review workflow "resume-review" step "review_checkpoint" \(read_file\)\./)).toBeInTheDocument(),
    );
    fireEvent.click(within(inspector).getByRole("button", { name: "Draft retry from best continuation lineage event workflow_degraded" }));
    await waitFor(() =>
      expect(
        screen.getByDisplayValue(
          'Run workflow "resume-review" with file_path="notes/review.md", _seraph_resume_from_step="review_checkpoint".',
        ),
      ).toBeInTheDocument(),
    );
    fireEvent.click(within(inspector).getByRole("button", { name: "Branch review_checkpoint from current run for checkpoint history review_checkpoint" }));
    await waitFor(() =>
      expect(
        screen.getByDisplayValue(
          'Run workflow "resume-review" with file_path="notes/review.md", _seraph_resume_from_step="review_checkpoint".',
        ),
      ).toBeInTheDocument(),
    );

    fireEvent.click(within(inspector).getByRole("button", { name: "Draft next step from workflow family for resume-review" }));
    await waitFor(() =>
      expect(
        screen.getByDisplayValue(
          'Review workflow family state for "resume-review". Current output: "notes/root-review.md". Best continuation: "branch review needs continuation" with latest output "notes/branch-review.md" Latest family failure: "branch review needs continuation". Related reusable outputs: "notes/branch-review.md", "notes/peer-review.md". Recommend the best next step, whether to continue a branch, compare outputs, or reuse one of the related outputs.',
        ),
      ).toBeInTheDocument(),
    );

    fireEvent.click(within(inspector).getByRole("button", { name: "Compare child branch output notes/branch-review.md" }));
    await waitFor(() =>
      expect(
        screen.getByDisplayValue(
          'Compare the workspace files "notes/root-review.md" and "notes/branch-review.md". Summarize the key differences, what changed between these workflow outputs, and whether the related branch improved the result.',
        ),
      ).toBeInTheDocument(),
    );

    fireEvent.click(within(inspector).getByRole("button", { name: "Continue workflow for family output notes/branch-review.md from resume-c" }));
    await waitFor(() =>
      expect(screen.getByDisplayValue("Continue child branch from the review checkpoint.")).toBeInTheDocument(),
    );

    fireEvent.click(within(inspector).getByRole("button", { name: "Continue Latest Branch" }));
    await waitFor(() =>
      expect(screen.getByDisplayValue("Continue child branch from the review checkpoint.")).toBeInTheDocument(),
    );

    fireEvent.click(within(inspector).getByRole("button", { name: "Open Latest Branch" }));
    await waitFor(() =>
      expect((inspector.querySelector(".cockpit-inspector-body") as HTMLElement).textContent).toContain("branch review needs continuation"),
    );
    expect(within(inspector).getAllByRole("button", { name: "Open Parent" }).length).toBeGreaterThan(0);
    expect(within(inspector).getByText("parent run")).toBeInTheDocument();
    expect(within(inspector).getByText("peer branch")).toBeInTheDocument();
    expect(within(inspector).getAllByText(/older than current/i)).toHaveLength(2);
    expect(within(inspector).getByRole("button", { name: "Compare ancestor output notes/root-review.md" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Compare peer branch output notes/peer-review.md" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Compare family output notes/root-review.md from resume-r" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Use ancestor output notes/root-review.md" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Continue peer branch resume-review" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Continue failure lineage branch resume-review" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Use failure context from resume-review failure lineage" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Branch review_checkpoint from parent run resume-review" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Branch peer_checkpoint from peer branch resume-review" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Retry review_checkpoint from failure lineage branch resume-review" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Retry step for failure lineage branch resume-review" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Repair step review_checkpoint for failure lineage branch resume-review" })).toBeInTheDocument();
    fireEvent.click(within(inspector).getByRole("button", { name: "Use ancestor output notes/root-review.md" }));
    await waitFor(() =>
      expect(screen.getByDisplayValue('Use the workspace file "notes/root-review.md" as context for the next action.')).toBeInTheDocument(),
    );
    fireEvent.click(within(inspector).getByRole("button", { name: "Branch review_checkpoint from parent run resume-review" }));
    await waitFor(() =>
      expect(
        screen.getByDisplayValue(
          'Run workflow "resume-review" with file_path="notes/review.md", _seraph_resume_from_step="review_checkpoint".',
        ),
      ).toBeInTheDocument(),
    );
    fireEvent.click(within(inspector).getByRole("button", { name: "Continue peer branch resume-review" }));
    await waitFor(() =>
      expect(screen.getByDisplayValue("Continue peer branch from the peer checkpoint.")).toBeInTheDocument(),
    );
    fireEvent.click(within(inspector).getByRole("button", { name: "Branch peer_checkpoint from peer branch resume-review" }));
    await waitFor(() =>
      expect(
        screen.getByDisplayValue(
          'Run workflow "resume-review" with file_path="notes/review.md", _seraph_resume_from_step="peer_checkpoint".',
        ),
      ).toBeInTheDocument(),
    );
    fireEvent.click(within(inspector).getByRole("button", { name: "Use failure context from resume-review failure lineage" }));
    await waitFor(() =>
      expect(screen.getByDisplayValue(/Review workflow "resume-review" step "review_checkpoint" \(read_file\)\./)).toBeInTheDocument(),
    );
    fireEvent.click(within(inspector).getByRole("button", { name: "Retry step for failure lineage branch resume-review" }));
    await waitFor(() =>
      expect(
        screen.getByDisplayValue(
          'Run workflow "resume-review" with file_path="notes/review.md", _seraph_resume_from_step="review_checkpoint".',
        ),
      ).toBeInTheDocument(),
    );
    fireEvent.click(within(inspector).getByRole("button", { name: "Retry review_checkpoint from failure lineage branch resume-review" }));
    await waitFor(() =>
      expect(
        screen.getByDisplayValue(
          'Run workflow "resume-review" with file_path="notes/review.md", _seraph_resume_from_step="review_checkpoint".',
        ),
      ).toBeInTheDocument(),
    );
    fireEvent.click(within(inspector).getByRole("button", { name: "Continue failure lineage branch resume-review" }));
    await waitFor(() =>
      expect(screen.getByDisplayValue("Continue child branch from the review checkpoint.")).toBeInTheDocument(),
    );
    fireEvent.click(within(inspector).getByRole("button", { name: "Use family output notes/root-review.md from resume-r" }));
    await waitFor(() =>
      expect(screen.getByDisplayValue('Use the workspace file "notes/root-review.md" as context for the next action.')).toBeInTheDocument(),
    );
    fireEvent.click(within(inspector).getByRole("button", { name: "Compare peer branch output notes/peer-review.md" }));
    await waitFor(() =>
      expect(
        screen.getByDisplayValue(
          'Compare the workspace files "notes/branch-review.md" and "notes/peer-review.md". Summarize the key differences, what changed between these workflow outputs, and whether the related branch improved the result.',
        ),
      ).toBeInTheDocument(),
    );
    fireEvent.click(within(inspector).getByRole("button", { name: "Use peer branch output notes/peer-review.md" }));
    await waitFor(() =>
      expect(screen.getByDisplayValue('Use the workspace file "notes/peer-review.md" as context for the next action.')).toBeInTheDocument(),
    );
  }, 15000);

  it("compares the specific output-history artifact instead of the source run primary output", async () => {
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
              artifact_paths: ["notes/root-review.md"],
              continued_error_steps: [],
              risk_level: "low",
              thread_id: "session-1",
              thread_label: "Session 1",
              run_identity: "resume-root-run",
              root_run_identity: "resume-root-run",
              replay_allowed: true,
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
              status: "succeeded",
              started_at: "2026-03-20T09:06:00Z",
              updated_at: "2026-03-20T09:08:00Z",
              summary: "branch review completed with multiple outputs",
              step_tools: ["read_file"],
              artifact_paths: ["notes/branch-review.md", "notes/branch-review-alt.md"],
              continued_error_steps: [],
              risk_level: "low",
              thread_id: "session-1",
              thread_label: "Session 1",
              run_identity: "resume-child-run",
              parent_run_identity: "resume-root-run",
              root_run_identity: "resume-root-run",
              branch_kind: "branch_from_checkpoint",
              branch_depth: 1,
              replay_allowed: true,
              timeline: [
                { kind: "workflow_started", at: "2026-03-20T09:06:00Z", summary: "Branch workflow started" },
                { kind: "workflow_succeeded", at: "2026-03-20T09:08:00Z", summary: "branch review completed with multiple outputs" },
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
    fireEvent.click(rootSummary);

    const inspector = document.querySelector(".cockpit-inspector") as HTMLElement;
    expect(within(inspector).getByRole("button", { name: "Compare child branch for output history notes/branch-review-alt.md" })).toBeInTheDocument();

    fireEvent.click(within(inspector).getByRole("button", { name: "Compare child branch for output history notes/branch-review-alt.md" }));
    await waitFor(() =>
      expect(
        screen.getByDisplayValue(
          'Compare the workspace files "notes/root-review.md" and "notes/branch-review-alt.md". Summarize the key differences, what changed between these workflow outputs, and whether the related branch improved the result.',
        ),
      ).toBeInTheDocument(),
    );
  }, 15000);

  it("keeps family next-step planning available without a best continuation", async () => {
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
              artifact_paths: ["notes/root-review.md"],
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
              id: "run-peer",
              tool_name: "workflow_resume_review",
              workflow_name: "resume-review",
              session_id: "session-1",
              status: "succeeded",
              started_at: "2026-03-20T09:07:00Z",
              updated_at: "2026-03-20T09:07:30Z",
              summary: "peer review branch completed",
              step_tools: ["read_file"],
              artifact_paths: ["notes/peer-review.md"],
              continued_error_steps: [],
              risk_level: "low",
              thread_id: "session-1",
              thread_label: "Session 1",
              run_identity: "resume-peer-run",
              parent_run_identity: "resume-root-run",
              root_run_identity: "resume-root-run",
              branch_kind: "branch_from_checkpoint",
              branch_depth: 1,
              resume_checkpoint_label: "peer_checkpoint",
              checkpoint_context_available: true,
              replay_allowed: true,
              timeline: [
                { kind: "workflow_started", at: "2026-03-20T09:07:00Z", summary: "Peer branch started" },
                { kind: "workflow_succeeded", at: "2026-03-20T09:07:30Z", summary: "peer review branch completed" },
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
    fireEvent.click(rootSummary);

    const inspector = document.querySelector(".cockpit-inspector") as HTMLElement;
    expect(within(inspector).queryByText("best continuation")).not.toBeInTheDocument();
    fireEvent.click(within(inspector).getByRole("button", { name: "Draft next step from workflow family for resume-review" }));

    await waitFor(() =>
      expect(
        screen.getByDisplayValue(
          'Review workflow family state for "resume-review". Current output: "notes/root-review.md". Related reusable outputs: "notes/peer-review.md". Recommend the best next step, whether to continue a branch, compare outputs, or reuse one of the related outputs.',
        ),
      ).toBeInTheDocument(),
    );
  });

  it("surfaces artifact lineage and follow-on control across outputs and the inspector", async () => {
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
      if (url.includes("/api/audit/events")) {
        return Promise.resolve(mockResponse([
          {
            id: "audit-root-output",
            session_id: "session-1",
            event_type: "tool_result",
            tool_name: "write_file",
            risk_level: "low",
            policy_mode: "balanced",
            summary: "Saved root review output",
            created_at: "2026-03-20T09:05:00Z",
            details: { arguments: { file_path: "notes/root-review.md" } },
          },
          {
            id: "audit-peer-output",
            session_id: "session-1",
            event_type: "tool_result",
            tool_name: "write_file",
            risk_level: "low",
            policy_mode: "balanced",
            summary: "Saved peer review output",
            created_at: "2026-03-20T09:07:30Z",
            details: { arguments: { file_path: "notes/peer-review.md" } },
          },
          {
            id: "audit-branch-output",
            session_id: "session-1",
            event_type: "tool_result",
            tool_name: "write_file",
            risk_level: "low",
            policy_mode: "balanced",
            summary: "Saved branch review output",
            created_at: "2026-03-20T09:08:00Z",
            details: { arguments: { file_path: "notes/branch-review.md" } },
          },
        ]));
      }
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
            workflows_ready: 2,
            workflows_total: 2,
            starter_packs_ready: 0,
            starter_packs_total: 0,
            mcp_servers_ready: 0,
            mcp_servers_total: 0,
          },
          native_tools: [{ name: "read_file", description: "Read", risk_level: "low", execution_boundaries: ["workspace_read"], availability: "ready" }],
          skills: [],
          workflows: [
            {
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
              output_surface_artifact_types: ["markdown_document"],
            },
            {
              name: "summarize-file",
              tool_name: "workflow_summarize_file",
              description: "Summarize a workspace file",
              inputs: {
                file_path: {
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
              file_path: "defaults/workflows/summarize-file.md",
              policy_modes: ["balanced", "full"],
              execution_boundaries: ["workspace_read"],
              risk_level: "low",
              requires_approval: false,
              approval_behavior: "never",
              is_available: true,
              availability: "ready",
              missing_tools: [],
              missing_skills: [],
            },
          ],
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
              artifact_paths: ["notes/root-review.md"],
              continued_error_steps: [],
              risk_level: "low",
              thread_id: "session-1",
              thread_label: "Session 1",
              run_identity: "resume-root-run",
              root_run_identity: "resume-root-run",
              branch_kind: "replay_from_start",
              branch_depth: 0,
              checkpoint_context_available: true,
              checkpoint_candidates: [
                {
                  step_id: "review_checkpoint",
                  resume_draft:
                    'Run workflow "resume-review" with file_path="notes/review.md", _seraph_resume_from_step="review_checkpoint".',
                  kind: "branch_from_checkpoint",
                },
              ],
              replay_allowed: true,
              replay_draft: 'Run workflow "resume-review" with file_path="notes/review.md".',
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
              step_records: [
                {
                  id: "review_checkpoint",
                  index: 0,
                  tool: "read_file",
                  status: "failed",
                  argument_keys: ["file_path"],
                  artifact_paths: ["notes/branch-review.md"],
                  error_summary: "review checkpoint needs continuation",
                  recovery_actions: [
                    {
                      type: "set_tool_policy",
                      label: "Allow read_file",
                      mode: "full",
                    },
                  ],
                  is_recoverable: true,
                },
              ],
              artifact_paths: ["notes/branch-review.md"],
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
              checkpoint_candidates: [
                {
                  step_id: "review_checkpoint",
                  resume_draft:
                    'Run workflow "resume-review" with file_path="notes/review.md", _seraph_resume_from_step="review_checkpoint".',
                  kind: "retry_failed_step",
                },
              ],
              replay_allowed: true,
              thread_continue_message: "Continue child branch from the review checkpoint.",
              retry_from_step_draft:
                'Run workflow "resume-review" with file_path="notes/review.md", _seraph_resume_from_step="review_checkpoint".',
            },
            {
              id: "run-peer",
              tool_name: "workflow_resume_review",
              workflow_name: "resume-review",
              session_id: "session-1",
              status: "succeeded",
              started_at: "2026-03-20T09:07:00Z",
              updated_at: "2026-03-20T09:07:30Z",
              summary: "peer review branch completed",
              step_tools: ["read_file"],
              artifact_paths: ["notes/peer-review.md"],
              continued_error_steps: [],
              risk_level: "low",
              thread_id: "session-1",
              thread_label: "Session 1",
              run_identity: "resume-peer-run",
              parent_run_identity: "resume-root-run",
              root_run_identity: "resume-root-run",
              branch_kind: "branch_from_checkpoint",
              branch_depth: 1,
              resume_checkpoint_label: "peer_checkpoint",
              checkpoint_context_available: true,
              checkpoint_candidates: [
                {
                  step_id: "peer_checkpoint",
                  resume_draft:
                    'Run workflow "resume-review" with file_path="notes/review.md", _seraph_resume_from_step="peer_checkpoint".',
                  kind: "branch_from_checkpoint",
                },
              ],
              replay_allowed: true,
              thread_continue_message: "Continue peer branch from the peer checkpoint.",
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

    const evidence = await screen.findByLabelText("Evidence shortcuts");
    const draftArtifactButton = await within(evidence).findByRole("button", {
      name: "Draft next step for artifact: notes/branch-review.md",
    });
    expect(draftArtifactButton).toBeInTheDocument();

    fireEvent.click(draftArtifactButton);
    await waitFor(() =>
      expect(screen.getByDisplayValue(/Review next steps for artifact "notes\/branch-review\.md"\./)).toBeInTheDocument(),
    );
    expect(screen.getByDisplayValue(/summarize-file/)).toBeInTheDocument();
    expect(screen.getByDisplayValue(/notes\/peer-review\.md/)).toBeInTheDocument();
    expect(screen.getByDisplayValue(/notes\/root-review\.md/)).toBeInTheDocument();

    fireEvent.click(await within(evidence).findByRole("button", { name: "Inspect artifact: notes/branch-review.md" }));

    const inspector = document.querySelector(".cockpit-inspector") as HTMLElement;
    expect(within(inspector).getByRole("button", { name: "Open Source Run" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Continue Source Run" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Use Source Failure" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Compare Related Output" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Retry review_checkpoint from artifact source notes/branch-review.md resume-review" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Retry step for artifact source notes/branch-review.md resume-review" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Repair step review_checkpoint for artifact source notes/branch-review.md resume-review" })).toBeInTheDocument();
    expect(within(inspector).getByText("source run")).toBeInTheDocument();
    expect(within(inspector).getByText("follow-on")).toBeInTheDocument();
    expect(within(inspector).getAllByText("related output").length).toBeGreaterThan(0);
    expect(within(inspector).getByRole("button", { name: "Run summarize-file from artifact notes/branch-review.md" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Compare related output notes/peer-review.md with artifact notes/branch-review.md" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Continue related output run notes/peer-review.md" })).toBeInTheDocument();

    fireEvent.click(within(inspector).getByRole("button", { name: "Use Source Failure" }));
    await waitFor(() =>
      expect(screen.getByDisplayValue(/Review workflow "resume-review" step "review_checkpoint" \(read_file\)\./)).toBeInTheDocument(),
    );

    fireEvent.click(within(inspector).getByRole("button", { name: "Compare related output notes/peer-review.md with artifact notes/branch-review.md" }));
    await waitFor(() =>
      expect(
        screen.getByDisplayValue(
          'Compare the workspace files "notes/branch-review.md" and "notes/peer-review.md". Summarize the key differences, what changed between these artifact outputs, and which file is the better base for the next step.',
        ),
      ).toBeInTheDocument(),
    );

    fireEvent.click(within(inspector).getByRole("button", { name: "Run summarize-file from artifact notes/branch-review.md" }));
    await waitFor(() =>
      expect(screen.getByDisplayValue(/Run workflow "summarize-file" with file_path="notes\/branch-review\.md"\./)).toBeInTheDocument(),
    );
  }, 15000);

  it("resolves artifact source lineage from the broader workflow window", async () => {
    useChatStore.setState({
      messages: [],
      sessionId: "session-1",
      sessions: [
        { id: "session-1", title: "Atlas thread", created_at: "", updated_at: "", last_message: null, last_message_role: null },
        { id: "session-2", title: "Research thread", created_at: "", updated_at: "", last_message: null, last_message_role: null },
      ],
    });

    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/sessions")) {
        return Promise.resolve(mockResponse([
          { id: "session-1", title: "Atlas thread", created_at: "", updated_at: "", last_message: null, last_message_role: null },
          { id: "session-2", title: "Research thread", created_at: "", updated_at: "", last_message: null, last_message_role: null },
        ]));
      }
      if (url.includes("/api/goals/tree")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/dashboard")) {
        return Promise.resolve(mockResponse({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }));
      }
      if (url.includes("/api/observer/state")) return Promise.resolve(mockResponse({}));
      if (url.includes("/api/observer/continuity")) {
        return Promise.resolve(mockResponse({
          daemon: { connected: false, pending_notification_count: 0, capture_mode: "balanced" },
          notifications: [],
          queued_insights: [],
          queued_insight_count: 0,
          recent_interventions: [],
          reach: { route_statuses: [] },
          imported_reach: { summary: { family_count: 0, active_family_count: 0, attention_family_count: 0, approval_family_count: 0 }, families: [] },
          source_adapters: { summary: { adapter_count: 0, ready_adapter_count: 0, degraded_adapter_count: 0, authenticated_adapter_count: 0, authenticated_ready_adapter_count: 0, authenticated_degraded_adapter_count: 0 }, adapters: [] },
          summary: { continuity_health: "steady", primary_surface: "browser", actionable_thread_count: 0, ambient_item_count: 0, pending_notification_count: 0, queued_insight_count: 0, recent_intervention_count: 0, degraded_route_count: 0, degraded_source_adapter_count: 0, attention_family_count: 0 },
          threads: [],
          recovery_actions: [],
        }));
      }
      if (url.includes("/api/audit/events")) {
        return Promise.resolve(mockResponse([
          {
            id: "audit-artifact",
            session_id: "session-2",
            tool_name: "write_file",
            event_type: "tool_result",
            risk_level: "low",
            policy_mode: "balanced",
            summary: "Research summary saved",
            created_at: "2026-03-21T10:05:00Z",
            details: { arguments: { file_path: "notes/shared-brief.md" } },
          },
        ]));
      }
      if (url.includes("/api/approvals/pending")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/capabilities/overview")) {
        return Promise.resolve(mockResponse({
          workflows: [],
          skills: [],
          mcp_servers: [],
          native_tools: [],
          starter_packs: [],
          catalog_items: [],
          recommendations: [],
          runbooks: [],
        }));
      }
      if (url.includes("/api/extensions")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/activity/ledger")) return Promise.resolve(mockResponse({ items: [], summary: {} }));
      if (url.includes("/api/workflows/runs?limit=40")) {
        return Promise.resolve(mockResponse({
          runs: [
            {
              id: "run-source",
              tool_name: "workflow_research_brief",
              workflow_name: "research-brief",
              session_id: "session-2",
              status: "succeeded",
              started_at: "2026-03-21T10:00:00Z",
              updated_at: "2026-03-21T10:05:00Z",
              summary: "research brief completed",
              step_tools: ["write_file"],
              step_records: [],
              artifact_paths: ["notes/shared-brief.md"],
              continued_error_steps: [],
              risk_level: "low",
              pending_approval_count: 0,
              pending_approval_ids: [],
              thread_id: "session-2",
              thread_label: "Research thread",
              replay_allowed: true,
              thread_continue_message: "Continue research brief",
              run_identity: "research-run",
              root_run_identity: "research-run",
            },
          ],
        }));
      }
      if (url.includes("/api/workflows/runs")) {
        return Promise.resolve(mockResponse({
          runs: [
            {
              id: "session-local-run",
              tool_name: "workflow_local_note",
              workflow_name: "local-note",
              session_id: "session-1",
              status: "succeeded",
              started_at: "2026-03-21T10:01:00Z",
              updated_at: "2026-03-21T10:02:00Z",
              summary: "local note completed",
              step_tools: ["write_file"],
              step_records: [],
              artifact_paths: ["notes/local.md"],
              continued_error_steps: [],
              risk_level: "low",
              pending_approval_count: 0,
              pending_approval_ids: [],
              thread_id: "session-1",
              thread_label: "Atlas thread",
              replay_allowed: true,
              thread_continue_message: "Continue local note",
              run_identity: "local-run",
              root_run_identity: "local-run",
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

    const evidence = await screen.findByLabelText("Evidence shortcuts");
    expect(await within(evidence).findByText("artifact: notes/shared-brief.md")).toBeInTheDocument();
    expect(within(evidence).getByText(/research-brief · succeeded/)).toBeInTheDocument();

    fireEvent.click(within(evidence).getByRole("button", { name: "Inspect artifact: notes/shared-brief.md" }));

    const inspector = document.querySelector(".cockpit-inspector") as HTMLElement;
    expect(within(inspector).getByRole("button", { name: "Open Source Run" })).toBeInTheDocument();
    expect(within(inspector).getByText("source run")).toBeInTheDocument();
    expect(within(inspector).getByText(/research-brief · research brief completed/)).toBeInTheDocument();
  }, 15000);

  it("fails closed when artifact lineage is ambiguous across recent runs", async () => {
    useChatStore.setState({
      messages: [],
      sessionId: "session-1",
      sessions: [
        { id: "session-1", title: "Atlas thread", created_at: "", updated_at: "", last_message: null, last_message_role: null },
      ],
    });

    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/sessions")) {
        return Promise.resolve(mockResponse([
          { id: "session-1", title: "Atlas thread", created_at: "", updated_at: "", last_message: null, last_message_role: null },
        ]));
      }
      if (url.includes("/api/goals/tree")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/dashboard")) {
        return Promise.resolve(mockResponse({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }));
      }
      if (url.includes("/api/observer/state")) return Promise.resolve(mockResponse({}));
      if (url.includes("/api/observer/continuity")) {
        return Promise.resolve(mockResponse({
          daemon: { connected: false, pending_notification_count: 0, capture_mode: "balanced" },
          notifications: [],
          queued_insights: [],
          queued_insight_count: 0,
          recent_interventions: [],
          reach: { route_statuses: [] },
          imported_reach: { summary: { family_count: 0, active_family_count: 0, attention_family_count: 0, approval_family_count: 0 }, families: [] },
          source_adapters: { summary: { adapter_count: 0, ready_adapter_count: 0, degraded_adapter_count: 0, authenticated_adapter_count: 0, authenticated_ready_adapter_count: 0, authenticated_degraded_adapter_count: 0 }, adapters: [] },
          summary: { continuity_health: "steady", primary_surface: "browser", actionable_thread_count: 0, ambient_item_count: 0, pending_notification_count: 0, queued_insight_count: 0, recent_intervention_count: 0, degraded_route_count: 0, degraded_source_adapter_count: 0, attention_family_count: 0 },
          threads: [],
          recovery_actions: [],
        }));
      }
      if (url.includes("/api/audit/events")) {
        return Promise.resolve(mockResponse([
          {
            id: "audit-ambiguous",
            session_id: "session-1",
            tool_name: "write_file",
            event_type: "tool_result",
            risk_level: "low",
            policy_mode: "balanced",
            summary: "Shared review saved",
            created_at: "2026-03-21T11:05:00Z",
            details: { arguments: { file_path: "notes/shared.md" } },
          },
        ]));
      }
      if (url.includes("/api/approvals/pending")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/capabilities/overview")) {
        return Promise.resolve(mockResponse({
          workflows: [],
          skills: [],
          mcp_servers: [],
          native_tools: [],
          starter_packs: [],
          catalog_items: [],
          recommendations: [],
          runbooks: [],
        }));
      }
      if (url.includes("/api/extensions")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/activity/ledger")) return Promise.resolve(mockResponse({ items: [], summary: {} }));
      if (url.includes("/api/workflows/runs")) {
        return Promise.resolve(mockResponse({
          runs: [
            {
              id: "run-a",
              tool_name: "workflow_write_summary",
              workflow_name: "write-summary",
              session_id: "session-1",
              status: "succeeded",
              started_at: "2026-03-21T11:00:00Z",
              updated_at: "2026-03-21T11:02:00Z",
              summary: "summary branch completed",
              step_tools: ["write_file"],
              step_records: [],
              artifact_paths: ["notes/shared.md"],
              continued_error_steps: [],
              risk_level: "low",
              pending_approval_count: 0,
              pending_approval_ids: [],
              thread_id: "session-1",
              thread_label: "Atlas thread",
              replay_allowed: true,
              run_identity: "run-a",
              root_run_identity: "run-a",
            },
            {
              id: "run-b",
              tool_name: "workflow_update_summary",
              workflow_name: "update-summary",
              session_id: "session-1",
              status: "failed",
              started_at: "2026-03-21T11:03:00Z",
              updated_at: "2026-03-21T11:04:00Z",
              summary: "update branch failed after writing shared output",
              step_tools: ["write_file"],
              step_records: [
                {
                  id: "write_shared",
                  index: 0,
                  tool: "write_file",
                  status: "failed",
                  argument_keys: ["file_path"],
                  artifact_paths: ["notes/shared.md"],
                  error_summary: "write verification failed",
                },
              ],
              artifact_paths: ["notes/shared.md"],
              continued_error_steps: ["write_shared"],
              risk_level: "low",
              pending_approval_count: 0,
              pending_approval_ids: [],
              thread_id: "session-1",
              thread_label: "Atlas thread",
              replay_allowed: true,
              run_identity: "run-b",
              root_run_identity: "run-b",
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

    const evidence = await screen.findByLabelText("Evidence shortcuts");
    expect(await within(evidence).findByText("artifact: notes/shared.md")).toBeInTheDocument();
    expect(within(evidence).getAllByText(/source ambiguous/).length).toBeGreaterThan(0);

    fireEvent.click(within(evidence).getByRole("button", { name: "Draft next step for artifact: notes/shared.md" }));
    await waitFor(() =>
      expect(screen.getByDisplayValue(/Source workflow is ambiguous across 2 recent runs/)).toBeInTheDocument(),
    );

    fireEvent.click(within(evidence).getByRole("button", { name: "Inspect artifact: notes/shared.md" }));

    const inspector = document.querySelector(".cockpit-inspector") as HTMLElement;
    expect(within(inspector).queryByRole("button", { name: "Open Source Run" })).not.toBeInTheDocument();
    expect(within(inspector).queryByRole("button", { name: "Use Source Failure" })).not.toBeInTheDocument();
    expect(within(inspector).getByText("source ambiguous (2 candidates)")).toBeInTheDocument();
    expect(within(inspector).getByText("unresolved · 2 recent runs wrote notes/shared.md")).toBeInTheDocument();
    expect(within(inspector).getByText("candidate source")).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Open candidate source run write-summary for artifact notes/shared.md" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Use candidate failure update-summary for artifact notes/shared.md" })).toBeInTheDocument();
  }, 15000);

  it("surfaces workflow approval, artifact, and trace density inside the inspector", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/sessions")) {
        return Promise.resolve(mockResponse([{ id: "session-1", title: "Atlas thread" }]));
      }
      if (url.includes("/api/goals/tree")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/dashboard")) {
        return Promise.resolve(mockResponse({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }));
      }
      if (url.includes("/api/observer/state")) return Promise.resolve(mockResponse({}));
      if (url.includes("/api/observer/continuity")) {
        return Promise.resolve(mockResponse({
          daemon: { connected: false, pending_notification_count: 0, capture_mode: "balanced" },
          notifications: [],
          queued_insights: [],
          queued_insight_count: 0,
          recent_interventions: [],
        }));
      }
      if (url.includes("/api/audit/events")) {
        return Promise.resolve(mockResponse([
          {
            id: "evt-file",
            session_id: "session-1",
            event_type: "tool_result",
            tool_name: "write_file",
            risk_level: "medium",
            policy_mode: "balanced",
            summary: "write_file saved notes/brief.md",
            details: { arguments: { file_path: "notes/brief.md" } },
            created_at: "2026-03-26T09:01:00Z",
          },
        ]));
      }
      if (url.includes("/api/approvals/approval-run/approve")) {
        return Promise.resolve(mockResponse({ status: "approved" }));
      }
      if (url.includes("/api/approvals/pending")) {
        return Promise.resolve(mockResponse([
          {
            id: "approval-run",
            session_id: "session-1",
            thread_id: "session-1",
            thread_label: "Atlas thread",
            tool_name: "write_file",
            risk_level: "high",
            status: "pending",
            summary: "Approve write_file for Atlas brief",
            created_at: "2026-03-26T09:02:00Z",
            resume_message: "Continue Atlas brief approval",
          },
        ]));
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
            workflows_ready: 2,
            workflows_total: 2,
            starter_packs_ready: 0,
            starter_packs_total: 0,
            mcp_servers_ready: 0,
            mcp_servers_total: 0,
          },
          native_tools: [{ name: "read_file", description: "Read", risk_level: "low", execution_boundaries: ["workspace_read"], availability: "ready" }],
          skills: [],
          workflows: [
            {
              name: "atlas-brief",
              tool_name: "workflow_atlas_brief",
              description: "Create an Atlas brief",
              inputs: { file_path: { type: "string", description: "Workspace file", required: true } },
              requires_tools: ["read_file"],
              requires_skills: [],
              user_invocable: true,
              enabled: true,
              step_count: 1,
              file_path: "defaults/workflows/atlas-brief.md",
              policy_modes: ["balanced", "full"],
              execution_boundaries: ["workspace_read"],
              risk_level: "low",
              requires_approval: false,
              approval_behavior: "never",
              is_available: true,
              availability: "ready",
              missing_tools: [],
              missing_skills: [],
              output_surface_artifact_types: ["document"],
            },
            {
              name: "summarize-file",
              tool_name: "workflow_summarize_file",
              description: "Summarize a workspace file",
              inputs: {
                file_path: {
                  type: "string",
                  description: "Workspace file",
                  required: true,
                  artifact_input: true,
                  artifact_types: ["document"],
                },
              },
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
              approval_behavior: "never",
              is_available: true,
              availability: "ready",
              missing_tools: [],
              missing_skills: [],
            },
          ],
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
            tool_name: "workflow_atlas_brief",
            workflow_name: "atlas-brief",
            session_id: "session-1",
            status: "awaiting_approval",
            started_at: "2026-03-26T09:00:00Z",
            updated_at: "2026-03-26T09:03:00Z",
            summary: "atlas-brief waiting on write_file approval",
            step_tools: ["read_file", "write_file"],
            step_records: [
              {
                id: "write_file",
                index: 1,
                tool: "write_file",
                status: "failed",
                argument_keys: ["file_path"],
                artifact_paths: ["notes/brief.md"],
                error_summary: "write_file blocked by approval",
                recovery_hint: "Approve write_file and continue the workflow.",
                recovery_actions: [{ type: "approval", label: "Approve write_file" }],
                is_recoverable: true,
              },
            ],
            artifact_paths: ["notes/brief.md"],
            continued_error_steps: ["write_file"],
            risk_level: "medium",
            pending_approval_count: 1,
            pending_approval_ids: ["approval-run"],
            thread_id: "session-1",
            thread_label: "Atlas thread",
            replay_allowed: true,
            replay_draft: "Run workflow \"atlas-brief\" with file_path=\"notes/brief.md\".",
            checkpoint_context_available: true,
            checkpoint_candidates: [{
              step_id: "write_file",
              kind: "retry_failed_step",
              status: "degraded",
              resume_supported: true,
              resume_draft:
                "Run workflow \"atlas-brief\" with file_path=\"notes/brief.md\", _seraph_resume_from_step=\"write_file\".",
            }],
            timeline: [
              {
                kind: "workflow_step_failed",
                at: "2026-03-26T09:02:00Z",
                summary: "write_file blocked by approval",
                step_id: "write_file",
                duration_ms: 320,
              },
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

    const workflowSummary = await screen.findByText("atlas-brief waiting on write_file approval");
    fireEvent.click(workflowSummary);

    const inspectorWindow = screen.getByText("Operations inspector").closest(".cockpit-window");
    expect(inspectorWindow).not.toBeNull();
    const approvalButton = await within(inspectorWindow as HTMLElement).findByRole("button", {
      name: "Approve approval context for atlas-brief",
    });
    const approvalStackRow = approvalButton.closest(".cockpit-inspector-stack-row");
    expect(approvalStackRow).not.toBeNull();
    fireEvent.click(approvalButton);
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/approvals/approval-run/approve"),
        expect.objectContaining({ method: "POST" }),
      ),
    );

    const artifactRow = screen.getByText(/artifact output · notes\/brief\.md/i).closest(".cockpit-inspector-stack-row");
    expect(artifactRow).not.toBeNull();
    fireEvent.click(within(artifactRow as HTMLElement).getByRole("button", { name: "Use artifact output notes/brief.md" }));
    await waitFor(() =>
      expect(screen.getByDisplayValue('Use the workspace file "notes/brief.md" as context for the next action.')).toBeInTheDocument(),
    );
    expect(within(artifactRow as HTMLElement).getByRole("button", { name: "Run summarize-file from artifact output notes/brief.md" })).toBeInTheDocument();

    const traceRetryButton = within(inspectorWindow as HTMLElement).getByRole("button", {
      name: "Draft retry from write_file for atlas-brief",
    });
    const traceRow = traceRetryButton.closest(".cockpit-inspector-stack-row");
    expect(traceRow).not.toBeNull();
    fireEvent.click(traceRetryButton);
    await waitFor(() =>
      expect(screen.getByDisplayValue('Run workflow "atlas-brief" with file_path="notes/brief.md", _seraph_resume_from_step="write_file".')).toBeInTheDocument(),
    );

    const stepRow = within(inspectorWindow as HTMLElement).getByText(/write_file · write_file failed · write_file blocked by approval/i).closest(".cockpit-inspector-stack-row");
    expect(stepRow).not.toBeNull();
    expect(within(stepRow as HTMLElement).getByRole("button", { name: "Repair step write_file in atlas-brief" })).toBeInTheDocument();

    fireEvent.click(within(stepRow as HTMLElement).getByRole("button", { name: "Use step context write_file for atlas-brief" }));
    await waitFor(() =>
      expect(screen.getByDisplayValue(/Review workflow "atlas-brief" step "write_file" \(write_file\)\./)).toBeInTheDocument(),
    );

    expect(within(stepRow as HTMLElement).getByRole("button", { name: "Run summarize-file from step output notes/brief.md" })).toBeInTheDocument();
  }, 15000);

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
    expect((await screen.findAllByText("Install Test Installable with high-risk capabilities")).length).toBeGreaterThan(0);
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
          version_line: "2026.4",
          compatibility: {
            seraph: ">=2026.4.10",
            current_version: "2026.4.10",
            compatible: true,
          },
          ok: true,
          results: [],
          diagnostics_summary: {
            issue_count: 0,
            error_issue_count: 0,
            warning_issue_count: 0,
            load_error_count: 0,
            degraded_contribution_count: 0,
            degraded_connector_count: 0,
            state_counts: { ready: 1 },
            highlighted_messages: [],
          },
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
    expect(within(studio).getByText("update workspace · 2026.3.21 -> 2026.4.01 · upgrade")).toBeInTheDocument();
    expect(within(studio).getByText("compatible · Seraph >=2026.4.10 · current 2026.4.10")).toBeInTheDocument();
    expect(within(studio).getByText("ready · no doctor or load errors")).toBeInTheDocument();
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

  it("surfaces extension ecosystem health and catalog governance signals in the operator surface", async () => {
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
          catalog_items: [{
            name: "Research Pack",
            catalog_id: "seraph.research-pack",
            type: "extension_pack",
            description: "Workspace research routines.",
            category: "capability-pack",
            bundled: true,
            installed: true,
            trust: "workspace",
            version: "2026.4.11",
            version_line: "2026.4",
            installed_version: "2026.4.03",
            update_available: true,
            compatibility: {
              seraph: ">=2026.4.01",
              current_version: "2026.4.07",
              compatible: true,
            },
            publisher: { name: "Workspace", homepage: null, support: null },
            diagnostics_summary: {
              issue_count: 0,
              error_issue_count: 0,
              warning_issue_count: 0,
              load_error_count: 0,
              degraded_contribution_count: 0,
              degraded_connector_count: 0,
              state_counts: { ready: 3 },
              highlighted_messages: [],
            },
            recommended_actions: [],
          }],
          recommendations: [],
          runbooks: [],
          marketplace_flows: [
            {
              id: "extension-pack:seraph.research-pack",
              label: "Research Pack",
              kind: "extension_pack",
              availability: "ready",
              summary: "Workspace research routines.",
              detail: "2026.4 · 2026.4.03 -> 2026.4.11 · trust workspace",
              ready_count: 1,
              total_count: 1,
              primary_action: { type: "install_catalog_item", label: "Update pack", name: "seraph.research-pack" },
              recommended_actions: [],
              draft_command: null,
              blocking_reasons: [],
              install_items: [],
              skills: [],
              workflows: [],
              related_runbooks: [],
              catalog_id: "seraph.research-pack",
              installed: true,
              update_available: true,
              version: "2026.4.11",
              version_line: "2026.4",
              installed_version: "2026.4.03",
              contribution_types: ["managed_connectors", "runbooks"],
              trust: "workspace",
              publisher: { name: "Workspace", homepage: null, support: null },
              compatibility: {
                seraph: ">=2026.4.01",
                current_version: "2026.4.07",
                compatible: true,
              },
              diagnostics_summary: {
                issue_count: 0,
                error_issue_count: 0,
                warning_issue_count: 0,
                load_error_count: 0,
                degraded_contribution_count: 0,
                degraded_connector_count: 0,
                state_counts: { ready: 3 },
                highlighted_messages: [],
              },
              status: "ready",
            },
            {
              id: "starter-pack:research-briefing",
              label: "Research Briefing",
              kind: "starter_pack",
              availability: "partial",
              summary: "Compose research skills, workflow, and installables.",
              detail: "0/3 ready · 1 install items missing · 1 runbooks",
              ready_count: 0,
              total_count: 3,
              primary_action: { type: "activate_starter_pack", label: "Activate pack", name: "research-briefing" },
              recommended_actions: [{ type: "install_catalog_item", label: "Install http-request", name: "http-request" }],
              draft_command: "Research the latest release notes",
              blocking_reasons: ["missing install item: http-request"],
              install_items: ["http-request"],
              skills: ["web-briefing"],
              workflows: ["web-brief-to-file"],
              related_runbooks: ["Research Briefing"],
            },
          ],
        }));
      }
      if (url.includes("/api/extensions") && !url.includes("/source")) {
        return Promise.resolve(mockResponse({
          extensions: [{
            id: "seraph.research-pack",
            display_name: "Research Pack",
            version: "2026.4.03",
            version_line: "2026.4",
            kind: "capability-pack",
            trust: "workspace",
            source: "manifest",
            location: "workspace",
            status: "degraded",
            summary: "Workspace research routines.",
            description: "Workspace research routines.",
            compatibility: {
              seraph: ">=2026.4.01",
              current_version: "2026.4.07",
              compatible: true,
            },
            publisher: { name: "Workspace", homepage: null, support: null },
            diagnostics_summary: {
              issue_count: 1,
              error_issue_count: 0,
              warning_issue_count: 1,
              load_error_count: 0,
              degraded_contribution_count: 1,
              degraded_connector_count: 1,
              state_counts: { degraded: 1, ready: 2 },
              highlighted_messages: ["GitHub adapter needs reconnect"],
            },
            issues: [{ severity: "warning", message: "GitHub adapter needs reconnect" }],
            load_errors: [],
            toggle_targets: [],
            toggleable_contribution_types: ["managed_connectors"],
            passive_contribution_types: ["runbooks"],
            enable_supported: true,
            disable_supported: true,
            removable: true,
            enabled_scope: "toggleable_contributions",
            configurable: true,
            metadata_supported: true,
            config_scope: "workspace_metadata",
            enabled: true,
            config: {},
            permission_summary: {
              status: "missing",
              ok: false,
              required: { tools: ["write_file"], execution_boundaries: ["workspace_write"], network: false },
              missing: { tools: ["write_file"], execution_boundaries: ["workspace_write"], network: false },
              risk_level: "high",
            },
            approval_profile: {
              requires_runtime_approval: false,
              requires_lifecycle_approval: true,
              risk_level: "high",
              runtime_behavior: "approval",
              lifecycle_boundaries: ["workspace_write"],
            },
            connector_summary: { total: 1, ready: 0, states: { degraded: 1 } },
            contributions: [],
            studio_files: [{
              key: "seraph.research-pack:manifest",
              role: "manifest",
              reference: "manifest.yaml",
              resolved_path: "/tmp/workspace/extensions/research-pack/manifest.yaml",
              label: "manifest.yaml",
              display_type: "manifest",
              format: "yaml",
              editable: true,
              save_supported: true,
              validation_supported: true,
              loaded: true,
              name: "Research Pack",
            }],
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

    await waitFor(() => expect(screen.getByText("extension health")).toBeInTheDocument());
    await waitFor(() => {
      expect(screen.getByText("1 installed · 1 degraded · 1 updates · 1 approval gated · 1 attention")).toBeInTheDocument();
    });
    await waitFor(() => {
      expect(screen.getByText("1 available · 1 updates · 1 extension packs · 1 compatible")).toBeInTheDocument();
    });
    await waitFor(() => {
      expect(screen.getByText("marketplace flows")).toBeInTheDocument();
      expect(screen.getByText("1/2 ready · 1 updates · 1 attention")).toBeInTheDocument();
    });

    const healthSection = screen.getByText("extension health").closest(".cockpit-operator-section");
    expect(healthSection).not.toBeNull();
    const healthRow = within(healthSection as HTMLElement).getByText("Research Pack").closest(".cockpit-operator-row");
    expect(healthRow).not.toBeNull();
    expect(within(healthRow as HTMLElement).getByRole("button", { name: "update" })).toBeInTheDocument();
    expect(within(healthRow as HTMLElement).getByRole("button", { name: "studio" })).toBeInTheDocument();
    expect(within(healthRow as HTMLElement).getByRole("button", { name: "draft" })).toBeInTheDocument();
    expect(within(healthRow as HTMLElement).getByText(/publisher Workspace/)).toBeInTheDocument();
    expect(within(healthRow as HTMLElement).getByText(/lifecycle approval/)).toBeInTheDocument();

    const flowSection = screen.getByText("marketplace flows").closest(".cockpit-operator-section");
    expect(flowSection).not.toBeNull();
    const flowRow = within(flowSection as HTMLElement).getByText("Research Briefing").closest(".cockpit-operator-row");
    expect(flowRow).not.toBeNull();
    expect(within(flowRow as HTMLElement).getByRole("button", { name: "Activate pack" })).toBeInTheDocument();
    expect(within(flowRow as HTMLElement).getByRole("button", { name: "repair" })).toBeInTheDocument();
    expect(within(flowRow as HTMLElement).getByRole("button", { name: "draft" })).toBeInTheDocument();
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
    expect((await screen.findAllByText("Update Test Installable with high-risk capabilities")).length).toBeGreaterThan(0);
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
    expect((await screen.findAllByText("Enable Test Installable contributions with high-risk capabilities")).length).toBeGreaterThan(0);
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
    const deferredResponses = Array.from({ length: 16 }, () => {
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

    await waitFor(() => expect(cockpitFetchCount).toBe(20));
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

  it("surfaces the team control plane in the operator terminal", async () => {
    let controlPlaneUrl: string | null = null;

    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/sessions")) {
        return Promise.resolve(mockResponse([
          { id: "session-1", title: "Session 1", created_at: "", updated_at: "", last_message: null, last_message_role: null },
        ]));
      }
      if (url.includes("/api/goals/tree")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/goals/dashboard")) {
        return Promise.resolve(mockResponse({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }));
      }
      if (url.includes("/api/runtime/status")) {
        return Promise.resolve(mockResponse({
          version: "2026.4.10",
          build_id: "SERAPH_PRIME_v2026.4.10",
          provider: "openrouter",
          model: "openrouter/openai/gpt-4.1-mini",
          model_label: "gpt-4.1-mini",
          llm_logging_enabled: true,
        }));
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
          summary: {
            continuity_health: "attention",
            primary_surface: "presence",
            recommended_focus: "telegram relay",
            actionable_thread_count: 1,
            ambient_item_count: 0,
            pending_notification_count: 0,
            queued_insight_count: 0,
            recent_intervention_count: 0,
            degraded_route_count: 1,
            degraded_source_adapter_count: 0,
            attention_family_count: 0,
            presence_surface_count: 1,
            attention_presence_surface_count: 1,
          },
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
          marketplace_flows: [],
        }));
      }
      if (url.includes("/api/extensions")) return Promise.resolve(mockResponse({ extensions: [], summary: {} }));
      if (url.includes("/api/activity/ledger")) {
        return Promise.resolve(mockResponse({ items: [], summary: { llm_call_count: 0, llm_cost_usd: 0, failure_count: 0 } }));
      }
      if (url.includes("/api/operator/control-plane")) {
        controlPlaneUrl = url;
        return Promise.resolve(mockResponse({
          governance: {
            workspace_mode: "single_operator_guarded_workspace",
            review_posture: "human review gates privileged mutations, extension lifecycle changes, and governed evolution proposals",
            approval_mode: "high_risk",
            tool_policy_mode: "balanced",
            mcp_policy_mode: "approval",
            delegation_enabled: true,
            roles: [
              {
                id: "human_operator",
                label: "Human operator",
                scope: "workspace_governance",
                summary: "Owns approvals, deployment posture, and final review of privileged mutations.",
                status: "active",
                permissions: ["approve high-risk actions"],
                boundaries: ["human_review", "workspace_write", "external_mcp"],
              },
            ],
          },
          usage: {
            window_hours: 24,
            llm_call_count: 7,
            llm_cost_usd: 0.0234,
            input_tokens: 300,
            output_tokens: 120,
            user_triggered_llm_calls: 3,
            autonomous_llm_calls: 4,
            failure_count: 1,
            pending_approvals: 2,
            active_workflows: 3,
            blocked_workflows: 1,
          },
          runtime_posture: {
            runtime: {
              version: "2026.4.10",
              build_id: "SERAPH_PRIME_v2026.4.10",
              provider: "openrouter",
              model: "openrouter/openai/gpt-4.1-mini",
              model_label: "gpt-4.1-mini",
            },
            extensions: {
              total: 5,
              ready: 4,
              degraded: 1,
              governed: 2,
              issue_count: 3,
              degraded_connector_count: 1,
            },
            continuity: {
              continuity_health: "attention",
              primary_surface: "presence",
              recommended_focus: "telegram relay",
              actionable_thread_count: 1,
              degraded_route_count: 1,
              degraded_source_adapter_count: 0,
              attention_presence_surface_count: 1,
            },
          },
          handoff: {
            pending_approvals: [
              {
                id: "approval:1",
                kind: "approval",
                label: "write_file",
                detail: "Approve guarded write",
                status: "high",
                thread_id: "session-1",
                thread_label: "Session 1",
                continue_message: "Resume after approval.",
              },
            ],
            blocked_workflows: [
              {
                id: "workflow:1",
                kind: "workflow",
                label: "repo-review",
                detail: "Workflow is blocked by approval context drift",
                status: "blocked",
                thread_id: "session-1",
                thread_label: "Session 1",
                continue_message: "Start a fresh guarded repo review.",
              },
            ],
            follow_ups: [
              {
                id: "follow:1",
                kind: "presence_repair",
                label: "Review Telegram relay",
                detail: "Connector requires config.",
                status: "requires_config",
                continue_message: "Plan the Telegram repair.",
              },
            ],
            review_receipts: [
              {
                id: "review:1",
                title: "write_file",
                summary: "Approval requested for write_file",
                status: "approval_requested",
                created_at: "2026-04-08T10:00:00Z",
                thread_id: "session-1",
                thread_label: "Session 1",
              },
            ],
          },
        }));
      }
      if (url.includes("/api/workflows/runs")) return Promise.resolve(mockResponse([]));
      if (url.includes("/api/settings/tool-policy-mode")) return Promise.resolve(mockResponse({ mode: "balanced" }));
      if (url.includes("/api/settings/mcp-policy-mode")) return Promise.resolve(mockResponse({ mode: "approval" }));
      if (url.includes("/api/settings/approval-mode")) return Promise.resolve(mockResponse({ mode: "high_risk" }));
      return Promise.resolve(mockResponse({}));
    });

    render(<CockpitView onSend={vi.fn()} />);

    const controlPlane = await screen.findByRole("region", { name: /team control plane/i });
    await waitFor(() => expect(controlPlane).toHaveTextContent(/single operator guarded workspace/i));
    expect(screen.getByText(/7 llm/i)).toBeInTheDocument();
    expect(screen.getByText(/1 blocked workflows/i)).toBeInTheDocument();
    expect(screen.getByText(/4\/5 extensions ready/i)).toBeInTheDocument();
    expect(screen.getByText(/2 governed/i)).toBeInTheDocument();
    expect(screen.getByText("Human operator")).toBeInTheDocument();
    expect(screen.getByText("Review Telegram relay")).toBeInTheDocument();
    expect(screen.getByText("Approval requested for write_file")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "continue" }).length).toBeGreaterThan(0);
    expect(controlPlaneUrl).not.toContain("session_id=");
  });
});
