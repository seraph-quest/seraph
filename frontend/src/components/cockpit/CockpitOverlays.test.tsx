import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../game/EventBus", () => ({
  EventBus: {
    emit: vi.fn(),
    on: vi.fn(),
    off: vi.fn(),
  },
}));

import { SettingsPanel } from "../SettingsPanel";
import { QuestPanel } from "../quest/QuestPanel";
import { GoalForm } from "../quest/GoalForm";
import { useChatStore } from "../../stores/chatStore";
import { useQuestStore } from "../../stores/questStore";

function mockResponse(data: unknown, ok = true) {
  return {
    ok,
    json: async () => data,
  };
}

describe("cockpit overlays", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("localStorage", {
      getItem: vi.fn(() => null),
      setItem: vi.fn(),
      removeItem: vi.fn(),
    });
    useChatStore.setState({
      settingsPanelOpen: false,
      questPanelOpen: false,
      onboardingCompleted: true,
      debugWalkability: false,
      interfaceMode: "cockpit",
      sessions: [],
      sessionId: null,
      messages: [],
      toolRegistry: [],
    });
    useQuestStore.setState({
      goalTree: [],
      dashboard: { domains: {}, active_count: 0, completed_count: 0, total_count: 0 },
      loading: false,
      refresh: vi.fn().mockResolvedValue(undefined),
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("renders settings in a cockpit modal instead of a legacy frame", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/skills")) return Promise.resolve(mockResponse({ skills: [] }));
      if (url.includes("/api/mcp/servers")) return Promise.resolve(mockResponse({ servers: [] }));
      if (url.includes("/api/catalog")) return Promise.resolve(mockResponse({ items: [] }));
      if (url.includes("/api/settings/")) return Promise.resolve(mockResponse({ mode: "balanced" }));
      if (url.includes("/api/audit/log")) return Promise.resolve(mockResponse({ entries: [] }));
      if (url.includes("/api/workflows")) return Promise.resolve(mockResponse({ workflows: [] }));
      return Promise.resolve(mockResponse({}));
    });

    useChatStore.setState({ settingsPanelOpen: true });
    const { container } = render(<SettingsPanel />);

    expect(await screen.findByText("Settings")).toBeInTheDocument();
    expect(container.querySelector(".cockpit-modal-card")).not.toBeNull();
    expect(container.querySelector(".rpg-frame")).toBeNull();

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalled();
    });
  });

  it("renders goals and goal editor in cockpit modals", async () => {
    useChatStore.setState({ questPanelOpen: true });
    const { container, rerender } = render(<QuestPanel />);

    expect(screen.getByText("Goals")).toBeInTheDocument();
    expect(container.querySelector(".cockpit-modal-card")).not.toBeNull();
    expect(container.querySelector(".quest-overlay")).toBeNull();

    rerender(<GoalForm onClose={vi.fn()} />);
    expect(screen.getByText("New Goal")).toBeInTheDocument();
    expect(container.querySelector(".rpg-frame")).toBeNull();
  });
});
