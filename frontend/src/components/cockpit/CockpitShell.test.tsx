import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CockpitView } from "./CockpitView";
import { QuestPanel } from "../quest/QuestPanel";
import { homeFixture } from "../../lib/homeContinuation.fixture";
import { useChatStore } from "../../stores/chatStore";
import { useCockpitLayoutStore } from "../../stores/cockpitLayoutStore";
import { usePanelLayoutStore } from "../../stores/panelLayoutStore";
import { useQuestStore } from "../../stores/questStore";
import { getDefaultPaneVisibility } from "./layouts";
import type { GoalInfo } from "../../types";

// Shell-level regressions for #1172 (empty canvas, missing chat, stale model
// and version labels, expected configuration painted as a fault).
const context = vi.hoisted(() => ({
  session: {
    authenticated: true,
    principal_id: "operator",
    session_id: "root",
    absolute_expires_at: "2099-01-01T00:00:00Z",
    idle_expires_at: "2099-01-01T00:00:00Z",
  },
}));
vi.mock("../auth/OperatorAuthGate", () => ({ useOptionalOperatorAuth: () => context }));
vi.mock("./WorkBoardPanel", () => ({ WorkBoardPanel: () => <section aria-label="Work board surface">board</section> }));

const BUILD = "SERAPH_PRIME_v2026.11.14";

// /api/runtime/status as captured on develop @ 0850178d: a stale configured
// default at the top level, the real chat route in effective_runtime.
const runtimeStatus = {
  version: "2026.11.14",
  build_id: BUILD,
  provider: "openrouter",
  model: "x-ai/grok-4.1-fast",
  model_label: "grok-4.1-fast",
  effective_runtime: {
    runtime_path: "chat_agent",
    active_profile: "near_tee.text",
    provider: "near_tee",
    provider_label: "near_tee",
    model: "deepseek-ai/DeepSeek-V4.1-Flash-1017",
    model_label: "DeepSeek-V4.1-Flash-1017",
    route_label: "NEAR TEE text slot",
    inference_ready: true,
    inference_readiness: { status: "ready", reasons: [] },
  },
};

const observerState: Record<string, unknown> = {
  data_quality: "degraded",
  data_quality_reason: "unscoped_fallback",
  attention_budget_remaining: 5,
  tool_policy_mode: "full",
  approval_mode: "high_risk",
};

const continuity = {
  daemon: {
    connected: false,
    pending_notification_count: 0,
    daemon_state: "disabled",
    last_error_kind: "configured_off",
    status_reason: "Native desktop presence is disabled by DAEMON_ENABLED=false.",
    recovery_hint: "Set DAEMON_ENABLED=true in .env.dev and start it with ./manage.sh -e dev daemon start when native desktop presence is wanted.",
  },
  notifications: [],
  queued_insights: [],
  queued_insight_count: 0,
  recent_interventions: [],
  reach: { route_statuses: [] },
  summary: {
    continuity_health: "ready",
    primary_surface: "browser",
    actionable_thread_count: 0,
    ambient_item_count: 0,
    pending_notification_count: 0,
    queued_insight_count: 0,
    recent_intervention_count: 0,
    degraded_route_count: 0,
    degraded_source_adapter_count: 0,
    attention_family_count: 0,
    presence_surface_count: 0,
    attention_presence_surface_count: 0,
    paired_presence_surface_count: 0,
    unpaired_presence_surface_count: 0,
    revoked_presence_surface_count: 0,
    blocked_device_surface_count: 0,
  },
  threads: [],
  recovery_actions: [],
};

const goal: GoalInfo = {
  id: "goal-1",
  parent_id: null,
  path: "goal-1",
  level: "monthly",
  title: "Ship the cockpit redesign",
  description: null,
  status: "active",
  domain: "productivity",
  start_date: null,
  due_date: null,
  sort_order: 0,
  children: [],
  revision: 1,
};

const fetchMock = vi.fn();
let currentObserverState: Record<string, unknown> = observerState;

function respond(url: string): unknown {
  if (url.includes("/api/operator/continuation?")) return homeFixture();
  if (url.endsWith("/api/auth/session")) return context.session;
  if (url.endsWith("/api/runtime/status")) return runtimeStatus;
  if (url.endsWith("/api/observer/state")) return currentObserverState;
  if (url.endsWith("/api/observer/continuity")) return continuity;
  if (url.includes("/api/approvals/pending")) return [];
  if (url.includes("/api/settings/tool-policy-mode")) return { mode: "full" };
  if (url.includes("/api/settings/mcp-policy-mode")) return { mode: "full" };
  if (url.includes("/api/settings/approval-mode")) return { mode: "high_risk" };
  if (url.endsWith("/api/goals/tree")) return [goal];
  if (url.endsWith("/api/goals/dashboard")) return { domains: {}, active_count: 1, completed_count: 0, total_count: 1 };
  if (url.endsWith("/api/sessions") || /\/api\/sessions\/[^/]+\/messages/.test(url)) return [];
  return {};
}

