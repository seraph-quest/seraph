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

// Static targets for native tools (fallback when map/API hasn't loaded yet)
const STATIC_TOOL_TARGETS: Record<string, ToolTargetWithPixels> = {
  [TOOL_NAMES.WEB_SEARCH]: {
    tool: TOOL_NAMES.WEB_SEARCH,
    positionX: POSITIONS.well,
    animationState: "at-well",
    pixelX: 192,
    pixelY: 280,
  },
  [TOOL_NAMES.READ_FILE]: {
    tool: TOOL_NAMES.READ_FILE,
    positionX: POSITIONS.signpost,
    animationState: "at-signpost",
    pixelX: 832,
    pixelY: 280,
  },
  [TOOL_NAMES.WRITE_FILE]: {
    tool: TOOL_NAMES.WRITE_FILE,
    positionX: POSITIONS.signpost,
    animationState: "at-signpost",
    pixelX: 832,
    pixelY: 280,
  },
  [TOOL_NAMES.FILL_TEMPLATE]: {
    tool: TOOL_NAMES.FILL_TEMPLATE,
    positionX: POSITIONS.bench,
    animationState: "at-bench",
    pixelX: 512,
    pixelY: 240,
  },
  [TOOL_NAMES.VIEW_SOUL]: {
    tool: TOOL_NAMES.VIEW_SOUL,
    positionX: POSITIONS.bench,
    animationState: "at-bench",
    pixelX: 512,
    pixelY: 240,
  },
  [TOOL_NAMES.UPDATE_SOUL]: {
    tool: TOOL_NAMES.UPDATE_SOUL,
    positionX: POSITIONS.bench,
    animationState: "at-bench",
    pixelX: 512,
    pixelY: 240,
  },
  [TOOL_NAMES.CREATE_GOAL]: {
    tool: TOOL_NAMES.CREATE_GOAL,
    positionX: POSITIONS.bench,
    animationState: "at-bench",
    pixelX: 512,
    pixelY: 240,
  },
  [TOOL_NAMES.UPDATE_GOAL]: {
    tool: TOOL_NAMES.UPDATE_GOAL,
    positionX: POSITIONS.bench,
    animationState: "at-bench",
    pixelX: 512,
    pixelY: 240,
  },
  [TOOL_NAMES.GET_GOALS]: {
    tool: TOOL_NAMES.GET_GOALS,
    positionX: POSITIONS.bench,
    animationState: "at-bench",
    pixelX: 512,
    pixelY: 240,
  },
  [TOOL_NAMES.GET_GOAL_PROGRESS]: {
    tool: TOOL_NAMES.GET_GOAL_PROGRESS,
    positionX: POSITIONS.bench,
    animationState: "at-bench",
    pixelX: 512,
    pixelY: 240,
  },
  [TOOL_NAMES.SHELL_EXECUTE]: {
    tool: TOOL_NAMES.SHELL_EXECUTE,
    positionX: POSITIONS.forge,
    animationState: "at-forge",
    pixelX: 384,
    pixelY: 320,
  },
  [TOOL_NAMES.BROWSE_WEBPAGE]: {
    tool: TOOL_NAMES.BROWSE_WEBPAGE,
    positionX: POSITIONS.tower,
    animationState: "at-tower",
    pixelX: 640,
    pixelY: 200,
  },
};

export function getToolTarget(toolName: string): ToolTargetWithPixels | null {
  // 1. Check dynamic tool station positions from the map (highest priority)
  const stationPositions = useChatStore.getState().toolStationPositions;
  const station = stationPositions[toolName];
  if (station) {
    return {
      tool: toolName,
      positionX: Math.round((station.x / SCENE.MAP_PIXEL_WIDTH) * 100),
      animationState: station.animation as AgentAnimationState,
      pixelX: station.x,
      pixelY: station.y,
    };
  }

  // 2. Check static targets
  const staticTarget = STATIC_TOOL_TARGETS[toolName];
  if (staticTarget) return staticTarget;

  // 3. Check dynamic tool registry from API (MCP tools)
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
