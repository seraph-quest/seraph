export const API_URL = import.meta.env.VITE_API_URL || "http://localhost:8004";
export const WS_URL = import.meta.env.VITE_WS_URL || "ws://localhost:8004/ws/chat";

export const WALK_DURATION_MS = 800;
export const SPEECH_DISPLAY_MS = 3000;
export const WS_RECONNECT_DELAY_MS = 3000;
export const WS_PING_INTERVAL_MS = 30000;

export const POSITIONS = {
  well: 15,
  bench: 50,
  signpost: 85,
} as const;

export const TOOL_NAMES = {
  WEB_SEARCH: "web_search",
  READ_FILE: "read_file",
  WRITE_FILE: "write_file",
  FILL_TEMPLATE: "fill_template",
} as const;

export const SCENE = {
  TILE_SIZE: 16,
  MAP_COLS: 64,
  MAP_ROWS: 32,
  MAP_PIXEL_WIDTH: 1024,  // 64 * 16
  MAP_PIXEL_HEIGHT: 512,  // 32 * 16
  SPRITE_SCALE: 1,
  WALK_SPEED: 300,

  // Tool station positions (village-local coords — where agent walks to)
  POSITIONS: {
    house1: { x: 192, y: 280 },    // HOUSE 1 — web_search
    church: { x: 512, y: 240 },    // CHURCH  — fill_template (on courtyard)
    house2: { x: 832, y: 280 },    // HOUSE 2 — read/write file
    bench:  { x: 512, y: 350 },    // idle / thinking spot (south path)
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