function renderShell(onSkipOnboarding?: () => boolean) {
  return render(
    <>
      <CockpitView onSend={onSend} onSkipOnboarding={onSkipOnboarding} />
      <QuestPanel />
    </>,
  );
}

const onSend = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  onSend.mockReset();
  currentObserverState = observerState;
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("WebSocket", class { close() {} send() {} });
  fetchMock.mockImplementation(async (input: RequestInfo | URL) => new Response(JSON.stringify(respond(String(input)))));
  window.localStorage.clear();
  useChatStore.setState({
    messages: [],
    sessionId: "conversation",
    sessions: [],
    connectionStatus: "connected",
    isAgentBusy: false,
    onboardingCompleted: true,
    questPanelOpen: false,
  });
  useQuestStore.setState({ goalTree: [goal], dashboard: { domains: {}, active_count: 1, completed_count: 0, total_count: 1 }, loading: false });
  usePanelLayoutStore.setState(usePanelLayoutStore.getInitialState(), true);
  useCockpitLayoutStore.setState({
    activeSection: "home",
    activeLayoutId: "default",
    inspectorVisible: true,
    chatDockOpen: true,
    paneVisibility: getDefaultPaneVisibility("default"),
    savedPaneVisibility: { default: getDefaultPaneVisibility("default") },
  });
});

afterEach(() => vi.unstubAllGlobals());

