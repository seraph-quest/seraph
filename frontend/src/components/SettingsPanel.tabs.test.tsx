import { act, fireEvent, render, screen, within } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SettingsPanel } from "./SettingsPanel";
import {
  openSettingsSection,
  resolveSettingsSection,
  settingsAnchorId,
  settingsSectionFrom,
  SETTINGS_MODEL_CARD_ID,
  SETTINGS_TABS,
  type SettingsSectionId,
  type SettingsTabId,
} from "../lib/settingsNavigation";
import { useChatStore } from "../stores/chatStore";

// #1280 PR 2: the 17 old tabs become 6. Every panel is a stub here; the test
// checks where each one lands, not what it renders.
vi.mock("./auth/OperatorAuthGate", () => ({ useOptionalOperatorAuth: () => null }));
const { stub } = vi.hoisted(() => ({ stub: (name: string) => () => <div data-testid={`panel-${name}`} /> }));
vi.mock("./settings/ModelTab", () => ({ ModelTab: stub("model-tab") }));
vi.mock("./settings/ScreenshotFolderPanel", () => ({ ScreenshotFolderPanel: stub("screenshot-folder") }));
vi.mock("./settings/ReportDeliveryPanel", () => ({ ReportDeliveryPanel: stub("reports") }));
// The Diagnostics tab is real (its folds are under test); what each fold holds is a stub.
vi.mock("./settings/CordisRuntimePanel", () => ({ CordisRuntimePanel: stub("runtime-host") }));
vi.mock("./settings/ModelRoutesPanel", () => ({ ModelRoutesPanel: stub("model-routes") }));
vi.mock("./settings/NearTeePanel", () => ({ NearTeePanel: stub("near-slots") }));
vi.mock("./settings/OpenRouterSetupPanel", () => ({ OpenRouterSetupPanel: stub("openrouter") }));
vi.mock("./settings/NearTextPanel", () => ({ NearTextPanel: stub("near-text") }));
vi.mock("./settings/InferenceAccountingPanel", () => ({ InferenceAccountingPanel: stub("cost-ledger") }));
vi.mock("./settings/CapabilityChecksPanel", () => ({ CapabilityChecksPanel: stub("capability-checks") }));
vi.mock("./settings/InterruptionModeToggle", () => ({ InterruptionModeToggle: stub("interruption") }));
vi.mock("./settings/ToolPolicyModeToggle", () => ({ ToolPolicyModeToggle: stub("tool-policy") }));
vi.mock("./settings/McpPolicyModeToggle", () => ({ McpPolicyModeToggle: stub("mcp-policy") }));
vi.mock("./settings/ApprovalModeToggle", () => ({ ApprovalModeToggle: stub("approval") }));
vi.mock("./settings/TelegramPairingPanel", () => ({ TelegramPairingPanel: stub("telegram") }));
vi.mock("./settings/CalendarConnectionPanel", () => ({ CalendarConnectionPanel: stub("calendar") }));
vi.mock("./settings/CalendarRescheduleProfiles", () => ({ CalendarRescheduleProfiles: stub("calendar-reschedule") }));
vi.mock("./settings/MailConnectionPanel", () => ({ MailConnectionPanel: stub("mail") }));
vi.mock("./settings/GitHubConnectionPanel", () => ({ GitHubConnectionPanel: stub("github") }));
vi.mock("./settings/GitHubActivityPanel", () => ({ GitHubActivityPanel: stub("github-activity") }));
vi.mock("./settings/ForgejoTitlePanel", () => ({ ForgejoTitlePanel: stub("forgejo") }));
vi.mock("./settings/MoltbookConnectionPanel", () => ({ MoltbookConnectionPanel: stub("moltbook") }));
vi.mock("./settings/DaemonStatus", () => ({ DaemonStatus: stub("native") }));
vi.mock("./settings/SelectedContextPairing", () => ({ SelectedContextPairing: stub("selected-context") }));
vi.mock("./settings/WorkflowPanel", () => ({ WorkflowPanel: stub("workflows") }));
vi.mock("./settings/SourceWatchPanel", () => ({ SourceWatchPanel: stub("guardian") }));
vi.mock("./settings/RepoSandboxPanel", () => ({ RepoSandboxPanel: stub("repo-sandbox") }));
vi.mock("./settings/BackgroundInferencePanel", () => ({ BackgroundInferencePanel: stub("background-jobs") }));
vi.mock("./settings/AuditLogPanel", () => ({ AuditLogPanel: stub("audit") }));

