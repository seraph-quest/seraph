import { describe, expect, it } from "vitest";

import { COCKPIT_LAYOUTS, getCockpitLayout } from "./layouts";

describe("cockpit layouts", () => {
  it("exposes the persisted default, focus, and review layouts", () => {
    expect(Object.keys(COCKPIT_LAYOUTS)).toEqual(["default", "focus", "review"]);
  });

  it("keeps the full cockpit surface in the default layout", () => {
    const layout = getCockpitLayout("default");

    expect(layout.centerSingleColumn).toBe(false);
    expect(layout.sections.rail).toBe(true);
    expect(layout.sections.timeline).toBe(true);
    expect(layout.sections.workflows).toBe(true);
    expect(layout.sections.audit).toBe(true);
    expect(layout.sections.trace).toBe(true);
    expect(layout.sections.conversation).toBe(true);
  });

  it("uses a trimmed single-column operator surface for focus layout", () => {
    const layout = getCockpitLayout("focus");

    expect(layout.centerSingleColumn).toBe(true);
    expect(layout.sections.rail).toBe(false);
    expect(layout.sections.guardianState).toBe(true);
    expect(layout.sections.timeline).toBe(true);
    expect(layout.sections.workflows).toBe(true);
    expect(layout.sections.audit).toBe(false);
    expect(layout.sections.trace).toBe(false);
    expect(layout.sections.inspector).toBe(true);
  });

  it("biases review layout toward evidence surfaces", () => {
    const layout = getCockpitLayout("review");

    expect(layout.centerSingleColumn).toBe(true);
    expect(layout.sections.rail).toBe(true);
    expect(layout.sections.guardianState).toBe(false);
    expect(layout.sections.timeline).toBe(true);
    expect(layout.sections.workflows).toBe(true);
    expect(layout.sections.audit).toBe(true);
    expect(layout.sections.trace).toBe(true);
  });
});
