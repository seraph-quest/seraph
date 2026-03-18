import { afterEach, describe, expect, it, vi } from "vitest";

import { PANEL_GRID_SIZE, clampRect, snapRectToGrid } from "./useDragResize";

describe("useDragResize snap helpers", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("snaps pane coordinates and sizes to the shared grid", () => {
    expect(
      snapRectToGrid(
        { x: 23, y: 41, width: 317, height: 205 },
        200,
        160,
      ),
    ).toEqual({
      x: 16,
      y: 48,
      width: 320,
      height: 208,
    });
  });

  it("preserves minimum sizes while snapping", () => {
    expect(
      snapRectToGrid(
        { x: 7, y: 9, width: 100, height: 110 },
        240,
        180,
      ),
    ).toEqual({
      x: 0,
      y: 16,
      width: 240,
      height: 180,
    });
  });

  it("clamps snapped panes to the visible viewport", () => {
    vi.stubGlobal("window", { innerWidth: 1280, innerHeight: 720 });

    expect(clampRect(1277, 719, 317, 205, 200, 160)).toEqual({
      x: 1200,
      y: 640,
      width: 320,
      height: 208,
    });
  });

  it("uses the shared 16px grid", () => {
    expect(PANEL_GRID_SIZE).toBe(16);
  });
});
