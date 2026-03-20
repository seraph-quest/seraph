import { describe, it, expect } from "vitest";
import { TOOL_NAMES, POSITIONS } from "./constants";

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
