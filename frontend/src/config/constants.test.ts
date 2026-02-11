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
    for (const [key, value] of Object.entries(POSITIONS)) {
      expect(value).toBeGreaterThanOrEqual(0);
      expect(value).toBeLessThanOrEqual(100);
    }
  });
});

describe("SCENE.POSITIONS", () => {
  it("has all required station keys", () => {
    const required = ["house1", "church", "house2", "bench", "forge", "tower", "clock", "mailbox"];
    for (const key of required) {
      expect(SCENE.POSITIONS).toHaveProperty(key);
    }
  });
});

describe("SCENE.WANDERING", () => {
  it("has at least 10 waypoints", () => {
    expect(SCENE.WANDERING.WAYPOINTS.length).toBeGreaterThanOrEqual(10);
  });
});
