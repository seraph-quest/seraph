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
    positionX: POSITIONS.computer,
    animationState: "at-computer",
    pixelX: SCENE.POSITIONS.computer.x,
    pixelY: SCENE.POSITIONS.computer.y,
  },
  [TOOL_NAMES.READ_FILE]: {
    tool: TOOL_NAMES.READ_FILE,
    positionX: POSITIONS.cabinet,
    animationState: "at-cabinet",
    pixelX: SCENE.POSITIONS.cabinet.x,
    pixelY: SCENE.POSITIONS.cabinet.y,
  },
  [TOOL_NAMES.WRITE_FILE]: {
    tool: TOOL_NAMES.WRITE_FILE,
    positionX: POSITIONS.cabinet,
    animationState: "at-cabinet",
    pixelX: SCENE.POSITIONS.cabinet.x,
    pixelY: SCENE.POSITIONS.cabinet.y,
  },
  [TOOL_NAMES.FILL_TEMPLATE]: {
    tool: TOOL_NAMES.FILL_TEMPLATE,
    positionX: POSITIONS.desk,
    animationState: "at-desk",
    pixelX: SCENE.POSITIONS.desk.x,
    pixelY: SCENE.POSITIONS.desk.y,
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
  return { animationState: "idle", positionX: POSITIONS.desk };
}

export function getThinkingState(): {
  animationState: AgentAnimationState;
  positionX: number;
} {
  return { animationState: "thinking", positionX: POSITIONS.desk };
}
