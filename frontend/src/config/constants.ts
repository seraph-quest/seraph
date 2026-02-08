export const API_URL = import.meta.env.VITE_API_URL || "http://localhost:8004";
export const WS_URL = import.meta.env.VITE_WS_URL || "ws://localhost:8004/ws/chat";

export const WALK_DURATION_MS = 800;
export const SPEECH_DISPLAY_MS = 3000;
export const WS_RECONNECT_DELAY_MS = 3000;
export const WS_PING_INTERVAL_MS = 30000;

export const POSITIONS = {
  computer: 15,
  desk: 50,
  cabinet: 85,
} as const;

export const TOOL_NAMES = {
  WEB_SEARCH: "web_search",
  READ_FILE: "read_file",
  WRITE_FILE: "write_file",
  FILL_TEMPLATE: "fill_template",
} as const;

export const SCENE = {
  WIDTH: 768,
  HEIGHT: 320,
  TILE_SIZE: 32,
  FLOOR_Y: 192,
  SPRITE_SCALE: 2,
  WALK_SPEED: 300,

  POSITIONS: {
    computer: { x: 115, y: 224 },
    desk: { x: 384, y: 224 },
    cabinet: { x: 653, y: 224 },
    door: { x: 384, y: 288 },
  },

  COLORS: {
    wallTop: 0x0a0a2e,
    wallBottom: 0x162447,
    floorTop: 0x3b2d1a,
    floorBottom: 0x2a1f0f,
    floorLine: 0xe2b714,
    border: 0xe2b714,
    panelBg: 0x16213e,
    text: 0xe0e0e0,
    bubbleBg: 0xe0e0e0,
    bubbleBorder: 0xe2b714,
    bubbleText: 0x1a1a2e,
    monitorScreen: 0x0f3460,
    monitorGreen: 0x00ff88,
    deskSurface: 0x5c3a1e,
    deskLegs: 0x3b2d1a,
    cabinetBody: 0x4a4a5e,
    cabinetDrawer: 0x3a3a4e,
    cabinetHandle: 0xe2b714,
    doorFrame: 0x5c3a1e,
    doorBody: 0x3b2d1a,
    doorKnob: 0xe2b714,
    wallDot: 0x1a3a5e,
    paper: 0xd4d4d4,
    pen: 0xe2b714,
  },
} as const;