describe("cockpit shell (#1172)", () => {
  it("renders Goals in the canvas when the persisted section is Goals and the overlay flag is not set", async () => {
    // The reported state: Review layout, Goals selected, overlay closed after reload.
    useCockpitLayoutStore.setState({
      activeSection: "goals",
      activeLayoutId: "review",
      paneVisibility: getDefaultPaneVisibility("review"),
      savedPaneVisibility: { review: getDefaultPaneVisibility("review") },
    });
    renderShell();
    const section = await screen.findByTestId("cockpit-goals-section");
    expect(within(section).getByText("Ship the cockpit redesign")).toBeInTheDocument();
    expect(useChatStore.getState().questPanelOpen).toBe(false);
    // Inline, not the modal overlay.
    expect(screen.queryByRole("button", { name: "Close priorities" })).not.toBeInTheDocument();
  });

  it("keeps the Work surface when the active layout hides its window pane", async () => {
    useCockpitLayoutStore.setState({
      activeSection: "work",
      activeLayoutId: "focus",
      paneVisibility: getDefaultPaneVisibility("focus"),
      savedPaneVisibility: { focus: getDefaultPaneVisibility("focus") },
    });
    expect(getDefaultPaneVisibility("focus").work_board_pane).toBe(false);
    renderShell();
    expect(await screen.findByRole("region", { name: "Work board surface" })).toBeInTheDocument();
  });

  it("shows the conversation and composer in every section and sends from it", async () => {
    useChatStore.setState({
      messages: [
        { id: "m1", role: "user", content: "What is next?", timestamp: Date.now(), sessionId: "conversation" },
        { id: "m2", role: "agent", content: "Review the open approval first.", timestamp: Date.now(), sessionId: "conversation" },
      ],
    });
    renderShell();
    const dock = await screen.findByTestId("cockpit-conversation-dock");
    expect(within(dock).getByText("Review the open approval first.")).toBeInTheDocument();
    for (const section of ["inbox", "library", "reports", "goals"] as const) {
      await act(async () => { fireEvent.click(screen.getByTestId(`cockpit-section-${section}`)); });
      expect(screen.getByTestId("cockpit-conversation-dock")).toBeInTheDocument();
    }
    const input = screen.getByLabelText("Message Seraph");
    fireEvent.change(input, { target: { value: "Plan my afternoon" } });
    await act(async () => { fireEvent.click(within(screen.getByTestId("cockpit-conversation-dock")).getByRole("button", { name: "Send" })); });
    expect(onSend).toHaveBeenCalledWith("Plan my afternoon");
  });

  it("collapses and restores the conversation dock", async () => {
    renderShell();
    await screen.findByTestId("cockpit-conversation-dock");
    fireEvent.click(screen.getByRole("button", { name: "Collapse conversation" }));
    expect(screen.queryByTestId("cockpit-conversation-dock")).not.toBeInTheDocument();
    expect(useCockpitLayoutStore.getState().chatDockOpen).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Open conversation" }));
    expect(screen.getByTestId("cockpit-conversation-dock")).toBeInTheDocument();
  });

  it("labels the chat model from effective_runtime, replacing a stale retained receipt", async () => {
    window.localStorage.setItem("seraph.cockpit.runtimeReceipt.v1", JSON.stringify({
      build_id: "SERAPH_PRIME_v2026.4.11",
      provider: "local_gemma",
      model: "gemma-4-26b-a4b-it-qat-gguf",
      model_label: "gemma 4 26B A4B IT QAT GGUF",
      effective_runtime: { route_label: "LOCAL GEMMA", model_label: "GEMMA 4 26B A4B IT QAT GGUF" },
    }));
    // Home section: before #1172 the runtime refresh never ran here.
    renderShell();
    const trigger = await screen.findByTestId("cockpit-status-trigger");
    await waitFor(() => expect(trigger).toHaveTextContent("DeepSeek-V4.1-Flash-1017"));
    expect(trigger).not.toHaveTextContent(/gemma|grok/i);
    fireEvent.click(trigger);
    const chat = screen.getByTestId("cockpit-status-item-chat_model");
    expect(chat).toHaveTextContent("NEAR TEE text slot · DeepSeek-V4.1-Flash-1017");
    expect(chat).toHaveTextContent("Ready");
    const version = screen.getByTestId("cockpit-status-item-version");
    expect(version).toHaveTextContent(BUILD);
    expect(version).not.toHaveTextContent("2026.4.11");
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/api/runtime/status"))).toBe(true);
  });

  it("presents expected configuration as no-action-needed, with reason and settings link", async () => {
    renderShell();
    const trigger = await screen.findByTestId("cockpit-status-trigger");
    await waitFor(() => expect(trigger).toHaveTextContent("All systems ready"));
    fireEvent.click(trigger);
    const desktop = await screen.findByTestId("cockpit-status-item-desktop");
    expect(desktop).toHaveTextContent("Off by configuration");
    expect(desktop).toHaveTextContent("No action needed");
    expect(desktop).toHaveTextContent("DAEMON_ENABLED=false");
    expect(within(desktop).getByRole("button", { name: "Open Settings → Native" })).toBeInTheDocument();
    const context = screen.getByTestId("cockpit-status-item-context");
    expect(context).toHaveTextContent("Not refreshed yet for this session");
    expect(context).toHaveTextContent("No action needed");
    expect(context).not.toHaveTextContent(/degraded seam|execution failure/i);
    // The old unknown chips are gone; nothing reads "unknown" without a reason.
    expect(screen.queryByText(/desktop unknown|bundle unknown|budget \?|state pending/i)).not.toBeInTheDocument();
    const activity = screen.getByTestId("cockpit-seraph-activity");
    expect(activity).not.toHaveTextContent("Degraded");
  });

  it("moves rarely used controls into the More menu and drops permanent hint chips", async () => {
    renderShell();
    await screen.findByTestId("cockpit-status-trigger");
    expect(screen.queryByRole("button", { name: "Extension studio" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Start fresh" })).not.toBeInTheDocument();
    expect(screen.queryByText("Shift+1/2/3 switch layouts")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "More actions" }));
    const menu = screen.getByRole("menu", { name: "More actions" });
    for (const name of ["Start fresh", "Priorities overlay", "Capability palette", "Extension studio", "Workspace windows", "Hide conversation"]) {
      expect(within(menu).getByRole("menuitem", { name })).toBeInTheDocument();
    }
    fireEvent.click(within(menu).getByRole("menuitem", { name: "Workspace windows" }));
    const drawer = screen.getByRole("dialog", { name: "Workspace windows" });
    const layout = within(drawer).getByRole("group", { name: "Layout" });
    for (const name of ["Default", "Focus", "Review", "Reset view", "Save view"]) {
      expect(within(layout).getByRole("button", { name })).toBeInTheDocument();
    }
  });

  it("shows onboarding as a slim dismissible banner and keeps Skip in the More menu", async () => {
    useChatStore.setState({ onboardingCompleted: false });
    const skip = vi.fn(() => true);
    renderShell(skip);
    const banner = await screen.findByRole("group", { name: "Onboarding actions" });
    expect(within(screen.getByTestId("cockpit-conversation-dock")).getByRole("group", { name: "Onboarding actions" })).toBe(banner);
    fireEvent.click(within(banner).getByRole("button", { name: "Dismiss onboarding notice" }));
    expect(screen.queryByRole("group", { name: "Onboarding actions" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "More actions" }));
    await act(async () => { fireEvent.click(screen.getByRole("menuitem", { name: "Skip onboarding" })); });
    expect(skip).toHaveBeenCalledTimes(1);
  });

  it("raises the header summary for a real observer source fault", async () => {
    currentObserverState = { ...observerState, data_quality: "degraded", data_quality_reason: "source_error:git" };
    renderShell();
    const trigger = await screen.findByTestId("cockpit-status-trigger");
    await waitFor(() => expect(trigger).toHaveTextContent("1 needs attention"));
    fireEvent.click(trigger);
    const context = screen.getByTestId("cockpit-status-item-context");
    expect(context).toHaveTextContent("Needs attention");
    expect(context).toHaveTextContent("failed: git");
    expect(context).not.toHaveTextContent("No action needed");
    expect(screen.getByTestId("cockpit-seraph-activity")).toHaveTextContent("Context degraded");
  });

  it("never lets an older shell status response overwrite a newer one", async () => {
    let releaseFirst!: () => void;
    let runtimeCalls = 0;
    const older = { ...runtimeStatus, effective_runtime: { ...runtimeStatus.effective_runtime, model_label: "Older-Model" } };
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/runtime/status")) {
        runtimeCalls += 1;
        if (runtimeCalls === 1) {
          await new Promise<void>((resolve) => { releaseFirst = resolve; });
          return new Response(JSON.stringify(older));
        }
      }
      return new Response(JSON.stringify(respond(url)));
    });
    renderShell();
    const trigger = await screen.findByTestId("cockpit-status-trigger");
    fireEvent.click(trigger);
    const chat = screen.getByTestId("cockpit-status-item-chat_model");
    await act(async () => { fireEvent.click(within(chat).getByRole("button", { name: "Check again" })); });
    await waitFor(() => expect(screen.getByTestId("cockpit-status-item-chat_model")).toHaveTextContent("DeepSeek-V4.1-Flash-1017"));
    await act(async () => { releaseFirst(); });
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByTestId("cockpit-status-item-chat_model")).toHaveTextContent("DeepSeek-V4.1-Flash-1017");
    expect(screen.getByTestId("cockpit-status-item-chat_model")).not.toHaveTextContent("Older-Model");
  });

  it("shows a full approvals page as N+ rather than an exact count", async () => {
    const approvals = Array.from({ length: 8 }, (_, index) => ({
      id: `approval-${index}`, status: "pending", tool_name: "write_file", summary: "Write a file", risk_level: "high",
      created_at: "2026-11-14T10:00:00Z", expires_at: null,
    }));
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/approvals/pending")) return new Response(JSON.stringify(approvals));
      return new Response(JSON.stringify(respond(url)));
    });
    renderShell();
    expect(await screen.findByRole("button", { name: /8\+ approvals waiting/ })).toBeInTheDocument();
    expect(within(screen.getByTestId("cockpit-attention")).getByText("8+")).toBeInTheDocument();
  });

  it("moves focus into the status dialog and returns it on Escape without reaching global shortcuts", async () => {
    const globalEscape = vi.fn();
    window.addEventListener("keydown", globalEscape);
    try {
      renderShell();
      const trigger = await screen.findByTestId("cockpit-status-trigger");
      expect(trigger).toHaveAttribute("aria-haspopup", "dialog");
      fireEvent.click(trigger);
      const dialog = screen.getByRole("dialog", { name: "System status" });
      expect(dialog).toHaveFocus();
      fireEvent.keyDown(dialog, { key: "Escape" });
      expect(screen.queryByRole("dialog", { name: "System status" })).not.toBeInTheDocument();
      expect(trigger).toHaveFocus();
      expect(globalEscape).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener("keydown", globalEscape);
    }
  });

  it("closes the More menu on Tab and on Escape wherever focus is", async () => {
    renderShell();
    const more = await screen.findByRole("button", { name: "More actions" });
    fireEvent.click(more);
    const first = screen.getAllByRole("menuitem")[0];
    expect(first).toHaveFocus();
    fireEvent.keyDown(first, { key: "Tab" });
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    fireEvent.click(more);
    expect(screen.getByRole("menu")).toBeInTheDocument();
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(more).toHaveFocus();
  });

  it("links an approval request in the conversation to the approvals pane", async () => {
    useChatStore.setState({
      messages: [{ id: "a1", role: "approval", content: "Approve writing report.md?", timestamp: Date.now(), sessionId: "conversation", approvalId: "approval-1" }],
    });
    renderShell();
    const dock = await screen.findByTestId("cockpit-conversation-dock");
    await act(async () => { fireEvent.click(within(dock).getByRole("button", { name: "Review approval" })); });
    expect(await screen.findByText("Pending approvals", { selector: ".cockpit-window-title" })).toBeInTheDocument();
  });
});
