export const API_URL = import.meta.env.VITE_API_URL || "http://localhost:8004";
export const WS_URL = import.meta.env.VITE_WS_URL || "ws://localhost:8004/ws/chat";

export const WALK_DURATION_MS = 800;
export const SPEECH_DISPLAY_MS = 3000;
export const WS_RECONNECT_DELAY_MS = 3000;
export const WS_PING_INTERVAL_MS = 30000;

export const POSITIONS = {
  mailbox: 10,
  well: 15,
  forge: 35,
  bench: 50,
  tower: 60,
  clock: 55,
  signpost: 85,
} as const;

export const TOOL_NAMES = {
  WEB_SEARCH: "web_search",
  READ_FILE: "read_file",
  WRITE_FILE: "write_file",
  FILL_TEMPLATE: "fill_template",
  SHELL_EXECUTE: "shell_execute",
  BROWSE_WEBPAGE: "browse_webpage",
  GET_CALENDAR_EVENTS: "get_calendar_events",
  CREATE_CALENDAR_EVENT: "create_calendar_event",
  READ_EMAILS: "read_emails",
  SEND_EMAIL: "send_email",
  VIEW_SOUL: "view_soul",
  UPDATE_SOUL: "update_soul",
  CREATE_GOAL: "create_goal",
  UPDATE_GOAL: "update_goal",
  GET_GOALS: "get_goals",
  GET_GOAL_PROGRESS: "get_goal_progress",
  // Things3 MCP tools
  GET_INBOX: "get_inbox",
  GET_TODAY: "get_today",
  GET_UPCOMING: "get_upcoming",
  GET_ANYTIME: "get_anytime",
  GET_SOMEDAY: "get_someday",
  GET_LOGBOOK: "get_logbook",
  GET_TRASH: "get_trash",
  GET_TODOS: "get_todos",
  GET_PROJECTS: "get_projects",
  GET_AREAS: "get_areas",
  GET_TAGS: "get_tags",
  GET_TAGGED_ITEMS: "get_tagged_items",
  GET_HEADINGS: "get_headings",
  SEARCH_TODOS: "search_todos",
  SEARCH_ADVANCED: "search_advanced",
  GET_RECENT: "get_recent",
  ADD_TODO: "add_todo",
  ADD_PROJECT: "add_project",
  UPDATE_TODO: "update_todo",
  UPDATE_PROJECT: "update_project",
  SHOW_ITEM: "show_item",
  SEARCH_ITEMS: "search_items",
} as const;

export const SCENE = {
  TILE_SIZE: 16,
  MAP_COLS: 64,
  MAP_ROWS: 32,
  MAP_PIXEL_WIDTH: 1024,  // 64 * 16
  MAP_PIXEL_HEIGHT: 512,  // 32 * 16
  SPRITE_SCALE: 2,
  WALK_SPEED: 300,

  // Tool station positions (village-local coords — where agent walks to)
  POSITIONS: {
    house1:  { x: 192, y: 280 },    // HOUSE 1 — web_search
    church:  { x: 512, y: 240 },    // CHURCH  — fill_template / soul / goals
    house2:  { x: 832, y: 280 },    // HOUSE 2 — read/write file
    bench:   { x: 512, y: 350 },    // idle / thinking spot (south path)
    forge:   { x: 384, y: 320 },    // FORGE — shell_execute
    tower:   { x: 640, y: 200 },    // TOWER — browse_webpage
    clock:   { x: 576, y: 340 },    // CLOCK — calendar
    mailbox: { x: 128, y: 340 },    // MAILBOX — email
    userHome: { x: 832, y: 340 },   // User avatar default position
  },

  COLORS: {
    bubbleBg: 0xe0e0e0,
    bubbleBorder: 0xe2b714,
    bubbleText: 0x1a1a2e,
  },

  WANDERING: {
    WAYPOINTS: [
      { x: 192, y: 280 },   // in front of house 1
      { x: 350, y: 280 },   // path left-center
      { x: 512, y: 240 },   // church courtyard
      { x: 670, y: 280 },   // path right-center
      { x: 832, y: 280 },   // in front of house 2
      { x: 512, y: 350 },   // south path (bench)
      { x: 300, y: 340 },   // near the well
      { x: 720, y: 340 },   // near the stairs
      { x: 384, y: 320 },   // near the forge
      { x: 640, y: 200 },   // near the tower
      { x: 576, y: 340 },   // near the clock
      { x: 128, y: 340 },   // near the mailbox
    ],
    MIN_DELAY_MS: 3000,
    MAX_DELAY_MS: 6000,
  },

  FOREST: {
    TILE_SPACING: 80,
    BUFFER_TILES: 3,
    DENSITY: 0.45,
    MAX_TREES: 300,
  },

  // Day/night: hours 6-17 = day, 18-5 = night
  DAY_START_HOUR: 6,
  DAY_END_HOUR: 18,
} as const;