/**
 * Every id the old 17-tab layout accepted, plus "model" from PR 1 and
 * "background-jobs". The last column names a stub panel, or "text:" for real
 * content the Extensions tab renders from the (empty) API responses.
 */
const OLD_SECTIONS: ReadonlyArray<[SettingsSectionId, SettingsTabId, string | null, string]> = [
  ["model", "model", null, "panel-model-tab"],
  ["artifacts", "model", null, "panel-model-tab"],
  ["general", "general", "general", "panel-interruption"],
  ["policies", "general", "policies", "panel-tool-policy"],
  ["telegram", "accounts", "telegram", "panel-telegram"],
  ["calendar", "accounts", "calendar", "panel-calendar"],
  ["mail", "accounts", "mail", "panel-mail"],
  ["github", "accounts", "github", "panel-github"],
  ["forgejo", "accounts", "forgejo", "panel-forgejo"],
  ["moltbook", "accounts", "moltbook", "panel-moltbook"],
  ["native", "accounts", "native", "panel-native"],
  ["workflows", "automation", "workflows", "panel-workflows"],
  ["guardian", "automation", "guardian", "panel-guardian"],
  ["repo-sandbox", "automation", "repo-sandbox", "panel-repo-sandbox"],
  ["background-jobs", "automation", "background-jobs", "panel-background-jobs"],
  ["skills", "extensions", "skills", "text:No skills loaded"],
  ["discover", "extensions", "discover", "text:No catalog items available"],
  ["mcp", "extensions", "mcp", "text:No servers configured"],
  ["audit", "diagnostics", "audit", "panel-audit"],
];

/** Ids added in #1280 PR 3 for the pieces of the old "Advanced and diagnostics" fold. */
const MOVED_SECTIONS: ReadonlyArray<[SettingsSectionId, SettingsTabId, string, string]> = [
  ["screenshot-folder", "automation", "screenshot-folder", "panel-screenshot-folder"],
  ["reports", "automation", "reports", "panel-reports"],
  ["runtime-host", "diagnostics", "runtime-host", "panel-runtime-host"],
  ["model-routes", "diagnostics", "model-routes", "panel-model-routes"],
  ["near-slots", "diagnostics", "near-slots", "panel-near-slots"],
  ["openrouter", "diagnostics", "openrouter", "panel-openrouter"],
  ["near-text", "diagnostics", "near-text", "panel-near-text"],
  ["cost-ledger", "diagnostics", "cost-ledger", "panel-cost-ledger"],
  ["capability-checks", "diagnostics", "capability-checks", "panel-capability-checks"],
];

const DIAGNOSTICS_FOLDS = ["runtime-host", "model-routes", "near-slots", "openrouter", "near-text", "cost-ledger", "capability-checks", "audit"];

const scrollIntoView = vi.fn();
const original = Element.prototype.scrollIntoView;

beforeEach(() => {
  useChatStore.setState({ settingsPanelOpen: false });
  scrollIntoView.mockReset();
  Element.prototype.scrollIntoView = scrollIntoView;
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) }));
});
afterEach(() => {
  Element.prototype.scrollIntoView = original;
  vi.unstubAllGlobals();
});

function tabLabel(tab: SettingsTabId): string {
  return SETTINGS_TABS.find((entry) => entry.id === tab)!.label;
}

function tabBar(): HTMLElement {
  return screen.getByRole("button", { name: "Model" }).parentElement as HTMLElement;
}

