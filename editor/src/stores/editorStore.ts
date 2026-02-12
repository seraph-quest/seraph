import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { EditorTool, MapDelta, MapObject, BuildingDef, BuildingFloor, CellStack } from "../types/editor";
import { floodFill } from "../lib/flood-fill";
import { UndoManager } from "../lib/undo";

const DEFAULT_MAP_WIDTH = 64;
const DEFAULT_MAP_HEIGHT = 40;
const TILE_LAYER_NAMES = ["ground", "terrain", "buildings", "decorations", "treetops"];

/** Get the topmost GID from a cell stack (0 if empty) */
function topOf(stack: CellStack): number {
  return stack.length > 0 ? stack[stack.length - 1] : 0;
}

interface EditorStore {
  // Map data
  mapWidth: number;
  mapHeight: number;
  tileSize: number;
  layers: CellStack[][];
  layerNames: string[];
  objects: MapObject[];

  // Buildings
  buildings: BuildingDef[];
  activeBuildingId: string | null;
  activeFloorIndex: number;

  // Editor state
  activeTool: EditorTool;
  activeLayerIndex: number;
  layerVisibility: boolean[];
  showGrid: boolean;
  showWalkability: boolean;
  showAnimations: boolean;

  // Viewport
  viewportOffsetX: number;
  viewportOffsetY: number;
  viewportZoom: number;

  // Panel layout
  panelWidth: number;
  layerH: number;
  objectH: number;
  buildingH: number;
  npcH: number;
  npcCollapsed: boolean;

  // Actions
  setActiveTool: (tool: EditorTool) => void;
  setActiveLayer: (index: number) => void;
  toggleLayerVisibility: (index: number) => void;
  toggleGrid: () => void;
  toggleWalkability: () => void;
  toggleAnimations: () => void;

  // Map operations
  setTile: (layerIndex: number, col: number, row: number, gid: number) => void;
  setTiles: (layerIndex: number, tiles: Array<{ col: number; row: number; gid: number }>) => void;
  fillTiles: (layerIndex: number, col: number, row: number, gid: number) => void;
  paintMultiTile: (
    layerIndex: number,
    startCol: number,
    startRow: number,
    gids: number[],
    selWidth: number,
  ) => void;

  // Objects
  addObject: (obj: MapObject) => void;
  removeObject: (index: number) => void;
  updateObject: (index: number, obj: MapObject) => void;

  // Buildings
  addBuilding: (building: BuildingDef) => void;
  removeBuilding: (id: string) => void;
  updateBuilding: (id: string, partial: Partial<BuildingDef>) => void;
  setActiveBuilding: (id: string | null) => void;
  setActiveFloor: (index: number) => void;
  setInteriorTile: (layerIndex: number, localCol: number, localRow: number, gid: number) => void;

  // Viewport
  setViewport: (offsetX: number, offsetY: number, zoom: number) => void;
  panViewport: (dx: number, dy: number) => void;
  zoomViewport: (delta: number, centerX: number, centerY: number) => void;

  // Panel layout
  setPanelWidth: (w: number) => void;
  setLayerH: (h: number) => void;
  setObjectH: (h: number) => void;
  setBuildingH: (h: number) => void;
  setNpcH: (h: number) => void;
  setNpcCollapsed: (collapsed: boolean) => void;

  // Undo/Redo
  undo: () => void;
  redo: () => void;

  // Map settings
  newMap: (width: number, height: number) => void;
  loadMapData: (layers: CellStack[][], objects: MapObject[], width: number, height: number) => void;

  // Internal
  _undoManager: UndoManager;
  _pendingDelta: MapDelta | null;
  beginStroke: () => void;
  endStroke: () => void;
}

/** Push gid onto stack (skip if top already matches). gid=0 pops top. Returns new stack. */
function stackPush(stack: CellStack, gid: number): CellStack {
  if (gid === 0) {
    // Erase: pop top
    return stack.length > 0 ? stack.slice(0, -1) : [];
  }
  // Paint: push if different from top
  if (topOf(stack) === gid) return stack;
  return [...stack, gid];
}

