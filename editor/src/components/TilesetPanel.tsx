import { useRef, useEffect, useCallback, useState } from "react";
import { useTilesetStore } from "../stores/tilesetStore";
import { useEditorStore } from "../stores/editorStore";
import { Tooltip } from "./Tooltip";
import { renderTilesetPreview } from "../lib/canvas-renderer";
import { CATEGORIES, getTilesetConfigs } from "../lib/tileset-loader";
import { AnimationDefiner } from "./AnimationDefiner";
import type { TileAnimationGroup } from "../types/editor";

const TILESET_SCALE = 2;

/** Build a hint lookup from the tileset configs */
const TILESET_HINTS: Record<string, string> = Object.fromEntries(
  getTilesetConfigs().map((c) => [c.name, c.hint])
);

export function TilesetPanel() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const { tilesets, activeTilesetIndex, selectedTiles, activeCategory, animDefinerOpen, animationGroups } = useTilesetStore();
  const showWalkability = useEditorStore((s) => s.showWalkability);
  const activeTool = useEditorStore((s) => s.activeTool);
  const isDragging = useRef(false);
  const [dragStart, setDragStart] = useState<{ col: number; row: number } | null>(null);

  const activeTileset = tilesets[activeTilesetIndex];

  // Animation groups for the current tileset
  const currentAnimGroups = animationGroups.filter(
    (g) => g.tilesetIndex === activeTilesetIndex
  );

  const selectAnimAnchor = useCallback(
    (group: TileAnimationGroup) => {
      const store = useTilesetStore.getState();
      // Switch tileset if needed
      if (group.tilesetIndex !== store.activeTilesetIndex) {
        store.setActiveTileset(group.tilesetIndex);
      }
      const ts = store.tilesets[group.tilesetIndex];
      if (!ts || group.entries.length === 0) return;

      if (group.entries.length === 1) {
        const localId = group.entries[0].anchorLocalId;
        const col = localId % ts.columns;
        const row = Math.floor(localId / ts.columns);
        store.setSelectedTiles({ startCol: col, startRow: row, endCol: col, endRow: row });
      } else {
        // Bounding box of all anchor tiles
        let minCol = Infinity, maxCol = -Infinity, minRow = Infinity, maxRow = -Infinity;
        for (const entry of group.entries) {
          const col = entry.anchorLocalId % ts.columns;
          const row = Math.floor(entry.anchorLocalId / ts.columns);
          if (col < minCol) minCol = col;
          if (col > maxCol) maxCol = col;
          if (row < minRow) minRow = row;
          if (row > maxRow) maxRow = row;
        }
        store.setSelectedTiles({ startCol: minCol, startRow: minRow, endCol: maxCol, endRow: maxRow });
      }
    },
    []
  );

  // Filter tilesets by active category
  const filteredTilesets = activeCategory
    ? tilesets.map((ts, i) => ({ ts, i })).filter(({ ts }) => ts.category === activeCategory)
    : tilesets.map((ts, i) => ({ ts, i }));

  const renderPreview = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas || !activeTileset) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    canvas.width = activeTileset.imageWidth * TILESET_SCALE;
    canvas.height = activeTileset.imageHeight * TILESET_SCALE;

    renderTilesetPreview(ctx, activeTileset, TILESET_SCALE, selectedTiles, showWalkability);
  }, [activeTileset, selectedTiles, showWalkability]);

  useEffect(() => {
    renderPreview();
  }, [renderPreview]);

  const getTileCoord = useCallback(
    (e: React.MouseEvent) => {
      if (!activeTileset || !canvasRef.current) return null;
      const rect = canvasRef.current.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      const col = Math.floor(x / (activeTileset.tileWidth * TILESET_SCALE));
      const row = Math.floor(y / (activeTileset.tileHeight * TILESET_SCALE));
      if (col < 0 || col >= activeTileset.columns || row < 0 || row >= activeTileset.rows) return null;
      return { col, row };
    },
    [activeTileset]
  );

  const onMouseDown = useCallback(
    (e: React.MouseEvent) => {
      const coord = getTileCoord(e);
      if (!coord) return;

      if (activeTool === "walkability" && activeTileset) {
        const localId = coord.row * activeTileset.columns + coord.col;
        useTilesetStore.getState().toggleWalkability(activeTilesetIndex, localId);
        return;
      }

      isDragging.current = true;
      setDragStart(coord);
      useTilesetStore.getState().setSelectedTiles({
        startCol: coord.col,
        startRow: coord.row,
        endCol: coord.col,
        endRow: coord.row,
      });
    },
    [getTileCoord, activeTool, activeTileset, activeTilesetIndex]
  );

  const onMouseMove = useCallback(
    (e: React.MouseEvent) => {
      if (!isDragging.current || !dragStart) return;
      const coord = getTileCoord(e);
      if (!coord) return;
      useTilesetStore.getState().setSelectedTiles({
        startCol: dragStart.col,
        startRow: dragStart.row,
        endCol: coord.col,
        endRow: coord.row,
      });
    },
    [getTileCoord, dragStart]
  );

  const onMouseUp = useCallback(() => {
    isDragging.current = false;
  }, []);

  if (!activeTileset) {
    return <div className="p-2 text-gray-500 text-xs">Loading tilesets...</div>;
  }

  return (
    <div className="flex flex-col h-full">
      <div className="px-2 py-1 text-xs font-bold text-gray-300 border-b border-gray-700">
        <Tooltip text="Tileset" desc="Click a tile to select it, or drag to select a multi-tile region. Then paint on the map with the Brush tool." side="left">
          <span>Tileset</span>
        </Tooltip>
      </div>

      {/* Category filter row */}
      <div className="flex gap-1 px-1 py-1 border-b border-gray-700">
        <Tooltip text="All" desc="Show all tilesets and animation sheets." side="bottom">
          <button
            onClick={() => useTilesetStore.getState().setActiveCategory(null)}
            className={`px-1.5 py-0.5 text-[10px] rounded ${
              activeCategory === null
                ? "bg-blue-600 text-white"
                : "bg-gray-700 text-gray-400 hover:bg-gray-600"
            }`}
          >
            All
          </button>
        </Tooltip>
        {CATEGORIES.map((cat) => (
          <Tooltip key={cat.key} text={cat.label} desc={`Show only ${cat.label.toLowerCase()} tilesets.`} side="bottom">
            <button
              onClick={() => useTilesetStore.getState().setActiveCategory(cat.key)}
              className={`px-1.5 py-0.5 text-[10px] rounded ${
                activeCategory === cat.key
                  ? "bg-blue-600 text-white"
                  : "bg-gray-700 text-gray-400 hover:bg-gray-600"
              }`}
            >
              {cat.label}
            </button>
          </Tooltip>
        ))}
      </div>

      {/* Tileset tabs */}
      <div className="flex flex-wrap gap-1 p-1 border-b border-gray-700 max-h-20 overflow-auto">
        {filteredTilesets.map(({ ts, i }) => {
          const label = ts.name.replace("CuteRPG_", "");
          const isAnim = ts.category === "animations";
          return (
            <Tooltip key={ts.name} text={label} desc={TILESET_HINTS[ts.name]} side="bottom">
              <button
                onClick={() => useTilesetStore.getState().setActiveTileset(i)}
                className={`px-1.5 py-0.5 text-[10px] rounded ${
                  i === activeTilesetIndex
                    ? "bg-yellow-600 text-white"
                    : "bg-gray-700 text-gray-400 hover:bg-gray-600"
                }`}
              >
                {label}{isAnim ? " (anim)" : ""}
              </button>
            </Tooltip>
          );
        })}
      </div>

      {/* Animation definer button — available for any tileset */}
      {!animDefinerOpen && (
        <div className="px-1 py-1 border-b border-gray-700">
          <button
            onClick={() => useTilesetStore.getState().setAnimDefinerOpen(true)}
            className="w-full py-0.5 text-[10px] bg-purple-700 hover:bg-purple-600 text-white rounded"
          >
            Define Animations
          </button>
        </div>
      )}

      {/* Animation quick-select list */}
      {!animDefinerOpen && currentAnimGroups.length > 0 && (
        <div className="border-b border-gray-700 px-1 py-1">
          <div className="text-[9px] text-gray-500 mb-0.5">Animations:</div>
          <div className="flex flex-wrap gap-1">
            {currentAnimGroups.map((group) => (
              <button
                key={group.id}
                onClick={() => selectAnimAnchor(group)}
                className="px-1.5 py-0.5 text-[10px] rounded bg-purple-800 text-purple-200 hover:bg-purple-600"
              >
                {group.name}
                {group.isMagicEffect && (
                  <span className="ml-1 text-[8px] text-fuchsia-300">FX</span>
                )}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Show AnimationDefiner or tileset canvas */}
      {animDefinerOpen ? (
        <div className="flex-1 min-h-0"><AnimationDefiner /></div>
      ) : (
        <>
          <div className="flex-1 overflow-auto p-1">
            <canvas
              ref={canvasRef}
              className="cursor-pointer"
              style={{ imageRendering: "pixelated" }}
              onMouseDown={onMouseDown}
              onMouseMove={onMouseMove}
              onMouseUp={onMouseUp}
              onMouseLeave={onMouseUp}
            />
          </div>

          {selectedTiles && (
            <div className="px-2 py-1 text-[10px] text-gray-400 border-t border-gray-700">
              Selected: ({Math.abs(selectedTiles.endCol - selectedTiles.startCol) + 1}x
              {Math.abs(selectedTiles.endRow - selectedTiles.startRow) + 1})
            </div>
          )}
        </>
      )}
    </div>
  );
}
