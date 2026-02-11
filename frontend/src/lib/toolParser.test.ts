import { describe, it, expect } from "vitest";
import { detectToolFromStep } from "./toolParser";

describe("detectToolFromStep", () => {
  // Regex pattern: ToolCall(name='...')
  it("detects ToolCall pattern", () => {
    expect(detectToolFromStep("ToolCall(name='web_search', ...)")).toBe("web_search");
  });

  it("detects ToolCall with double quotes", () => {
    expect(detectToolFromStep('ToolCall(name="read_file", args={})')).toBe("read_file");
  });

  // Regex pattern: tool_name: '...'
  it("detects tool_name pattern with colon", () => {
    expect(detectToolFromStep("tool_name: 'shell_execute'")).toBe("shell_execute");
  });

  it("detects tool_name pattern with equals", () => {
    expect(detectToolFromStep('tool_name="write_file"')).toBe("write_file");
  });

  // Regex pattern: Calling tool: '...'
  it("detects Calling tool pattern", () => {
    expect(detectToolFromStep("Calling tool: 'fill_template'")).toBe("fill_template");
  });

  // Regex pattern: Using tool: '...'
  it("detects Using tool pattern", () => {
    expect(detectToolFromStep("Using tool: browse_webpage")).toBe("browse_webpage");
  });

  // Regex pattern: "tool": "..."
  it("detects JSON tool key pattern", () => {
    expect(detectToolFromStep('{"tool": "view_soul", "args": {}}')).toBe("view_soul");
  });

  // Fallback substring matching
  it("falls back to substring match", () => {
    expect(detectToolFromStep("I will now use web_search to find...")).toBe("web_search");
  });

  it("returns null for unknown tool", () => {
    expect(detectToolFromStep("ToolCall(name='unknown_tool')")).toBeNull();
  });

  it("returns null when no tool detected", () => {
    expect(detectToolFromStep("Just thinking about the problem...")).toBeNull();
  });

  // Phase 1 tools
  it("detects soul tools", () => {
    expect(detectToolFromStep("Calling tool: 'update_soul'")).toBe("update_soul");
  });

  it("detects goal tools", () => {
    expect(detectToolFromStep("Calling tool: 'create_goal'")).toBe("create_goal");
  });
});
