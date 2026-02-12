import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { LoadedTileset, TileSelection, TileAnimationGroup, AnimationLookup, RecentTileSelection } from "../types/editor";
import { loadAllTilesets, type TilesetCategory } from "../lib/tileset-loader";

interface TilesetStore {
  tilesets: LoadedTileset[];
  activeTilesetIndex: number;
  selectedTiles: TileSelection | null;
  loaded: boolean;
  loadError: string | null;
  loadProgress: { loaded: number; total: number; currentName: string };

  /** Active category filter (null = show all) */
  activeCategory: TilesetCategory | null;

  /** Cache of loaded sprite images keyed by path */
  spriteImageCache: Map<string, HTMLImageElement>;

  /** User-defined animation groups */
  animationGroups: TileAnimationGroup[];
  /** Pre-computed GID → animation frames lookup */
  animationLookup: AnimationLookup;
  /** Whether the animation definer panel is open */
  animDefinerOpen: boolean;

  /** Recently used tile selections (most recent first) */
  recentSelections: RecentTileSelection[];

  /** Persisted walkability data (parallel to tilesets[]) */
  tilesetWalkability: boolean[][];

  loadTilesets: (basePath: string) => Promise<void>;
  setActiveTileset: (index: number) => void;
  setSelectedTiles: (sel: TileSelection | null) => void;
  toggleWalkability: (tilesetIndex: number, localId: number) => void;
  setWalkability: (tilesetIndex: number, localId: number, walkable: boolean) => void;
  getSelectedGids: () => number[];
  setActiveCategory: (category: TilesetCategory | null) => void;
  loadSpriteImage: (path: string) => Promise<HTMLImageElement>;

  addAnimationGroup: (group: TileAnimationGroup) => void;
  updateAnimationGroup: (id: string, group: TileAnimationGroup) => void;
  removeAnimationGroup: (id: string) => void;
  setAnimDefinerOpen: (open: boolean) => void;
  setAnimationGroups: (groups: TileAnimationGroup[]) => void;
  addRecentSelection: () => void;
  _rebuildAnimationLookup: () => void;
}

