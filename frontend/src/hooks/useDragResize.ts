import { useCallback, useEffect, useRef, type CSSProperties, type PointerEventHandler } from "react";
import { usePanelLayoutStore, PANEL_MIN_SIZES } from "../stores/panelLayoutStore";

export type ResizeEdge = "n" | "s" | "e" | "w" | "ne" | "nw" | "se" | "sw";

const VISIBLE_MIN = 80;

function clampRect(
  x: number,
  y: number,
  width: number,
  height: number,
  minW: number,
  minH: number,
) {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const w = Math.max(width, minW);
  const h = Math.max(height, minH);
  const cx = Math.max(-(w - VISIBLE_MIN), Math.min(x, vw - VISIBLE_MIN));
  // Top: keep drag bar on screen (y >= 0); bottom/sides: keep 80px visible
  const cy = Math.max(0, Math.min(y, vh - VISIBLE_MIN));
  return { x: cx, y: cy, width: w, height: h };
}

const CURSOR_MAP: Record<ResizeEdge, string> = {
  n: "n-resize",
  s: "s-resize",
  e: "e-resize",
  w: "w-resize",
  ne: "ne-resize",
  nw: "nw-resize",
  se: "se-resize",
  sw: "sw-resize",
};

export function useDragResize(
  panelId: string,
  opts: { minWidth: number; minHeight: number },
) {
  const { setRect, bringToFront, getZIndex, panels } = usePanelLayoutStore();
  const rect = panels[panelId];
  const minW = opts.minWidth ?? PANEL_MIN_SIZES[panelId]?.width ?? 200;
  const minH = opts.minHeight ?? PANEL_MIN_SIZES[panelId]?.height ?? 200;

  const dragState = useRef<{ startX: number; startY: number; origX: number; origY: number } | null>(null);
  const resizeState = useRef<{
    edge: ResizeEdge;
    startX: number;
    startY: number;
    origX: number;
    origY: number;
    origW: number;
    origH: number;
  } | null>(null);

  const onDragPointerDown: PointerEventHandler = useCallback(
    (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      (e.target as HTMLElement).setPointerCapture(e.pointerId);
      bringToFront(panelId);
      const r = panels[panelId];
      dragState.current = { startX: e.clientX, startY: e.clientY, origX: r.x, origY: r.y };
    },
    [panelId, bringToFront, panels],
  );

  const onResizePointerDown = useCallback(
    (edge: ResizeEdge): PointerEventHandler =>
      (e) => {
        if (e.button !== 0) return;
        e.preventDefault();
        e.stopPropagation();
        (e.target as HTMLElement).setPointerCapture(e.pointerId);
        bringToFront(panelId);
        const r = panels[panelId];
        resizeState.current = {
          edge,
          startX: e.clientX,
          startY: e.clientY,
          origX: r.x,
          origY: r.y,
          origW: r.width,
          origH: r.height,
        };
      },
    [panelId, bringToFront, panels],
  );

  useEffect(() => {
    const onMove = (e: PointerEvent) => {
      if (dragState.current) {
        const dx = e.clientX - dragState.current.startX;
        const dy = e.clientY - dragState.current.startY;
        const clamped = clampRect(
          dragState.current.origX + dx,
          dragState.current.origY + dy,
          rect.width,
          rect.height,
          minW,
          minH,
        );
        setRect(panelId, { x: clamped.x, y: clamped.y });
        return;
      }

      if (resizeState.current) {
        const s = resizeState.current;
        const dx = e.clientX - s.startX;
        const dy = e.clientY - s.startY;

        let newX = s.origX;
        let newY = s.origY;
        let newW = s.origW;
        let newH = s.origH;

        if (s.edge.includes("e")) newW = s.origW + dx;
        if (s.edge.includes("w")) { newW = s.origW - dx; newX = s.origX + dx; }
        if (s.edge.includes("s")) newH = s.origH + dy;
        if (s.edge.includes("n")) { newH = s.origH - dy; newY = s.origY + dy; }

        // Enforce min sizes before clamping — adjust position if shrinking past min
        if (newW < minW) {
          if (s.edge.includes("w")) newX = s.origX + s.origW - minW;
          newW = minW;
        }
        if (newH < minH) {
          if (s.edge.includes("n")) newY = s.origY + s.origH - minH;
          newH = minH;
        }

        const clamped = clampRect(newX, newY, newW, newH, minW, minH);
        setRect(panelId, clamped);
      }
    };

    const onUp = () => {
      dragState.current = null;
      resizeState.current = null;
    };

    document.addEventListener("pointermove", onMove);
    document.addEventListener("pointerup", onUp);
    return () => {
      document.removeEventListener("pointermove", onMove);
      document.removeEventListener("pointerup", onUp);
    };
  }, [panelId, setRect, rect, minW, minH]);

  // Reclamp on window resize
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>;
    const onResize = () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        const r = usePanelLayoutStore.getState().panels[panelId];
        if (!r) return;
        const clamped = clampRect(r.x, r.y, r.width, r.height, minW, minH);
        setRect(panelId, clamped);
      }, 150);
    };
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      clearTimeout(timer);
    };
  }, [panelId, setRect, minW, minH]);

  const style: CSSProperties = {
    position: "fixed",
    left: rect.x,
    top: rect.y,
    width: rect.width,
    height: rect.height,
    zIndex: getZIndex(panelId),
  };

  const dragHandleProps = { onPointerDown: onDragPointerDown };

  const resizeHandleProps = (edge: ResizeEdge) => ({
    onPointerDown: onResizePointerDown(edge),
    style: { cursor: CURSOR_MAP[edge] } as CSSProperties,
  });

  return { dragHandleProps, resizeHandleProps, style, bringToFront: () => bringToFront(panelId) };
}
