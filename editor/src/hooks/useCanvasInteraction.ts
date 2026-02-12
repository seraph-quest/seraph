import { useCallback, useRef, useEffect } from "react";
import { useEditorStore } from "../stores/editorStore";
import { useTilesetStore } from "../stores/tilesetStore";

export function useCanvasInteraction(canvasRef: React.RefObject<HTMLCanvasElement | null>) {
  const isPainting = useRef(false);
  const isPanning = useRef(false);
  const lastPan = useRef({ x: 0, y: 0 });
  const spaceDown = useRef(false);

  // Drag state for object tool
  const isDragging = useRef(false);
  const dragObjIndex = useRef(-1);
  const dragOffset = useRef({ x: 0, y: 0 });

  const screenToTile = useCallback(
    (clientX: number, clientY: number) => {
      const canvas = canvasRef.current;
      if (!canvas) return null;
      const rect = canvas.getBoundingClientRect();
      const { viewportOffsetX, viewportOffsetY, viewportZoom, tileSize, mapWidth, mapHeight } =
        useEditorStore.getState();
      const x = clientX - rect.left;
      const y = clientY - rect.top;
      const scaledTile = tileSize * viewportZoom;
      const col = Math.floor((x - viewportOffsetX) / scaledTile);
      const row = Math.floor((y - viewportOffsetY) / scaledTile);
      if (col < 0 || col >= mapWidth || row < 0 || row >= mapHeight) return null;
      return { col, row };
    },
    [canvasRef]
  );

  const screenToWorld = useCallback(
    (clientX: number, clientY: number) => {
      const canvas = canvasRef.current;
      if (!canvas) return null;
      const rect = canvas.getBoundingClientRect();
      const { viewportOffsetX, viewportOffsetY, viewportZoom } =
        useEditorStore.getState();
      const sx = clientX - rect.left;
      const sy = clientY - rect.top;
      const wx = (sx - viewportOffsetX) / viewportZoom;
      const wy = (sy - viewportOffsetY) / viewportZoom;
      return { wx, wy };
    },
    [canvasRef]
  );

  /** Hit-test objects in reverse order (topmost first). Returns index + offset for smooth drag. */
  const hitTestObject = useCallback(
    (clientX: number, clientY: number) => {
      const world = screenToWorld(clientX, clientY);
      if (!world) return null;
      const { wx, wy } = world;
      const { objects, tileSize } = useEditorStore.getState();

      for (let i = objects.length - 1; i >= 0; i--) {
        const obj = objects[i];
        if (wx >= obj.x && wx <= obj.x + tileSize && wy >= obj.y && wy <= obj.y + tileSize) {
          return { index: i, offsetX: wx - obj.x, offsetY: wy - obj.y };
        }
      }
      return null;
    },
    [screenToWorld]
  );

  const paintAt = useCallback(
    (col: number, row: number) => {
      const store = useEditorStore.getState();
      const tilesetStore = useTilesetStore.getState();
      const { activeTool, activeLayerIndex } = store;

      if (activeTool === "brush") {
        const gids = tilesetStore.getSelectedGids();
        if (gids.length === 0) return;

        if (gids.length === 1) {
          store.setTile(activeLayerIndex, col, row, gids[0]);
        } else {
          const sel = tilesetStore.selectedTiles;
          if (!sel) return;
          const selW = Math.abs(sel.endCol - sel.startCol) + 1;
          store.paintMultiTile(activeLayerIndex, col, row, gids, selW);
        }
      } else if (activeTool === "eraser") {
        store.setTile(activeLayerIndex, col, row, 0);
      } else if (activeTool === "fill") {
        const gids = tilesetStore.getSelectedGids();
        if (gids.length !== 1) return;
        store.fillTiles(activeLayerIndex, col, row, gids[0]);
      } else if (activeTool === "walkability") {
        // Toggle walkability for whatever tile is at this position
        const { layers } = store;
        for (let li = layers.length - 1; li >= 0; li--) {
          const gid = layers[li][row * store.mapWidth + col];
          if (gid > 0) {
            const tilesets = tilesetStore.tilesets;
            for (let ti = tilesets.length - 1; ti >= 0; ti--) {
              if (gid >= tilesets[ti].firstGid) {
                const localId = gid - tilesets[ti].firstGid;
                tilesetStore.toggleWalkability(ti, localId);
                return;
              }
            }
          }
        }
      }
    },
    []
  );

  const onMouseDown = useCallback(
    (e: React.MouseEvent) => {
      const { activeTool } = useEditorStore.getState();

      if (e.button === 1 || spaceDown.current || activeTool === "hand") {
        isPanning.current = true;
        lastPan.current = { x: e.clientX, y: e.clientY };
        return;
      }

      if (e.button !== 0) return;

      if (activeTool === "object") {
        const hit = hitTestObject(e.clientX, e.clientY);
        if (hit) {
          isDragging.current = true;
          dragObjIndex.current = hit.index;
          dragOffset.current = { x: hit.offsetX, y: hit.offsetY };
        }
        return;
      }

      const tile = screenToTile(e.clientX, e.clientY);
      if (!tile) return;

      isPainting.current = true;
      useEditorStore.getState().beginStroke();
      paintAt(tile.col, tile.row);
    },
    [screenToTile, paintAt, hitTestObject]
  );

  const onMouseMove = useCallback(
    (e: React.MouseEvent) => {
      if (isPanning.current) {
        const dx = e.clientX - lastPan.current.x;
        const dy = e.clientY - lastPan.current.y;
        useEditorStore.getState().panViewport(dx, dy);
        lastPan.current = { x: e.clientX, y: e.clientY };
        return;
      }

      if (isDragging.current) {
        const canvas = canvasRef.current;
        if (canvas) canvas.style.cursor = "grabbing";
        const world = screenToWorld(e.clientX, e.clientY);
        if (!world) return;
        const store = useEditorStore.getState();
        const { tileSize } = store;
        const obj = store.objects[dragObjIndex.current];
        if (!obj) return;
        const rawX = world.wx - dragOffset.current.x;
        const rawY = world.wy - dragOffset.current.y;
        const x = Math.round(rawX / tileSize) * tileSize;
        const y = Math.round(rawY / tileSize) * tileSize;
        store.updateObject(dragObjIndex.current, { ...obj, x, y });
        return;
      }

      // Update cursor for object tool hover
      const { activeTool } = useEditorStore.getState();
      if (activeTool === "object") {
        const canvas = canvasRef.current;
        if (canvas) {
          const hit = hitTestObject(e.clientX, e.clientY);
          canvas.style.cursor = hit ? "grab" : "default";
        }
        return;
      }

      if (!isPainting.current) return;
      const tile = screenToTile(e.clientX, e.clientY);
      if (!tile) return;

      // Only continuous paint for brush and eraser
      if (activeTool === "brush" || activeTool === "eraser") {
        paintAt(tile.col, tile.row);
      }
    },
    [canvasRef, screenToTile, screenToWorld, hitTestObject, paintAt]
  );

  const onMouseUp = useCallback(() => {
    if (isPanning.current) {
      isPanning.current = false;
      return;
    }
    if (isDragging.current) {
      isDragging.current = false;
      dragObjIndex.current = -1;
      const canvas = canvasRef.current;
      if (canvas) canvas.style.cursor = "";
      return;
    }
    if (isPainting.current) {
      isPainting.current = false;
      useEditorStore.getState().endStroke();
      useTilesetStore.getState().addRecentSelection();
    }
  }, []);

  const onWheel = useCallback(
    (e: React.WheelEvent) => {
      e.preventDefault();
      const canvas = canvasRef.current;
      if (!canvas) return;
      const rect = canvas.getBoundingClientRect();
      const centerX = e.clientX - rect.left;
      const centerY = e.clientY - rect.top;
      const delta = e.deltaY > 0 ? -0.25 : 0.25;
      useEditorStore.getState().zoomViewport(delta, centerX, centerY);
    },
    [canvasRef]
  );

  // Track spacebar for pan mode
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.code === "Space") {
        e.preventDefault();
        spaceDown.current = true;
      }
    };
    const handleKeyUp = (e: KeyboardEvent) => {
      if (e.code === "Space") {
        spaceDown.current = false;
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    window.addEventListener("keyup", handleKeyUp);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
      window.removeEventListener("keyup", handleKeyUp);
    };
  }, []);

  return { onMouseDown, onMouseMove, onMouseUp, onWheel };
}
