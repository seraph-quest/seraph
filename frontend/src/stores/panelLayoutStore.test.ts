import { afterEach, describe, expect, it, vi } from "vitest";

const { localStorageMock } = vi.hoisted(() => {
  let store: Record<string, string> = {};
  const mock = {
    getItem: vi.fn((key: string) => store[key] ?? null),
    setItem: vi.fn((key: string, value: string) => {
      store[key] = value;
    }),
    removeItem: vi.fn((key: string) => {
      delete store[key];
    }),
    clear: vi.fn(() => {
      store = {};
    }),
  };
  Object.defineProperty(globalThis, "localStorage", { value: mock, configurable: true });
  return { localStorageMock: mock };
});

import { getPackedCockpitPanels, usePanelLayoutStore } from "./panelLayoutStore";
import { useCockpitLayoutStore } from "./cockpitLayoutStore";
import { getDefaultPaneVisibility } from "../components/cockpit/layouts";

describe("panelLayoutStore packed cockpit layouts", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    localStorageMock.clear();
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
        ...getPackedCockpitPanels("default", getDefaultPaneVisibility("default")),
      },
    });
  });

  it("packs the default layout across the workspace with no bottom gaps per column", () => {
    vi.stubGlobal("window", { innerWidth: 1600, innerHeight: 980 });

    const panels = getPackedCockpitPanels("default", getDefaultPaneVisibility("default"));
    const columns = new Map<number, Array<{ y: number; height: number }>>();

    for (const panel of Object.values(panels)) {
      const items = columns.get(panel.x) ?? [];
      items.push({ y: panel.y, height: panel.height });
      columns.set(panel.x, items);
    }

    const bottoms = [...columns.values()].map((items) => {
      const sorted = items.sort((left, right) => left.y - right.y);
      for (let index = 1; index < sorted.length; index += 1) {
        const previous = sorted[index - 1];
        const current = sorted[index];
        expect(current.y - (previous.y + previous.height)).toBe(16);
      }
      const last = sorted[sorted.length - 1];
      return last ? last.y + last.height : 0;
    });

    expect(Math.max(...bottoms) - Math.min(...bottoms)).toBeLessThanOrEqual(16);
  });

  it("focus layout keeps a denser three-column arrangement without the hidden inspector", () => {
    vi.stubGlobal("window", { innerWidth: 1600, innerHeight: 980 });

    const panels = getPackedCockpitPanels("focus", {
      ...getDefaultPaneVisibility("focus"),
      inspector_pane: false,
    });
    const xs = new Set(Object.values(panels).map((panel) => panel.x));

    expect(xs.size).toBe(3);
    expect(panels.inspector_pane).toBeUndefined();
    expect(panels.operator_timeline_pane).toBeDefined();
    expect(panels.response_pane).toBeDefined();
    expect(panels.conversation_pane).toBeDefined();
  });

  it("gives the default layout more width to the main guardian surfaces than to inventory panes", () => {
    vi.stubGlobal("window", { innerWidth: 1600, innerHeight: 980 });

    const panels = getPackedCockpitPanels("default", getDefaultPaneVisibility("default"));

    expect(panels.response_pane.width).toBeGreaterThan(panels.sessions_pane.width);
    expect(panels.guardian_state_pane.width).toBeGreaterThan(panels.goals_pane.width);
    expect(panels.operator_timeline_pane.width).toBeGreaterThanOrEqual(panels.approvals_pane.width);
    expect(panels.conversation_pane.width).toBeGreaterThan(panels.audit_pane.width);
  });

  it("keeps focus and review layouts functionally distinct", () => {
    vi.stubGlobal("window", { innerWidth: 1600, innerHeight: 980 });

    const focusPanels = getPackedCockpitPanels("focus", getDefaultPaneVisibility("focus"));
    const reviewPanels = getPackedCockpitPanels("review", getDefaultPaneVisibility("review"));

    expect(focusPanels.response_pane.width).toBeGreaterThan(focusPanels.workflows_pane.width);
    expect(reviewPanels.audit_pane.width).toBeGreaterThan(reviewPanels.sessions_pane.width);
    expect(reviewPanels.trace_pane.width).toBeGreaterThan(reviewPanels.sessions_pane.width);
  });

  it("persists manual pane edits inside the active layout snapshot", () => {
    vi.stubGlobal("window", { innerWidth: 1600, innerHeight: 980 });
    useCockpitLayoutStore.setState({
      activeLayoutId: "focus",
      inspectorVisible: true,
      paneVisibility: getDefaultPaneVisibility("focus"),
      savedPaneVisibility: {
        focus: getDefaultPaneVisibility("focus"),
      },
    });
    usePanelLayoutStore.getState().applyCockpitLayout("focus", getDefaultPaneVisibility("focus"));

    usePanelLayoutStore.getState().setRect("response_pane", { x: 320, y: 160 });
    usePanelLayoutStore.getState().applyCockpitLayout("default", getDefaultPaneVisibility("default"));
    usePanelLayoutStore.getState().applyCockpitLayout("focus", getDefaultPaneVisibility("focus"));

    expect(usePanelLayoutStore.getState().panels.response_pane.x).toBe(320);
    expect(usePanelLayoutStore.getState().panels.response_pane.y).toBe(160);
  });

  it("resetCockpitLayout restores the packed canonical layout for the active preset", () => {
    vi.stubGlobal("window", { innerWidth: 1600, innerHeight: 980 });
    usePanelLayoutStore.getState().applyCockpitLayout("review", getDefaultPaneVisibility("review"));
    const packed = getPackedCockpitPanels("review", getDefaultPaneVisibility("review"));

    usePanelLayoutStore.getState().setRect("audit_pane", { x: 64 });
    usePanelLayoutStore.getState().resetCockpitLayout("review", getDefaultPaneVisibility("review"));

    expect(usePanelLayoutStore.getState().panels.audit_pane.x).toBe(packed.audit_pane.x);
  });
});
