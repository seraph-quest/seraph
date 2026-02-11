import type { AgentAnimationState, FacingDirection } from "../types";
import { POSITIONS, TOOL_NAMES, SCENE } from "../config/constants";
import { useChatStore } from "../stores/chatStore";

interface ToolTargetWithPixels {
  tool: string;
  positionX: number;
  animationState: AgentAnimationState;
  pixelX: number;
  pixelY: number;
}

// Static targets for native tools (fallback when API hasn't loaded yet)
const STATIC_TOOL_TARGETS: Record<string, ToolTargetWithPixels> = {
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
  [TOOL_NAMES.VIEW_SOUL]: {
    tool: TOOL_NAMES.VIEW_SOUL,
    positionX: POSITIONS.bench,
    animationState: "at-bench",
    pixelX: SCENE.POSITIONS.church.x,
    pixelY: SCENE.POSITIONS.church.y,
  },
  [TOOL_NAMES.UPDATE_SOUL]: {
    tool: TOOL_NAMES.UPDATE_SOUL,
    positionX: POSITIONS.bench,
    animationState: "at-bench",
    pixelX: SCENE.POSITIONS.church.x,
    pixelY: SCENE.POSITIONS.church.y,
  },
  [TOOL_NAMES.CREATE_GOAL]: {
    tool: TOOL_NAMES.CREATE_GOAL,
    positionX: POSITIONS.bench,
    animationState: "at-bench",
    pixelX: SCENE.POSITIONS.church.x,
    pixelY: SCENE.POSITIONS.church.y,
  },
  [TOOL_NAMES.UPDATE_GOAL]: {
    tool: TOOL_NAMES.UPDATE_GOAL,
    positionX: POSITIONS.bench,
    animationState: "at-bench",
    pixelX: SCENE.POSITIONS.church.x,
    pixelY: SCENE.POSITIONS.church.y,
  },
  [TOOL_NAMES.GET_GOALS]: {
    tool: TOOL_NAMES.GET_GOALS,
    positionX: POSITIONS.bench,
    animationState: "at-bench",
    pixelX: SCENE.POSITIONS.church.x,
    pixelY: SCENE.POSITIONS.church.y,
  },
  [TOOL_NAMES.GET_GOAL_PROGRESS]: {
    tool: TOOL_NAMES.GET_GOAL_PROGRESS,
    positionX: POSITIONS.bench,
    animationState: "at-bench",
    pixelX: SCENE.POSITIONS.church.x,
    pixelY: SCENE.POSITIONS.church.y,
  },
  [TOOL_NAMES.SHELL_EXECUTE]: {
    tool: TOOL_NAMES.SHELL_EXECUTE,
    positionX: POSITIONS.forge,
    animationState: "at-forge",
    pixelX: SCENE.POSITIONS.forge.x,
    pixelY: SCENE.POSITIONS.forge.y,
  },
  [TOOL_NAMES.BROWSE_WEBPAGE]: {
    tool: TOOL_NAMES.BROWSE_WEBPAGE,
    positionX: POSITIONS.tower,
    animationState: "at-tower",
    pixelX: SCENE.POSITIONS.tower.x,
    pixelY: SCENE.POSITIONS.tower.y,
  },
};

export function getToolTarget(toolName: string): ToolTargetWithPixels | null {
  // 1. Check static targets first
  const staticTarget = STATIC_TOOL_TARGETS[toolName];
  if (staticTarget) return staticTarget;

  // 2. Check dynamic tool registry from API
  const registry = useChatStore.getState().toolRegistry;
  const meta = registry.find((t) => t.name === toolName);
  if (meta && meta.pixelX != null && meta.pixelY != null && meta.animation) {
    return {
      tool: toolName,
      positionX: Math.round((meta.pixelX / SCENE.MAP_PIXEL_WIDTH) * 100),
      animationState: meta.animation as AgentAnimationState,
      pixelX: meta.pixelX,
      pixelY: meta.pixelY,
    };
  }

  return null;
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
