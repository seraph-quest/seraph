import { describe, expect, it } from "vitest";

import { COCKPIT_LAYOUTS, getCockpitLayout } from "./layouts";

describe("cockpit layouts", () => {
  it("exposes the persisted default, focus, and review layouts", () => {
    expect(Object.keys(COCKPIT_LAYOUTS)).toEqual(["default", "focus", "review"]);
  });

  it("keeps the full cockpit surface in the default layout", () => {
    const layout = getCockpitLayout("default");

    expect(layout.centerSingleColumn).toBe(false);
    expect(layout.paneVisibility.sessions_pane).toBe(true);
    expect(layout.paneVisibility.operator_timeline_pane).toBe(true);
    expect(layout.paneVisibility.workflows_pane).toBe(true);
    expect(layout.paneVisibility.audit_pane).toBe(false);
    expect(layout.paneVisibility.trace_pane).toBe(false);
    expect(layout.paneVisibility.conversation_pane).toBe(true);
    expect(layout.paneVisibility.outputs_pane).toBe(false);
    expect(layout.paneVisibility.desktop_shell_pane).toBe(false);
    expect(layout.paneVisibility.operator_surface_pane).toBe(true);
  });

  it("uses a trimmed single-column operator surface for focus layout", () => {
    const layout = getCockpitLayout("focus");

    expect(layout.centerSingleColumn).toBe(true);
    expect(layout.paneVisibility.sessions_pane).toBe(false);
    expect(layout.paneVisibility.goals_pane).toBe(false);
    expect(layout.paneVisibility.outputs_pane).toBe(false);
    expect(layout.paneVisibility.guardian_state_pane).toBe(true);
    expect(layout.paneVisibility.operator_timeline_pane).toBe(true);
    expect(layout.paneVisibility.workflows_pane).toBe(false);
    expect(layout.paneVisibility.audit_pane).toBe(false);
    expect(layout.paneVisibility.trace_pane).toBe(false);
    expect(layout.paneVisibility.inspector_pane).toBe(false);
    expect(layout.paneVisibility.operator_surface_pane).toBe(false);
    expect(layout.paneVisibility.presence_pane).toBe(true);
    expect(layout.paneVisibility.response_pane).toBe(false);
  });

  it("biases review layout toward evidence surfaces", () => {
    const layout = getCockpitLayout("review");

    expect(layout.centerSingleColumn).toBe(true);
    expect(layout.paneVisibility.sessions_pane).toBe(false);
    expect(layout.paneVisibility.guardian_state_pane).toBe(false);
    expect(layout.paneVisibility.operator_timeline_pane).toBe(true);
    expect(layout.paneVisibility.workflows_pane).toBe(true);
    expect(layout.paneVisibility.audit_pane).toBe(true);
    expect(layout.paneVisibility.trace_pane).toBe(true);
    expect(layout.paneVisibility.presence_pane).toBe(false);
    expect(layout.paneVisibility.conversation_pane).toBe(false);
  });
});
