import { describe, it, expect } from "vitest";
import {
  getToolEffect,
  getFacingDirection,
  getIdleState,
  getThinkingState,
} from "./animationStateMachine";
import { POSITIONS } from "../config/constants";

describe("getToolEffect", () => {
  it("returns casting effect with valid pool", () => {
    const effect = getToolEffect("web_search", 4);
    expect(effect).not.toBeNull();
    expect(effect!.animationState).toBe("casting");
    expect(effect!.effectIndex).toBeGreaterThanOrEqual(0);
    expect(effect!.effectIndex).toBeLessThan(4);
  });

  it("returns deterministic index for same tool name", () => {
    const a = getToolEffect("shell_execute", 10);
    const b = getToolEffect("shell_execute", 10);
    expect(a!.effectIndex).toBe(b!.effectIndex);
  });

  it("returns different indices for different tools", () => {
    const a = getToolEffect("web_search", 100);
    const b = getToolEffect("shell_execute", 100);
    expect(a!.effectIndex).not.toBe(b!.effectIndex);
  });

  it("returns null when pool is empty", () => {
    expect(getToolEffect("web_search", 0)).toBeNull();
  });

  it("returns null when pool size is negative", () => {
    expect(getToolEffect("web_search", -1)).toBeNull();
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