export const useTilesetStore = create<TilesetStore>()(
  persist(
    (set, get) => ({
  tilesets: [],
  activeTilesetIndex: 0,
  selectedTiles: null,
  loaded: false,
  loadError: null,
  loadProgress: { loaded: 0, total: 0, currentName: "" },
  activeCategory: null,
  spriteImageCache: new Map(),
  animationGroups: [],
  animationLookup: new Map(),
  animDefinerOpen: false,
  recentSelections: [],
  tilesetWalkability: [],

  loadTilesets: async (basePath: string) => {
    try {
      const tilesets = await loadAllTilesets(basePath, (loaded, total, name) => {
        set({ loadProgress: { loaded, total, currentName: name } });
      });

      // Merge persisted walkability back into loaded tilesets
      const { tilesetWalkability } = get();
      if (tilesetWalkability.length > 0) {
        for (let i = 0; i < tilesets.length && i < tilesetWalkability.length; i++) {
          if (tilesetWalkability[i] && tilesetWalkability[i].length === tilesets[i].walkability.length) {
            tilesets[i] = { ...tilesets[i], walkability: [...tilesetWalkability[i]] };
          }
        }
      }

      set({ tilesets, loaded: true });
      get()._rebuildAnimationLookup();
    } catch (err) {
      set({ loadError: err instanceof Error ? err.message : String(err) });
    }
  },

  setActiveTileset: (index) => set({ activeTilesetIndex: index, selectedTiles: null }),

  setSelectedTiles: (sel) => set({ selectedTiles: sel }),

  toggleWalkability: (tilesetIndex, localId) => {
    const { tilesets, tilesetWalkability } = get();
    const ts = tilesets[tilesetIndex];
    if (!ts) return;
    const newWalkability = [...ts.walkability];
    newWalkability[localId] = !newWalkability[localId];
    const newTilesets = [...tilesets];
    newTilesets[tilesetIndex] = { ...ts, walkability: newWalkability };

    const newTsWalk = [...tilesetWalkability];
    newTsWalk[tilesetIndex] = newWalkability;
    set({ tilesets: newTilesets, tilesetWalkability: newTsWalk });
  },

  setWalkability: (tilesetIndex, localId, walkable) => {
    const { tilesets, tilesetWalkability } = get();
    const ts = tilesets[tilesetIndex];
    if (!ts) return;
    if (ts.walkability[localId] === walkable) return;
    const newWalkability = [...ts.walkability];
    newWalkability[localId] = walkable;
    const newTilesets = [...tilesets];
    newTilesets[tilesetIndex] = { ...ts, walkability: newWalkability };

    const newTsWalk = [...tilesetWalkability];
    newTsWalk[tilesetIndex] = newWalkability;
    set({ tilesets: newTilesets, tilesetWalkability: newTsWalk });
  },

  getSelectedGids: () => {
    const { tilesets, activeTilesetIndex, selectedTiles } = get();
    if (!selectedTiles) return [];
    const ts = tilesets[activeTilesetIndex];
    if (!ts) return [];

    const gids: number[] = [];
    const minCol = Math.min(selectedTiles.startCol, selectedTiles.endCol);
    const maxCol = Math.max(selectedTiles.startCol, selectedTiles.endCol);
    const minRow = Math.min(selectedTiles.startRow, selectedTiles.endRow);
    const maxRow = Math.max(selectedTiles.startRow, selectedTiles.endRow);

    for (let r = minRow; r <= maxRow; r++) {
      for (let c = minCol; c <= maxCol; c++) {
        const localId = r * ts.columns + c;
        gids.push(ts.firstGid + localId);
      }
    }
    return gids;
  },

  setActiveCategory: (category) => {
    const { tilesets } = get();
    const firstIndex = category === null
      ? 0
      : tilesets.findIndex((ts) => ts.category === category);
    set({
      activeCategory: category,
      activeTilesetIndex: firstIndex >= 0 ? firstIndex : 0,
      selectedTiles: null,
    });
  },

  loadSpriteImage: async (path: string) => {
    const { spriteImageCache } = get();
    const cached = spriteImageCache.get(path);
    if (cached) return cached;

    const img = new Image();
    img.src = path;
    await new Promise<void>((resolve, reject) => {
      img.onload = () => resolve();
      img.onerror = () => reject(new Error(`Failed to load sprite: ${path}`));
    });

    const newCache = new Map(get().spriteImageCache);
    newCache.set(path, img);
    set({ spriteImageCache: newCache });
    return img;
  },

  addAnimationGroup: (group) => {
    set((s) => ({ animationGroups: [...s.animationGroups, group] }));
    get()._rebuildAnimationLookup();
  },

  updateAnimationGroup: (id, group) => {
    set((s) => ({
      animationGroups: s.animationGroups.map((g) => (g.id === id ? group : g)),
    }));
    get()._rebuildAnimationLookup();
  },

  removeAnimationGroup: (id) => {
    set((s) => ({
      animationGroups: s.animationGroups.filter((g) => g.id !== id),
    }));
    get()._rebuildAnimationLookup();
  },

  setAnimDefinerOpen: (open) => set({ animDefinerOpen: open }),

  addRecentSelection: () => {
    const { activeTilesetIndex, selectedTiles, recentSelections } = get();
    if (!selectedTiles) return;
    const entry: RecentTileSelection = {
      tilesetIndex: activeTilesetIndex,
      selection: { ...selectedTiles },
      usedAt: Date.now(),
    };
    // Deduplicate: remove existing entry with same tileset + selection
    const filtered = recentSelections.filter(
      (r) =>
        r.tilesetIndex !== entry.tilesetIndex ||
        r.selection.startCol !== entry.selection.startCol ||
        r.selection.startRow !== entry.selection.startRow ||
        r.selection.endCol !== entry.selection.endCol ||
        r.selection.endRow !== entry.selection.endRow
    );
    set({ recentSelections: [entry, ...filtered].slice(0, 20) });
  },

  setAnimationGroups: (groups) => {
    set({ animationGroups: groups });
    get()._rebuildAnimationLookup();
  },

  _rebuildAnimationLookup: () => {
    const { animationGroups, tilesets } = get();
    const lookup: AnimationLookup = new Map();

    for (const group of animationGroups) {
      const ts = tilesets[group.tilesetIndex];
      if (!ts) continue;

      for (const entry of group.entries) {
        const anchorGid = ts.firstGid + entry.anchorLocalId;
        const frames = entry.frames.map((localId) => ({
          gid: ts.firstGid + localId,
          duration: group.frameDuration,
        }));
        const totalDuration = frames.length * group.frameDuration;
        if (frames.length > 0) {
          lookup.set(anchorGid, { frames, totalDuration });
        }
      }
    }

    set({ animationLookup: lookup });
  },
    }),
    {
      name: "seraph-editor-tileset",
      version: 1,
      partialize: (state) => ({
        animationGroups: state.animationGroups,
        activeTilesetIndex: state.activeTilesetIndex,
        recentSelections: state.recentSelections,
        tilesetWalkability: state.tilesetWalkability,
      }),
    }
  )
);
