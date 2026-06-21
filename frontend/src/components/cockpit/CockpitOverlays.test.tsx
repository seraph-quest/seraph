import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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

const artifactStoragePayload = {
  screen: {
    preservation_enabled: true,
    archive_dir: "/Users/test/Library/Application Support/Seraph/artifacts/screen-captures",
    archive_dir_source: "default",
    exists: true,
    writable: true,
    creation_error: null,
    stored_artifacts: ["image", "provider_output", "analysis_json"],
    inspection_endpoint: "/api/observer/screen-artifacts",
    inspection_visibility: "localhost_only",
    control_env: {
      enabled: "SERAPH_PRESERVE_SCREEN_CAPTURES",
      archive_dir: "SERAPH_SCREEN_CAPTURE_ARCHIVE_DIR or SCREEN_CAPTURE_ARCHIVE_DIR",
    },
  },
  reports: {
    enabled: true,
    hour: 21,
    analysis_provider: "deterministic-local",
    archive_dir: "/tmp/seraph-dev-data/artifacts/reports",
    archive_dir_source: "default",
    exists: true,
    writable: true,
    creation_error: null,
    stored_artifacts: ["report_text", "report_json"],
    control_env: {
      archive_dir: "REPORT_ARCHIVE_DIR",
      enabled: "END_OF_DAY_REPORT_ENABLED",
      llm: "END_OF_DAY_REPORT_LLM_ENABLED",
    },
  },
  email: {
    enabled: false,
    preview_required: true,
    smtp_configured: false,
    recipient_configured: false,
    allowlist_configured: false,
    control_env: {
      enabled: "EMAIL_REPORTS_ENABLED",
      preview_required: "EMAIL_REPORTS_PREVIEW_REQUIRED",
      smtp_host: "SMTP_HOST",
      recipient: "EMAIL_REPORTS_TO",
      allowlist: "EMAIL_REPORTS_TO_ALLOWLIST",
    },
  },
};

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
      themePreference: "system",
      onboardingCompleted: true,
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
      if (url.includes("/api/settings/artifact-storage")) return Promise.resolve(mockResponse(artifactStoragePayload));
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
    expect(screen.getByRole("group", { name: "Theme preference" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "light" })).toBeInTheDocument();

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalled();
    });
  });

  it("updates the theme preference from settings", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/skills")) return Promise.resolve(mockResponse({ skills: [] }));
      if (url.includes("/api/mcp/servers")) return Promise.resolve(mockResponse({ servers: [] }));
      if (url.includes("/api/catalog")) return Promise.resolve(mockResponse({ items: [] }));
      if (url.includes("/api/settings/artifact-storage")) return Promise.resolve(mockResponse(artifactStoragePayload));
      if (url.includes("/api/settings/")) return Promise.resolve(mockResponse({ mode: "balanced" }));
      if (url.includes("/api/audit/log")) return Promise.resolve(mockResponse({ entries: [] }));
      if (url.includes("/api/workflows")) return Promise.resolve(mockResponse({ workflows: [] }));
      return Promise.resolve(mockResponse({}));
    });

    useChatStore.setState({ settingsPanelOpen: true, themePreference: "system" });
    render(<SettingsPanel />);

    const lightButton = await screen.findByRole("button", { name: "light" });
    fireEvent.click(lightButton);

    expect(useChatStore.getState().themePreference).toBe("light");
  });

  it("renders priorities and priority editor in cockpit modals", async () => {
    useChatStore.setState({ questPanelOpen: true });
    const { container, rerender } = render(<QuestPanel />);

    expect(screen.getByText("Priorities")).toBeInTheDocument();
    expect(container.querySelector(".cockpit-modal-card")).not.toBeNull();
    expect(container.querySelector(".quest-overlay")).toBeNull();

    rerender(<GoalForm onClose={vi.fn()} />);
    expect(screen.getByText("New Priority")).toBeInTheDocument();
    expect(container.querySelector(".rpg-frame")).toBeNull();
  });
});
