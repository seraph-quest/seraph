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
  // Phase 1 tools
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
  // Phase 2 tools
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
  [TOOL_NAMES.GET_CALENDAR_EVENTS]: {
    tool: TOOL_NAMES.GET_CALENDAR_EVENTS,
    positionX: POSITIONS.clock,
    animationState: "at-clock",
    pixelX: SCENE.POSITIONS.clock.x,
    pixelY: SCENE.POSITIONS.clock.y,
  },
  [TOOL_NAMES.CREATE_CALENDAR_EVENT]: {
    tool: TOOL_NAMES.CREATE_CALENDAR_EVENT,
    positionX: POSITIONS.clock,
    animationState: "at-clock",
    pixelX: SCENE.POSITIONS.clock.x,
    pixelY: SCENE.POSITIONS.clock.y,
  },
  [TOOL_NAMES.READ_EMAILS]: {
    tool: TOOL_NAMES.READ_EMAILS,
    positionX: POSITIONS.mailbox,
    animationState: "at-mailbox",
    pixelX: SCENE.POSITIONS.mailbox.x,
    pixelY: SCENE.POSITIONS.mailbox.y,
  },
  [TOOL_NAMES.SEND_EMAIL]: {
    tool: TOOL_NAMES.SEND_EMAIL,
    positionX: POSITIONS.mailbox,
    animationState: "at-mailbox",
    pixelX: SCENE.POSITIONS.mailbox.x,
    pixelY: SCENE.POSITIONS.mailbox.y,
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
