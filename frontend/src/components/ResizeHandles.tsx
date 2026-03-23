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
            onPointerDown={props.onPointerDown}
            style={{ ...edgeStyles[corner], ...props.style, zIndex: 20 }}
          >
            {corner === "se" ? <div className="cockpit-window-corner-grip" /> : null}
          </div>
        );
      })}
    </>
  );
}
