import { describe, it, expect } from "vitest";
import {
  getToolTarget,
  getFacingDirection,
  getIdleState,
  getThinkingState,
} from "./animationStateMachine";
import { POSITIONS } from "../config/constants";

describe("getToolTarget", () => {
  it("returns target for web_search", () => {
    const target = getToolTarget("web_search");
    expect(target).not.toBeNull();
    expect(target!.animationState).toBe("at-well");
    expect(target!.positionX).toBe(POSITIONS.well);
  });

  it("returns target for shell_execute", () => {
    const target = getToolTarget("shell_execute");
    expect(target).not.toBeNull();
    expect(target!.animationState).toBe("at-forge");
  });

  it("returns target for browse_webpage", () => {
    const target = getToolTarget("browse_webpage");
    expect(target).not.toBeNull();
    expect(target!.animationState).toBe("at-tower");
  });

  it("returns target for read_file", () => {
    const target = getToolTarget("read_file");
    expect(target).not.toBeNull();
    expect(target!.animationState).toBe("at-signpost");
  });

  it("returns target for view_soul", () => {
    const target = getToolTarget("view_soul");
    expect(target).not.toBeNull();
    expect(target!.animationState).toBe("at-bench");
  });

  it("returns null for unknown tool", () => {
    expect(getToolTarget("nonexistent_tool")).toBeNull();
  });
});

describe("getFacingDirection", () => {
  it("returns left when target is to the left", () => {
    expect(getFacingDirection(50, 10)).toBe("left");
  });

  it("returns right when target is to the right", () => {
    expect(getFacingDirection(10, 50)).toBe("right");
  });

  it("returns right when at same position", () => {
    expect(getFacingDirection(50, 50)).toBe("right");
  });
});

describe("getIdleState", () => {
  it("returns idle at bench", () => {
    const state = getIdleState();
    expect(state.animationState).toBe("idle");
    expect(state.positionX).toBe(POSITIONS.bench);
  });
});

describe("getThinkingState", () => {
  it("returns thinking at bench", () => {
    const state = getThinkingState();
    expect(state.animationState).toBe("thinking");
    expect(state.positionX).toBe(POSITIONS.bench);
  });
});
