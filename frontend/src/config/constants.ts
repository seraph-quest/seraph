export const API_URL = import.meta.env.VITE_API_URL || "http://localhost:8004";
export const WS_URL = import.meta.env.VITE_WS_URL || "ws://localhost:8004/ws/chat";

export const WALK_DURATION_MS = 800;
export const SPEECH_DISPLAY_MS = 3000;
export const WS_RECONNECT_DELAY_MS = 3000;
export const WS_PING_INTERVAL_MS = 30000;

// Percentage-based positions for animation state machine (fallback)
export const POSITIONS = {
  mailbox: 10,
  well: 15,
  forge: 35,
  bench: 50,
  tower: 60,
  clock: 55,
  signpost: 85,
} as const;

// Native tool names (static fallback — dynamic tools loaded from API)
export const TOOL_NAMES = {
  WEB_SEARCH: "web_search",
  READ_FILE: "read_file",
  WRITE_FILE: "write_file",
  FILL_TEMPLATE: "fill_template",
  SHELL_EXECUTE: "shell_execute",
  BROWSE_WEBPAGE: "browse_webpage",
  VIEW_SOUL: "view_soul",
  UPDATE_SOUL: "update_soul",
  CREATE_GOAL: "create_goal",
  UPDATE_GOAL: "update_goal",
  GET_GOALS: "get_goals",
  GET_GOAL_PROGRESS: "get_goal_progress",
} as const;

export const SCENE = {
  TILE_SIZE: 16,
  MAP_COLS: 64,
  MAP_ROWS: 40,
  MAP_PIXEL_WIDTH: 1024,  // 64 * 16
  MAP_PIXEL_HEIGHT: 640,  // 40 * 16
  SPRITE_SCALE: 2,
  WALK_SPEED: 300,

  /** Tiled JSON map file path (relative to public/) */
  MAP_FILE: "maps/village.json",

  COLORS: {
    bubbleBg: 0xe0e0e0,
    bubbleBorder: 0xe2b714,
    bubbleText: 0x1a1a2e,
  },

  WANDERING: {
    MIN_DELAY_MS: 3000,
    MAX_DELAY_MS: 6000,
  },
} as const;
