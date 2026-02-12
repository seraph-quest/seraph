import { useRef, useEffect, useCallback } from "react";
import { useEditorStore } from "../stores/editorStore";
import { useTilesetStore } from "../stores/tilesetStore";
import { renderMap } from "../lib/canvas-renderer";
import { useCanvasInteraction } from "../hooks/useCanvasInteraction";
import { getSpriteBasePath } from "../lib/sprite-registry";
import { resolveTileGid, getTileSourceRect } from "../lib/tileset-loader";
import type { TiledTileLayer } from "../types/map";
import type { NPC } from "../types/editor";

export function MapCanvas() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const animFrameRef = useRef<number>(0);
  const { onMouseDown, onMouseMove, onMouseUp, onWheel } = useCanvasInteraction(canvasRef);

  const render = useCallback((timestamp?: DOMHighResTimeStamp) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const store = useEditorStore.getState();
    const tilesetStore = useTilesetStore.getState();
    const ts = timestamp ?? 0;

    // Resize canvas to match container
    const rect = canvas.parentElement!.getBoundingClientRect();
    if (canvas.width !== rect.width || canvas.height !== rect.height) {
      canvas.width = rect.width;
      canvas.height = rect.height;
    }

    // Build TiledTileLayer objects for renderer
    const tileLayers: TiledTileLayer[] = store.layers.map((data, i) => ({
      id: i + 1,
      name: store.layerNames[i],
      type: "tilelayer",
      width: store.mapWidth,
      height: store.mapHeight,
      x: 0,
      y: 0,
      data,
      opacity: 1,
      visible: true,
    }));

    renderMap(
      ctx,
      canvas.width,
      canvas.height,
      tileLayers,
      tilesetStore.tilesets,
      store.mapWidth,
      store.mapHeight,
      store.tileSize,
      {
        offsetX: store.viewportOffsetX,
        offsetY: store.viewportOffsetY,
        zoom: store.viewportZoom,
      },
      store.layerVisibility,
      store.activeLayerIndex,
      store.showGrid,
      store.showWalkability,
      tilesetStore.animationLookup,
      ts,
      store.showAnimations,
    );

    // Draw building zone overlays (world mode)
    if (!store.activeBuildingId) {
      drawBuildingZones(ctx, store);
    } else {
      // Interior-edit mode: draw mask over non-zone area and portal markers
      drawInteriorOverlay(ctx, store, tilesetStore);
    }

    // Draw objects on top
    drawObjects(ctx, store, tilesetStore.spriteImageCache, ts);

    animFrameRef.current = requestAnimationFrame(render);
  }, []);

  useEffect(() => {
    animFrameRef.current = requestAnimationFrame(render);
    return () => cancelAnimationFrame(animFrameRef.current);
  }, [render]);

  const activeTool = useEditorStore((s) => s.activeTool);
  const cursorClass =
    activeTool === "hand" ? "cursor-grab active:cursor-grabbing" :
    activeTool === "object" ? "cursor-default" :
    "cursor-crosshair";

  return (
    <div className="flex-1 relative overflow-hidden bg-gray-800">
      <canvas
        ref={canvasRef}
        className={`absolute inset-0 ${cursorClass}`}
        onMouseDown={onMouseDown}
        onMouseMove={onMouseMove}
        onMouseUp={onMouseUp}
        onMouseLeave={onMouseUp}
        onWheel={onWheel}
        onContextMenu={(e) => e.preventDefault()}
      />
      <div className="absolute bottom-2 right-2 text-xs text-gray-500 pointer-events-none select-none">
        {useEditorStore((s) => `${s.mapWidth}×${s.mapHeight} | Zoom: ${s.viewportZoom.toFixed(1)}x`)}
      </div>
    </div>
  );
}

