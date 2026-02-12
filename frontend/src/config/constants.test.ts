import { describe, it, expect } from "vitest";
import { TOOL_NAMES, POSITIONS, SCENE } from "./constants";

describe("TOOL_NAMES", () => {
  it("has expected count of native tools", () => {
    const count = Object.keys(TOOL_NAMES).length;
    // 12 native tools (MCP tools loaded dynamically from API)
    expect(count).toBeGreaterThanOrEqual(12);
  });
});

describe("POSITIONS", () => {
  it("all values are in 0-100 range", () => {
    for (const [, value] of Object.entries(POSITIONS)) {
      expect(value).toBeGreaterThanOrEqual(0);
      expect(value).toBeLessThanOrEqual(100);
    }
  });
});

describe("SCENE", () => {
  it("has map file path", () => {
    expect(SCENE.MAP_FILE).toBeDefined();
    expect(SCENE.MAP_FILE).toContain("village.json");
  });

  it("has wandering timing", () => {
    expect(SCENE.WANDERING.MIN_DELAY_MS).toBeGreaterThan(0);
    expect(SCENE.WANDERING.MAX_DELAY_MS).toBeGreaterThan(SCENE.WANDERING.MIN_DELAY_MS);
  });
});
