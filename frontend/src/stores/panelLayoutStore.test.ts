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

describe("panelLayoutStore packed cockpit layouts", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    localStorageMock.clear();
    useCockpitLayoutStore.setState({ activeLayoutId: "default", inspectorVisible: true });
    usePanelLayoutStore.setState({
      panels: {
        ...usePanelLayoutStore.getState().panels,
        ...getPackedCockpitPanels("default", true),
      },
    });
  });

  it("packs the default layout across the workspace with no bottom gaps per column", () => {
    vi.stubGlobal("window", { innerWidth: 1600, innerHeight: 980 });

    const panels = getPackedCockpitPanels("default", true);
    const columns = new Map<number, Array<{ y: number; height: number }>>();

    for (const panel of Object.values(panels)) {
      const items = columns.get(panel.x) ?? [];
      items.push({ y: panel.y, height: panel.height });
      columns.set(panel.x, items);
    }

    const bottoms = [...columns.values()].map((items) => {
      const sorted = items.sort((left, right) => left.y - right.y);
      const last = sorted[sorted.length - 1];
      return last ? last.y + last.height : 0;
    });

    expect(new Set(bottoms).size).toBe(1);
  });

  it("focus layout keeps a denser three-column arrangement without the hidden inspector", () => {
    vi.stubGlobal("window", { innerWidth: 1600, innerHeight: 980 });

    const panels = getPackedCockpitPanels("focus", false);
    const xs = new Set(Object.values(panels).map((panel) => panel.x));

    expect(xs.size).toBe(3);
    expect(panels.inspector_pane).toBeUndefined();
    expect(panels.response_pane).toBeDefined();
    expect(panels.conversation_pane).toBeDefined();
  });

  it("gives the default layout more width to the main guardian surfaces than to inventory panes", () => {
    vi.stubGlobal("window", { innerWidth: 1600, innerHeight: 980 });

    const panels = getPackedCockpitPanels("default", true);

    expect(panels.response_pane.width).toBeGreaterThan(panels.sessions_pane.width);
    expect(panels.guardian_state_pane.width).toBeGreaterThan(panels.goals_pane.width);
    expect(panels.conversation_pane.width).toBeGreaterThan(panels.audit_pane.width);
  });

  it("keeps focus and review layouts functionally distinct", () => {
    vi.stubGlobal("window", { innerWidth: 1600, innerHeight: 980 });

    const focusPanels = getPackedCockpitPanels("focus", true);
    const reviewPanels = getPackedCockpitPanels("review", true);

    expect(focusPanels.response_pane.width).toBeGreaterThan(focusPanels.workflows_pane.width);
    expect(reviewPanels.audit_pane.width).toBeGreaterThan(reviewPanels.sessions_pane.width);
    expect(reviewPanels.trace_pane.width).toBeGreaterThan(reviewPanels.sessions_pane.width);
  });

  it("persists manual pane edits inside the active layout snapshot", () => {
    vi.stubGlobal("window", { innerWidth: 1600, innerHeight: 980 });
    useCockpitLayoutStore.setState({ activeLayoutId: "focus", inspectorVisible: true });
    usePanelLayoutStore.getState().applyCockpitLayout("focus", true);

    usePanelLayoutStore.getState().setRect("response_pane", { x: 320, y: 160 });
    usePanelLayoutStore.getState().applyCockpitLayout("default", true);
    usePanelLayoutStore.getState().applyCockpitLayout("focus", true);

    expect(usePanelLayoutStore.getState().panels.response_pane.x).toBe(320);
    expect(usePanelLayoutStore.getState().panels.response_pane.y).toBe(160);
  });

  it("resetCockpitLayout restores the packed canonical layout for the active preset", () => {
    vi.stubGlobal("window", { innerWidth: 1600, innerHeight: 980 });
    usePanelLayoutStore.getState().applyCockpitLayout("review", true);
    const packed = getPackedCockpitPanels("review", true);

    usePanelLayoutStore.getState().setRect("audit_pane", { x: 64 });
    usePanelLayoutStore.getState().resetCockpitLayout("review", true);

    expect(usePanelLayoutStore.getState().panels.audit_pane.x).toBe(packed.audit_pane.x);
  });
});