describe("Settings tabs (#1280 PR 2)", () => {
  it("shows six tabs in order and opens on Model", () => {
    useChatStore.setState({ settingsPanelOpen: true });
    render(<SettingsPanel />);
    const tabs = within(tabBar()).getAllByRole("button").map((button) => button.textContent);
    expect(tabs).toEqual(["Model", "General", "Accounts", "Automation", "Extensions", "Diagnostics"]);
    expect(screen.getByRole("button", { name: "Model" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("panel-model-tab")).toBeInTheDocument();
  });

  it("uses Accounts, not Connections, because the cockpit already has a Connections page", () => {
    expect(SETTINGS_TABS.map((tab) => tab.label)).not.toContain("Connections");
  });

  it.each([
    ["General", ["panel-interruption", "panel-tool-policy", "panel-mcp-policy", "panel-approval"]],
    ["Accounts", ["panel-telegram", "panel-calendar", "panel-calendar-reschedule", "panel-mail", "panel-github", "panel-github-activity", "panel-forgejo", "panel-moltbook", "panel-native", "panel-selected-context"]],
    ["Automation", ["panel-workflows", "panel-guardian", "panel-repo-sandbox", "panel-screenshot-folder", "panel-background-jobs", "panel-reports"]],
    ["Extensions", ["settings-section-skills", "settings-section-discover", "settings-section-mcp"]],
    ["Diagnostics", DIAGNOSTICS_FOLDS.map((fold) => settingsAnchorId(fold))],
  ])("%s holds every panel it absorbs", (label, panels) => {
    useChatStore.setState({ settingsPanelOpen: true });
    render(<SettingsPanel />);
    fireEvent.click(screen.getByRole("button", { name: label }));
    for (const panel of panels) {
      expect(document.querySelector(`[data-testid="${panel}"], #${panel}`)).not.toBeNull();
    }
    expect(screen.queryByTestId("panel-model-tab")).not.toBeInTheDocument();
  });

  it("reads artifact storage once for the screenshot folder and reports on Automation", () => {
    useChatStore.setState({ settingsPanelOpen: true });
    render(<SettingsPanel />);
    fireEvent.click(screen.getByRole("button", { name: "Automation" }));
    const urls = vi.mocked(fetch).mock.calls.map(([input]) => String(input));
    expect(urls.filter((url) => url.includes("/api/settings/artifact-storage"))).toHaveLength(1);
  });

  it("opens Diagnostics with every fold closed and nothing inside mounted", () => {
    useChatStore.setState({ settingsPanelOpen: true });
    render(<SettingsPanel />);
    fireEvent.click(screen.getByRole("button", { name: "Diagnostics" }));
    for (const fold of DIAGNOSTICS_FOLDS) {
      const element = document.getElementById(settingsAnchorId(fold))!;
      expect(element.tagName).toBe("DETAILS");
      expect(element).not.toHaveAttribute("open");
      expect(within(element).queryByTestId(`panel-${fold}`)).not.toBeInTheDocument();
    }
    // The audit log fetches only once its fold is opened.
    const audit = document.getElementById(settingsAnchorId("audit"))!;
    fireEvent.click(audit.querySelector("summary")!);
    expect(audit).toHaveAttribute("open");
    expect(within(audit).getByTestId("panel-audit")).toBeInTheDocument();
    fireEvent.click(audit.querySelector("summary")!);
    expect(audit).not.toHaveAttribute("open");
  });

  it.each(MOVED_SECTIONS)("new id %s opens %s and its fold or panel", (section, tab, anchor, marker) => {
    expect(resolveSettingsSection(section)).toEqual({ tab, anchor });
    expect(settingsSectionFrom(section)).toBe(section);
    render(<SettingsPanel />);
    act(() => openSettingsSection(section));
    expect(screen.getByRole("button", { name: tabLabel(tab) })).toHaveAttribute("aria-pressed", "true");
    const target = document.getElementById(settingsAnchorId(anchor))!;
    expect(within(target).getByTestId(marker)).toBeInTheDocument();
    if (tab === "diagnostics") expect(target).toHaveAttribute("open");
    expect(scrollIntoView.mock.contexts[0]).toBe(target);
  });

  it("does not mount background jobs on the Model tab any more", () => {
    useChatStore.setState({ settingsPanelOpen: true });
    render(<SettingsPanel />);
    expect(screen.queryByTestId("panel-background-jobs")).not.toBeInTheDocument();
  });

  it.each(OLD_SECTIONS)("old id %s opens %s and brings its panel into view", async (section, tab, anchor, marker) => {
    expect(resolveSettingsSection(section)).toEqual(anchor ? { tab, anchor } : { tab });
    expect(settingsSectionFrom(section)).toBe(section);
    render(<SettingsPanel />);
    act(() => openSettingsSection(section));
    expect(useChatStore.getState().settingsPanelOpen).toBe(true);
    expect(screen.getByRole("button", { name: tabLabel(tab) })).toHaveAttribute("aria-pressed", "true");
    if (anchor) {
      const target = document.getElementById(settingsAnchorId(anchor));
      expect(target).not.toBeNull();
      if (marker.startsWith("text:")) expect(await within(target!).findByText(marker.slice(5))).toBeInTheDocument();
      else expect(within(target!).getByTestId(marker)).toBeInTheDocument();
      expect(scrollIntoView).toHaveBeenCalledTimes(1);
      expect(scrollIntoView.mock.contexts[0]).toBe(target);
    } else {
      expect(screen.getByTestId(marker)).toBeInTheDocument();
    }
  });

  it("deep-links into an already open panel on another tab", () => {
    useChatStore.setState({ settingsPanelOpen: true });
    render(<SettingsPanel />);
    act(() => openSettingsSection("audit"));
    expect(screen.getByRole("button", { name: "Diagnostics" })).toHaveAttribute("aria-pressed", "true");
    act(() => openSettingsSection("native"));
    expect(screen.getByRole("button", { name: "Accounts" })).toHaveAttribute("aria-pressed", "true");
    expect(scrollIntoView.mock.contexts[scrollIntoView.mock.contexts.length - 1]).toBe(document.getElementById(settingsAnchorId("native")));
  });

  it("does not scroll again when the operator just switches tabs after a deep link", () => {
    render(<SettingsPanel />);
    act(() => openSettingsSection("mail"));
    expect(scrollIntoView).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Model" }));
    fireEvent.click(screen.getByRole("button", { name: "Accounts" }));
    expect(scrollIntoView).toHaveBeenCalledTimes(1);
  });

  it("still scrolls the Model card into view for the \"model\" link", () => {
    const card = document.createElement("div");
    card.id = SETTINGS_MODEL_CARD_ID;
    document.body.appendChild(card);
    try {
      render(<SettingsPanel />);
      act(() => openSettingsSection("model"));
      expect(scrollIntoView.mock.contexts[0]).toBe(card);
    } finally {
      card.remove();
    }
  });

  it.each(SETTINGS_TABS.map((tab) => tab.id))("new tab id %s opens that tab", (tab) => {
    expect(settingsSectionFrom(tab)).toBe(tab);
    render(<SettingsPanel />);
    act(() => openSettingsSection(tab));
    expect(screen.getByRole("button", { name: tabLabel(tab) })).toHaveAttribute("aria-pressed", "true");
  });

  it("reopens on Model after a deep link", () => {
    render(<SettingsPanel />);
    act(() => openSettingsSection("audit"));
    act(() => useChatStore.getState().setSettingsPanelOpen(false));
    act(() => useChatStore.getState().setSettingsPanelOpen(true));
    expect(screen.getByRole("button", { name: "Model" })).toHaveAttribute("aria-pressed", "true");
  });

  it.each(["connections", "settings", "", "__proto__", "toString", 42, null])("ignores unknown section %s", (value) => {
    expect(settingsSectionFrom(value)).toBeUndefined();
    render(<SettingsPanel />);
    act(() => openSettingsSection(value as SettingsSectionId));
    expect(useChatStore.getState().settingsPanelOpen).toBe(false);
  });
  it.each(SETTINGS_TABS.filter((tab) => tab.id !== "extensions").map((tab) => tab.label))(
    "does not fetch skills, catalog or MCP servers on %s",
    (label) => {
      useChatStore.setState({ settingsPanelOpen: true });
      render(<SettingsPanel />);
      fireEvent.click(screen.getByRole("button", { name: label }));
      const urls = vi.mocked(fetch).mock.calls.map(([input]) => String(input));
      expect(urls.filter((url) => /\/api\/(skills|catalog|mcp\/servers)/.test(url))).toEqual([]);
    },
  );

  it("fetches skills, catalog and MCP servers once Extensions opens", async () => {
    useChatStore.setState({ settingsPanelOpen: true });
    render(<SettingsPanel />);
    fireEvent.click(screen.getByRole("button", { name: "Extensions" }));
    expect(await screen.findByText("No servers configured")).toBeInTheDocument();
    const urls = vi.mocked(fetch).mock.calls.map(([input]) => String(input));
    for (const path of ["/api/skills", "/api/catalog", "/api/mcp/servers"]) {
      expect(urls.some((url) => url.includes(path))).toBe(true);
    }
  });

  it("shows Workflows once, not as an added heading over the panel's own", () => {
    useChatStore.setState({ settingsPanelOpen: true });
    render(<SettingsPanel />);
    fireEvent.click(screen.getByRole("button", { name: "Automation" }));
    const group = document.getElementById(settingsAnchorId("workflows"))!;
    expect(group.getAttribute("aria-label")).toBeNull();
    expect(within(group).queryByText("Workflows")).not.toBeInTheDocument();
  });

  describe("anchored deep link", () => {
    let observers: Array<{ callback: ResizeObserverCallback; disconnected: boolean }> = [];
    beforeEach(() => {
      observers = [];
      vi.stubGlobal("ResizeObserver", class {
        entry: { callback: ResizeObserverCallback; disconnected: boolean };
        constructor(callback: ResizeObserverCallback) {
          this.entry = { callback, disconnected: false };
          observers.push(this.entry);
        }
        observe() {}
        unobserve() {}
        disconnect() { this.entry.disconnected = true; }
      });
    });
    const grow = () => act(() => {
      for (const entry of observers) if (!entry.disconnected) entry.callback([], {} as ResizeObserver);
    });

    it("keeps the target in view while content above it grows", () => {
      render(<SettingsPanel />);
      act(() => openSettingsSection("native"));
      const target = document.getElementById(settingsAnchorId("native"));
      expect(scrollIntoView).toHaveBeenCalledTimes(1);
      grow();
      grow();
      expect(scrollIntoView).toHaveBeenCalledTimes(3);
      expect(scrollIntoView.mock.contexts.every((context) => context === target)).toBe(true);
    });

    it.each(["wheel", "touchstart", "keydown", "pointerdown", "focusin"])("lets go after the operator's %s", (name) => {
      render(<SettingsPanel />);
      act(() => openSettingsSection("mcp"));
      const body = document.querySelector(".cockpit-modal-body")!;
      act(() => { body.dispatchEvent(new Event(name, { bubbles: true })); });
      scrollIntoView.mockReset();
      grow();
      expect(scrollIntoView).not.toHaveBeenCalled();
    });

    it("lets go when focus moves into Settings with no key or pointer event (#1298 review)", () => {
      render(<SettingsPanel />);
      act(() => openSettingsSection("native"));
      grow();
      expect(scrollIntoView).toHaveBeenCalledTimes(2);
      // Programmatic focus (assistive tech, a script) fires focusin and nothing else.
      const target = within(document.getElementById(settingsAnchorId("native"))!).getByTestId("panel-native");
      const field = document.createElement("input");
      target.appendChild(field);
      act(() => field.focus());
      scrollIntoView.mockReset();
      grow();
      expect(scrollIntoView).not.toHaveBeenCalled();
    });

    it("lets go after a manual tab switch and on close", () => {
      render(<SettingsPanel />);
      act(() => openSettingsSection("repo-sandbox"));
      fireEvent.click(screen.getByRole("button", { name: "Accounts" }));
      scrollIntoView.mockReset();
      grow();
      expect(scrollIntoView).not.toHaveBeenCalled();
      act(() => openSettingsSection("discover"));
      act(() => useChatStore.getState().setSettingsPanelOpen(false));
      act(() => useChatStore.getState().setSettingsPanelOpen(true));
      scrollIntoView.mockReset();
      grow();
      expect(scrollIntoView).not.toHaveBeenCalled();
    });

    it("stops the tab bar's offset below the sticky bar", () => {
      render(<SettingsPanel />);
      act(() => openSettingsSection("native"));
      const body = document.querySelector<HTMLElement>(".cockpit-modal-body")!;
      expect(body.style.getPropertyValue("--settings-tabbar-offset")).toMatch(/^\d+px$/);
      expect(document.querySelector(".cockpit-settings-tabbar")).toBe(tabBar());
    });
  });
});