function drawObjects(
  ctx: CanvasRenderingContext2D,
  store: ReturnType<typeof useEditorStore.getState>,
  spriteCache: Map<string, HTMLImageElement>,
  timestamp: number,
) {
  const { viewportOffsetX: ox, viewportOffsetY: oy, viewportZoom: zoom, tileSize } = store;
  const scale = tileSize * zoom;

  for (const obj of store.objects) {
    const x = ox + (obj.x / tileSize) * scale;
    const y = oy + (obj.y / tileSize) * scale;

    if (obj.type === "spawn_point") {
      // If a sprite is assigned, render it
      if (obj.spriteSheet) {
        const imgPath = `${getSpriteBasePath("character")}/${obj.spriteSheet}.png`;
        const img = spriteCache.get(imgPath);
        const frameW = 24;
        const frameH = 24;
        // Render full 24x24 frame at native zoom, anchored at tile bottom-left.
        // Offset left by 4px to center visible content on the tile.
        const destW = frameW * zoom;
        const destH = frameH * zoom;
        const spriteOffsetX = 4 * zoom;

        // Animate through first 4 columns (walk cycle) when showAnimations
        let srcCol = 0;
        const srcRow = 0;
        if (store.showAnimations) {
          srcCol = Math.floor(timestamp / 200) % 4;
        }

        if (img) {
          ctx.imageSmoothingEnabled = false;
          ctx.drawImage(img, srcCol * frameW, srcRow * frameH, frameW, frameH, x - spriteOffsetX, y + scale - destH, destW, destH);
          ctx.imageSmoothingEnabled = true;
        } else {
          // Fallback circle while loading
          ctx.fillStyle = "rgba(59, 130, 246, 0.4)";
          ctx.beginPath();
          ctx.arc(x + scale / 2, y + scale / 2, scale / 3, 0, Math.PI * 2);
          ctx.fill();
          useTilesetStore.getState().loadSpriteImage(imgPath).catch(() => {});
        }

        // Blue border around sprite
        ctx.strokeStyle = "#3b82f6";
        ctx.lineWidth = 2;
        ctx.strokeRect(x, y, scale, scale);
      } else {
        // No sprite assigned — default blue circle
        ctx.fillStyle = "rgba(59, 130, 246, 0.6)";
        ctx.beginPath();
        ctx.arc(x + scale / 2, y + scale / 2, scale / 3, 0, Math.PI * 2);
        ctx.fill();
        ctx.strokeStyle = "#3b82f6";
        ctx.lineWidth = 2;
        ctx.stroke();
      }

      ctx.fillStyle = "#fff";
      ctx.font = `${Math.max(8, 10 * zoom)}px sans-serif`;
      ctx.textAlign = "center";
      ctx.fillText(obj.name, x + scale / 2, y - 4);
    } else if (obj.type === "npc") {
      drawNPC(ctx, obj, x, y, scale, zoom, spriteCache, timestamp, store.showAnimations);
    }
  }
  ctx.textAlign = "left";
}

function drawNPC(
  ctx: CanvasRenderingContext2D,
  npc: NPC,
  x: number,
  y: number,
  scale: number,
  zoom: number,
  spriteCache: Map<string, HTMLImageElement>,
  timestamp: number,
  showAnimations: boolean,
) {
  const basePath = getSpriteBasePath(npc.spriteType);
  const imgPath = `${basePath}/${npc.spriteSheet}.png`;
  const img = spriteCache.get(imgPath);

  const frameW = 24;
  const frameH = 24;
  // Render full 24x24 frame at native zoom, anchored at tile bottom-left.
  // Offset left by 4px to center visible content on the tile.
  const destW = frameW * zoom;
  const destH = frameH * zoom;
  const spriteOffsetX = 4 * zoom;

  if (img) {
    let srcCol = npc.frameCol;
    const srcRow = npc.frameRow;

    // Animate by cycling through the character's walk frames
    if (showAnimations) {
      const animCols = npc.spriteType === "character" ? 4 : 3;
      const baseCol = npc.frameCol; // colOffset: 0, 4, 8, or 12 for characters; 0 for enemies
      srcCol = baseCol + Math.floor(timestamp / 200) % animCols;
    }

    const sx = srcCol * frameW;
    const sy = srcRow * frameH;

    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(img, sx, sy, frameW, frameH, x - spriteOffsetX, y + scale - destH, destW, destH);
    ctx.imageSmoothingEnabled = true;
  } else {
    // Fallback: purple rectangle while sprite loads
    ctx.fillStyle = "rgba(168, 85, 247, 0.4)";
    ctx.fillRect(x, y, scale, scale);

    // Kick off async load
    useTilesetStore.getState().loadSpriteImage(imgPath).catch(() => {});
  }

  // Purple border
  ctx.strokeStyle = "#a855f7";
  ctx.lineWidth = 2;
  ctx.strokeRect(x, y, scale, scale);

  // Label
  ctx.fillStyle = "#d8b4fe";
  ctx.font = `${Math.max(8, 10 * zoom)}px sans-serif`;
  ctx.textAlign = "center";
  ctx.fillText(npc.name, x + scale / 2, y - 4);
}

