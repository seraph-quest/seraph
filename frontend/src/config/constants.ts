const localWsUrl = () => {
  if (typeof window === "undefined") {
    return "ws://localhost:8004/ws/chat";
  }
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  if (window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1") {
    return `ws://${window.location.hostname}:8004/ws/chat`;
  }
  return `${protocol}//${window.location.host}/ws/chat`;
};

const configuredApiUrl = import.meta.env.VITE_API_URL;
const localApiUrl = () => {
  if (typeof window === "undefined") {
    return "http://localhost:8004";
  }
  if (window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1") {
    return `http://${window.location.hostname}:8004`;
  }
  return "";
};

export const API_URL = configuredApiUrl === "/api" ? "" : configuredApiUrl || localApiUrl();
export const WS_URL = import.meta.env.VITE_WS_URL || localWsUrl();

export const WALK_DURATION_MS = 800;
export const SPEECH_DISPLAY_MS = 3000;
export const WS_RECONNECT_DELAY_MS = 3000;
export const WS_PING_INTERVAL_MS = 30000;

// Percentage-based positions for animation state machine (fallback)
export const POSITIONS = {
  bench: 50,
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
  STORE_SECRET: "store_secret",
  GET_SECRET: "get_secret",
  LIST_SECRETS: "list_secrets",
  DELETE_SECRET: "delete_secret",
} as const;