export const useEditorStore = create<EditorStore>()(
  persist(
    (set, get) => ({
  mapWidth: DEFAULT_MAP_WIDTH,
  mapHeight: DEFAULT_MAP_HEIGHT,
  tileSize: 16,
  layers: TILE_LAYER_NAMES.map(
    () => Array.from({ length: DEFAULT_MAP_WIDTH * DEFAULT_MAP_HEIGHT }, () => [] as number[])
  ),
  layerNames: [...TILE_LAYER_NAMES],
  objects: [],

  buildings: [],
  activeBuildingId: null,
  activeFloorIndex: 0,

  activeTool: "brush",
  activeLayerIndex: 0,
  layerVisibility: TILE_LAYER_NAMES.map(() => true),
  showGrid: true,
  showWalkability: false,
  showAnimations: true,

  viewportOffsetX: 0,
  viewportOffsetY: 0,
  viewportZoom: 2,

  panelWidth: 320,
  layerH: 140,
  objectH: 160,
  buildingH: 160,
  npcH: 200,
  npcCollapsed: false,

  _undoManager: new UndoManager(),
  _pendingDelta: null,

  setActiveTool: (tool) => set({ activeTool: tool }),
  setActiveLayer: (index) => set({ activeLayerIndex: index }),

  toggleLayerVisibility: (index) =>
    set((s) => {
      const v = [...s.layerVisibility];
      v[index] = !v[index];
      return { layerVisibility: v };
    }),

  toggleGrid: () => set((s) => ({ showGrid: !s.showGrid })),
  toggleWalkability: () => set((s) => ({ showWalkability: !s.showWalkability })),
  toggleAnimations: () => set((s) => ({ showAnimations: !s.showAnimations })),

  setTile: (layerIndex, col, row, gid) => {
    const { layers, mapWidth, mapHeight, _pendingDelta } = get();
    if (col < 0 || col >= mapWidth || row < 0 || row >= mapHeight) return;
    const idx = row * mapWidth + col;
    const oldStack = layers[layerIndex][idx];
    const newStack = stackPush(oldStack, gid);
    if (oldStack === newStack) return;

    const newLayers = layers.map((l, i) => (i === layerIndex ? [...l] : l));
    newLayers[layerIndex][idx] = newStack;

    if (_pendingDelta && _pendingDelta.layerIndex === layerIndex) {
      _pendingDelta.changes.push({ x: col, y: row, oldValue: [...oldStack], newValue: [...newStack] });
    }

    set({ layers: newLayers });
  },

  setTiles: (layerIndex, tiles) => {
    const { layers, mapWidth, mapHeight, _pendingDelta } = get();
    const newLayer = [...layers[layerIndex]];
    const changes: MapDelta["changes"] = [];

    for (const { col, row, gid } of tiles) {
      if (col < 0 || col >= mapWidth || row < 0 || row >= mapHeight) continue;
      const idx = row * mapWidth + col;
      const oldStack = newLayer[idx];
      const newStack = stackPush(oldStack, gid);
      if (oldStack === newStack) continue;
      newLayer[idx] = newStack;
      changes.push({ x: col, y: row, oldValue: [...oldStack], newValue: [...newStack] });
    }

    if (changes.length === 0) return;

    if (_pendingDelta && _pendingDelta.layerIndex === layerIndex) {
      _pendingDelta.changes.push(...changes);
    }

    const newLayers = layers.map((l, i) => (i === layerIndex ? newLayer : l));
    set({ layers: newLayers });
  },

  fillTiles: (layerIndex, col, row, gid) => {
    const { layers, mapWidth, mapHeight, _undoManager } = get();
    const newLayer = [...layers[layerIndex]];
    const changes = floodFill(newLayer, mapWidth, mapHeight, col, row, gid);
    if (changes.length === 0) return;

    const delta: MapDelta = {
      layerIndex,
      changes: changes.map((c) => {
        const idx = c.y * mapWidth + c.x;
        return { x: c.x, y: c.y, oldValue: c.oldValue, newValue: [...newLayer[idx]] };
      }),
    };
    _undoManager.push(delta);

    const newLayers = layers.map((l, i) => (i === layerIndex ? newLayer : l));
    set({ layers: newLayers });
  },

  paintMultiTile: (layerIndex, startCol, startRow, gids, selWidth) => {
    const { layers, mapWidth, mapHeight, _pendingDelta } = get();
    const newLayer = [...layers[layerIndex]];
    const selHeight = Math.ceil(gids.length / selWidth);
    const changes: MapDelta["changes"] = [];

    for (let r = 0; r < selHeight; r++) {
      for (let c = 0; c < selWidth; c++) {
        const col = startCol + c;
        const row = startRow + r;
        if (col < 0 || col >= mapWidth || row < 0 || row >= mapHeight) continue;
        const gid = gids[r * selWidth + c];
        if (gid === undefined) continue;
        const idx = row * mapWidth + col;
        const oldStack = newLayer[idx];
        const newStack = stackPush(oldStack, gid);
        if (oldStack === newStack) continue;
        newLayer[idx] = newStack;
        changes.push({ x: col, y: row, oldValue: [...oldStack], newValue: [...newStack] });
      }
    }

    if (changes.length === 0) return;
    if (_pendingDelta && _pendingDelta.layerIndex === layerIndex) {
      _pendingDelta.changes.push(...changes);
    }

    const newLayers = layers.map((l, i) => (i === layerIndex ? newLayer : l));
    set({ layers: newLayers });
  },

  addObject: (obj) =>
    set((s) => ({ objects: [...s.objects, obj] })),

  removeObject: (index) =>
    set((s) => ({ objects: s.objects.filter((_, i) => i !== index) })),

  updateObject: (index, obj) =>
    set((s) => ({
      objects: s.objects.map((o, i) => (i === index ? obj : o)),
    })),

  addBuilding: (building) =>
    set((s) => ({ buildings: [...s.buildings, building] })),

  removeBuilding: (id) =>
    set((s) => ({
      buildings: s.buildings.filter((b) => b.id !== id),
      activeBuildingId: s.activeBuildingId === id ? null : s.activeBuildingId,
    })),

  updateBuilding: (id, partial) =>
    set((s) => ({
      buildings: s.buildings.map((b) => (b.id === id ? { ...b, ...partial } : b)),
    })),

  setActiveBuilding: (id) =>
    set({ activeBuildingId: id, activeFloorIndex: 0 }),

  setActiveFloor: (index) =>
    set({ activeFloorIndex: index }),

  setInteriorTile: (layerIndex, localCol, localRow, gid) => {
    const { buildings, activeBuildingId, activeFloorIndex } = get();
    if (!activeBuildingId) return;
    const bIdx = buildings.findIndex((b) => b.id === activeBuildingId);
    if (bIdx < 0) return;
    const building = buildings[bIdx];
    const floor = building.floors[activeFloorIndex];
    if (!floor) return;
    if (localCol < 0 || localCol >= building.zoneW || localRow < 0 || localRow >= building.zoneH) return;
    const idx = localRow * building.zoneW + localCol;
    const oldStack = floor.layers[layerIndex]?.[idx] ?? [];
    const newStack = stackPush(oldStack, gid);
    if (oldStack === newStack) return;

    const newFloorLayers = floor.layers.map((l, i) =>
      i === layerIndex ? [...l.slice(0, idx), newStack, ...l.slice(idx + 1)] : l
    );
    const newFloor: BuildingFloor = { ...floor, layers: newFloorLayers };
    const newFloors = building.floors.map((f, i) => (i === activeFloorIndex ? newFloor : f));
    const newBuildings = buildings.map((b, i) =>
      i === bIdx ? { ...b, floors: newFloors } : b
    );
    set({ buildings: newBuildings });
  },

  setViewport: (offsetX, offsetY, zoom) =>
    set({ viewportOffsetX: offsetX, viewportOffsetY: offsetY, viewportZoom: zoom }),

  panViewport: (dx, dy) =>
    set((s) => ({
      viewportOffsetX: s.viewportOffsetX + dx,
      viewportOffsetY: s.viewportOffsetY + dy,
    })),

  zoomViewport: (delta, centerX, centerY) =>
    set((s) => {
      const oldZoom = s.viewportZoom;
      const newZoom = Math.max(0.5, Math.min(8, oldZoom + delta));
      const scale = newZoom / oldZoom;
      return {
        viewportZoom: newZoom,
        viewportOffsetX: centerX - (centerX - s.viewportOffsetX) * scale,
        viewportOffsetY: centerY - (centerY - s.viewportOffsetY) * scale,
      };
    }),

  setPanelWidth: (w) => set({ panelWidth: w }),
  setLayerH: (h) => set({ layerH: h }),
  setObjectH: (h) => set({ objectH: h }),
  setBuildingH: (h) => set({ buildingH: h }),
  setNpcH: (h) => set({ npcH: h }),
  setNpcCollapsed: (collapsed) => set({ npcCollapsed: collapsed }),

  undo: () => {
    const { _undoManager, layers, mapWidth } = get();
    const delta = _undoManager.undo(layers);
    if (!delta) return;

    const newLayers = layers.map((l) => [...l]);
    for (const change of delta.changes) {
      newLayers[delta.layerIndex][change.y * mapWidth + change.x] = [...change.oldValue];
    }
    set({ layers: newLayers });
  },

  redo: () => {
    const { _undoManager, layers, mapWidth } = get();
    const delta = _undoManager.redo();
    if (!delta) return;

    const newLayers = layers.map((l) => [...l]);
    for (const change of delta.changes) {
      newLayers[delta.layerIndex][change.y * mapWidth + change.x] = [...change.newValue];
    }
    set({ layers: newLayers });
  },

  beginStroke: () => {
    set({
      _pendingDelta: { layerIndex: get().activeLayerIndex, changes: [] },
    });
  },

  endStroke: () => {
    const { _pendingDelta, _undoManager } = get();
    if (_pendingDelta && _pendingDelta.changes.length > 0) {
      _undoManager.push(_pendingDelta);
    }
    set({ _pendingDelta: null });
  },

  newMap: (width, height) => {
    const layers = TILE_LAYER_NAMES.map(() =>
      Array.from({ length: width * height }, () => [] as number[])
    );
    set({
      mapWidth: width,
      mapHeight: height,
      layers,
      objects: [],
      buildings: [],
      activeBuildingId: null,
      activeFloorIndex: 0,
      layerVisibility: TILE_LAYER_NAMES.map(() => true),
      activeLayerIndex: 0,
    });
    get()._undoManager.clear();
  },

  loadMapData: (layers, objects, width, height) => {
    set({
      mapWidth: width,
      mapHeight: height,
      layers,
      objects,
      activeBuildingId: null,
      activeFloorIndex: 0,
      layerVisibility: layers.map(() => true),
      activeLayerIndex: 0,
    });
    get()._undoManager.clear();
  },
    }),
    {
      name: "seraph-editor-map",
      version: 2,
      partialize: (state) => ({
        mapWidth: state.mapWidth,
        mapHeight: state.mapHeight,
        layers: state.layers,
        layerNames: state.layerNames,
        objects: state.objects,
        buildings: state.buildings,
        activeLayerIndex: state.activeLayerIndex,
        layerVisibility: state.layerVisibility,
        activeTool: state.activeTool,
        showGrid: state.showGrid,
        showWalkability: state.showWalkability,
        showAnimations: state.showAnimations,
        viewportOffsetX: state.viewportOffsetX,
        viewportOffsetY: state.viewportOffsetY,
        viewportZoom: state.viewportZoom,
        panelWidth: state.panelWidth,
        layerH: state.layerH,
        objectH: state.objectH,
        buildingH: state.buildingH,
        npcH: state.npcH,
        npcCollapsed: state.npcCollapsed,
      }),
      migrate: (persisted: unknown, version: number) => {
        if (version < 2) {
          // Migrate from v1 (number[][]) to v2 (CellStack[][])
          const old = persisted as Record<string, unknown>;
          if (old.layers && Array.isArray(old.layers)) {
            old.layers = (old.layers as unknown[][]).map((layer) => {
              if (!Array.isArray(layer)) return layer;
              return layer.map((cell) => {
                if (Array.isArray(cell)) return cell; // already migrated
                const n = cell as number;
                return n > 0 ? [n] : [];
              });
            });
          }
        }
        return persisted as EditorStore;
      },
    }
  )
);
