import { afterEach, describe, expect, it, vi } from "vitest";

import { getPackedCockpitPanels } from "./panelLayoutStore";

describe("panelLayoutStore packed cockpit layouts", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
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
});
