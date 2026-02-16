import type { CSSProperties, PointerEventHandler } from "react";
import type { ResizeEdge } from "../hooks/useDragResize";

interface ResizeHandlesProps {
  resizeHandleProps: (edge: ResizeEdge) => {
    onPointerDown: PointerEventHandler;
    style: CSSProperties;
  };
}

const EDGE_SIZE = 6;

const edgeStyles: Record<ResizeEdge, CSSProperties> = {
  n: { position: "absolute", top: -EDGE_SIZE / 2, left: EDGE_SIZE, right: EDGE_SIZE, height: EDGE_SIZE },
  s: { position: "absolute", bottom: -EDGE_SIZE / 2, left: EDGE_SIZE, right: EDGE_SIZE, height: EDGE_SIZE },
  e: { position: "absolute", right: -EDGE_SIZE / 2, top: EDGE_SIZE, bottom: EDGE_SIZE, width: EDGE_SIZE },
  w: { position: "absolute", left: -EDGE_SIZE / 2, top: EDGE_SIZE, bottom: EDGE_SIZE, width: EDGE_SIZE },
  nw: { position: "absolute", top: -EDGE_SIZE / 2, left: -EDGE_SIZE / 2, width: EDGE_SIZE * 2, height: EDGE_SIZE * 2 },
  ne: { position: "absolute", top: -EDGE_SIZE / 2, right: -EDGE_SIZE / 2, width: EDGE_SIZE * 2, height: EDGE_SIZE * 2 },
  sw: { position: "absolute", bottom: -EDGE_SIZE / 2, left: -EDGE_SIZE / 2, width: EDGE_SIZE * 2, height: EDGE_SIZE * 2 },
  se: { position: "absolute", bottom: -EDGE_SIZE / 2, right: -EDGE_SIZE / 2, width: EDGE_SIZE * 2, height: EDGE_SIZE * 2 },
};

const corners: ResizeEdge[] = ["nw", "ne", "sw", "se"];
const edges: ResizeEdge[] = ["n", "s", "e", "w"];

const cornerDotPositions: Record<string, CSSProperties> = {
  nw: { position: "absolute", top: -3, left: -3 },
  ne: { position: "absolute", top: -3, right: -3 },
  sw: { position: "absolute", bottom: -3, left: -3 },
  se: { position: "absolute", bottom: -3, right: -3 },
};

export function ResizeHandles({ resizeHandleProps }: ResizeHandlesProps) {
  return (
    <>
      {edges.map((edge) => {
        const props = resizeHandleProps(edge);
        return (
          <div
            key={edge}
            onPointerDown={props.onPointerDown}
            style={{ ...edgeStyles[edge], ...props.style, zIndex: 11 }}
          />
        );
      })}
      {corners.map((corner) => {
        const props = resizeHandleProps(corner);
        return (
          <div
            key={corner}
            className="group"
            onPointerDown={props.onPointerDown}
            style={{ ...edgeStyles[corner], ...props.style, zIndex: 20 }}
          >
            <div
              className="opacity-0 group-hover:opacity-100 transition-opacity"
              style={{
                ...cornerDotPositions[corner],
                width: 6,
                height: 6,
                background: "#e2b714",
                pointerEvents: "none",
              }}
            />
          </div>
        );
      })}
    </>
  );
}