function drawBuildingZones(
  ctx: CanvasRenderingContext2D,
  store: ReturnType<typeof useEditorStore.getState>,
) {
  const { viewportOffsetX: ox, viewportOffsetY: oy, viewportZoom: zoom, tileSize, buildings } = store;
  const scaledTile = tileSize * zoom;

  for (const b of buildings) {
    const x = ox + b.zoneCol * scaledTile;
    const y = oy + b.zoneRow * scaledTile;
    const w = b.zoneW * scaledTile;
    const h = b.zoneH * scaledTile;

    // Semi-transparent fill
    ctx.fillStyle = "rgba(234, 179, 8, 0.08)";
    ctx.fillRect(x, y, w, h);

    // Border
    ctx.strokeStyle = "rgba(234, 179, 8, 0.6)";
    ctx.lineWidth = 2;
    ctx.setLineDash([4, 4]);
    ctx.strokeRect(x, y, w, h);
    ctx.setLineDash([]);

    // Label
    ctx.fillStyle = "rgba(234, 179, 8, 0.8)";
    ctx.font = `${Math.max(8, 10 * zoom)}px sans-serif`;
    ctx.textAlign = "left";
    ctx.fillText(b.name, x + 3, y - 3);
  }
  ctx.textAlign = "left";
}

function drawInteriorOverlay(
  ctx: CanvasRenderingContext2D,
  store: ReturnType<typeof useEditorStore.getState>,
  tilesetStore: ReturnType<typeof useTilesetStore.getState>,
) {
  const { viewportOffsetX: ox, viewportOffsetY: oy, viewportZoom: zoom, tileSize, buildings, activeBuildingId, activeFloorIndex, mapWidth, mapHeight } = store;
  const scaledTile = tileSize * zoom;

  const building = buildings.find((b) => b.id === activeBuildingId);
  if (!building) return;

  const floor = building.floors[activeFloorIndex];
  if (!floor) return;

  // Dim the area outside the building zone
  const zx = ox + building.zoneCol * scaledTile;
  const zy = oy + building.zoneRow * scaledTile;
  const zw = building.zoneW * scaledTile;
  const zh = building.zoneH * scaledTile;
  const totalW = mapWidth * scaledTile;
  const totalH = mapHeight * scaledTile;

  ctx.fillStyle = "rgba(0, 0, 0, 0.55)";
  // Top strip
  ctx.fillRect(ox, oy, totalW, building.zoneRow * scaledTile);
  // Bottom strip
  const bottomY = zy + zh;
  ctx.fillRect(ox, bottomY, totalW, oy + totalH - bottomY);
  // Left strip
  ctx.fillRect(ox, zy, building.zoneCol * scaledTile, zh);
  // Right strip
  const rightX = zx + zw;
  ctx.fillRect(rightX, zy, ox + totalW - rightX, zh);

  // Draw interior tiles over the zone
  for (let li = 0; li < floor.layers.length; li++) {
    if (!store.layerVisibility[li]) continue;
    const isActive = li === store.activeLayerIndex;
    ctx.globalAlpha = isActive ? 1.0 : 0.7;

    const layerData = floor.layers[li];
    for (let lr = 0; lr < building.zoneH; lr++) {
      for (let lc = 0; lc < building.zoneW; lc++) {
        const gid = layerData[lr * building.zoneW + lc];
        if (gid <= 0) continue;

        const resolved = resolveTileGid(gid, tilesetStore.tilesets);
        if (!resolved) continue;

        const { sx, sy, sw, sh } = getTileSourceRect(resolved.localId, resolved.tileset);
        const dx = ox + (building.zoneCol + lc) * scaledTile;
        const dy = oy + (building.zoneRow + lr) * scaledTile;
        ctx.drawImage(resolved.tileset.image, sx, sy, sw, sh, dx, dy, scaledTile, scaledTile);
      }
    }
  }
  ctx.globalAlpha = 1.0;

  // Draw portal markers
  for (const p of floor.portals) {
    const px = ox + (building.zoneCol + p.localCol) * scaledTile;
    const py = oy + (building.zoneRow + p.localRow) * scaledTile;

    ctx.fillStyle =
      p.kind === "entry" ? "rgba(34, 197, 94, 0.5)" :
      p.kind === "stairs_up" ? "rgba(59, 130, 246, 0.5)" :
      "rgba(249, 115, 22, 0.5)";
    ctx.fillRect(px, py, scaledTile, scaledTile);

    ctx.strokeStyle =
      p.kind === "entry" ? "#22c55e" :
      p.kind === "stairs_up" ? "#3b82f6" :
      "#f97316";
    ctx.lineWidth = 2;
    ctx.strokeRect(px, py, scaledTile, scaledTile);

    // Icon text
    ctx.fillStyle = "#fff";
    ctx.font = `${Math.max(8, 12 * zoom)}px sans-serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    const icon = p.kind === "entry" ? "D" : p.kind === "stairs_up" ? "\u2191" : "\u2193";
    ctx.fillText(icon, px + scaledTile / 2, py + scaledTile / 2);
    ctx.textBaseline = "alphabetic";
  }
  ctx.textAlign = "left";

  // Zone highlight border
  ctx.strokeStyle = "#eab308";
  ctx.lineWidth = 3;
  ctx.strokeRect(zx, zy, zw, zh);
}
