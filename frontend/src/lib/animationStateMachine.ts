import type { AgentAnimationState, FacingDirection } from "../types";
import { POSITIONS, TOOL_NAMES, SCENE } from "../config/constants";

interface ToolTargetWithPixels {
  tool: string;
  positionX: number;
  animationState: AgentAnimationState;
  pixelX: number;
  pixelY: number;
}

const TOOL_TARGETS: Record<string, ToolTargetWithPixels> = {
  [TOOL_NAMES.WEB_SEARCH]: {
    tool: TOOL_NAMES.WEB_SEARCH,
    positionX: POSITIONS.well,
    animationState: "at-well",
    pixelX: SCENE.POSITIONS.house1.x,
    pixelY: SCENE.POSITIONS.house1.y,
  },
  [TOOL_NAMES.READ_FILE]: {
    tool: TOOL_NAMES.READ_FILE,
    positionX: POSITIONS.signpost,
    animationState: "at-signpost",
    pixelX: SCENE.POSITIONS.house2.x,
    pixelY: SCENE.POSITIONS.house2.y,
  },
  [TOOL_NAMES.WRITE_FILE]: {
    tool: TOOL_NAMES.WRITE_FILE,
    positionX: POSITIONS.signpost,
    animationState: "at-signpost",
    pixelX: SCENE.POSITIONS.house2.x,
    pixelY: SCENE.POSITIONS.house2.y,
  },
  [TOOL_NAMES.FILL_TEMPLATE]: {
    tool: TOOL_NAMES.FILL_TEMPLATE,
    positionX: POSITIONS.bench,
    animationState: "at-bench",
    pixelX: SCENE.POSITIONS.church.x,
    pixelY: SCENE.POSITIONS.church.y,
  },
};

export function getToolTarget(toolName: string): ToolTargetWithPixels | null {
  return TOOL_TARGETS[toolName] ?? null;
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
