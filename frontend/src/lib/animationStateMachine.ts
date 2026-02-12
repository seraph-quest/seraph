import type { AgentAnimationState, FacingDirection } from "../types";
import { POSITIONS } from "../config/constants";

/** djb2 string hash — deterministic mapping from tool name to effect index */
function djb2Hash(str: string): number {
  let hash = 5381;
  for (let i = 0; i < str.length; i++) {
    hash = ((hash << 5) + hash + str.charCodeAt(i)) >>> 0;
  }
  return hash;
}

export interface ToolEffect {
  tool: string;
  effectIndex: number;
  animationState: "casting";
}

/**
 * Map a tool name to a magic effect from the pool via hash.
 * Returns null if no effects are available (pool empty) — caller should
 * fall back to the thinking animation.
 */
export function getToolEffect(
  toolName: string,
  effectPoolSize: number
): ToolEffect | null {
  if (effectPoolSize <= 0) return null;
  return {
    tool: toolName,
    effectIndex: djb2Hash(toolName) % effectPoolSize,
    animationState: "casting",
  };
}

export function getFacingDirection(
  currentX: number,
  targetX: number
): FacingDirection {
  return targetX < currentX ? "left" : "right";
}

export function getIdleState(): {
  animationState: AgentAnimationState;
  positionX: number;
} {
  return { animationState: "idle", positionX: POSITIONS.bench };
}

export function getThinkingState(): {
  animationState: AgentAnimationState;
  positionX: number;
} {
  return { animationState: "thinking", positionX: POSITIONS.bench };
}
