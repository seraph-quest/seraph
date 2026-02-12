import type { TilesetCategory } from "../lib/tileset-loader";

export type EditorTool = "brush" | "eraser" | "fill" | "stamp" | "object" | "walkability" | "hand";

export interface TileSelection {
  startCol: number;
  startRow: number;
  endCol: number;
  endRow: number;
}

export interface Stamp {
  name: string;
  category: string;
  /** Tile GIDs arranged row-major, -1 = empty */
  tiles: number[][];
  /** Which tiles in this stamp are not walkable */
  collisionMask: boolean[][];
  width: number;
  height: number;
  tilesetIndex: number;
}

export interface MapDelta {
  layerIndex: number;
  changes: Array<{ x: number; y: number; oldValue: number; newValue: number }>;
}

export interface SpawnPoint {
  name: string;
  type: "spawn_point";
  x: number;
  y: number;
  /** Character sprite sheet assigned to this spawn (e.g. "Character_001") */
  spriteSheet?: string;
}

export interface NPC {
  name: string;
  type: "npc";
  x: number;
  y: number;
  spriteSheet: string;
  spriteType: "character" | "enemy";
  frameCol: number;
  frameRow: number;
}

export type MapObject = SpawnPoint | NPC;

/** A recently used tile selection for quick re-selection */
export interface RecentTileSelection {
  tilesetIndex: number;
  selection: TileSelection;
  /** Timestamp for ordering (most recent first) */
  usedAt: number;
}

/** A single tile that participates in an animation — the anchor local ID + its frame sequence */
export interface TileAnimationEntry {
  anchorLocalId: number;
  frames: number[];
}

/** User-defined animation group (a set of tiles that share timing) */
export interface TileAnimationGroup {
  id: string;
  name: string;
  tilesetIndex: number;
  frameDuration: number;
  entries: TileAnimationEntry[];
  /** When true, this group is included in the magic effect pool for tool-use animations */
  isMagicEffect?: boolean;
}

/** Pre-computed lookup: GID → animation frames for fast rendering */
export type AnimationLookup = Map<
  number,
  { frames: { gid: number; duration: number }[]; totalDuration: number }
>;

export interface LoadedTileset {
  name: string;
  image: HTMLImageElement;
  imageWidth: number;
  imageHeight: number;
  tileWidth: number;
  tileHeight: number;
  columns: number;
  rows: number;
  tileCount: number;
  firstGid: number;
  category: TilesetCategory;
  /** Per-tile walkability (indexed by local tile ID) */
  walkability: boolean[];
}
