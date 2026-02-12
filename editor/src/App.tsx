import { useEffect, useCallback, useRef } from "react";
import { useEditorStore } from "./stores/editorStore";
import { useTilesetStore } from "./stores/tilesetStore";
import { useKeyboardShortcuts } from "./hooks/useKeyboardShortcuts";
import { MenuBar } from "./components/MenuBar";
import { ToolBar } from "./components/ToolBar";
import { MapCanvas } from "./components/MapCanvas";
import { TilesetPanel } from "./components/TilesetPanel";
import { LayerPanel } from "./components/LayerPanel";
import { ObjectPanel } from "./components/ObjectPanel";
import { NPCBrowser } from "./components/NPCBrowser";
import { BuildingPanel } from "./components/BuildingPanel";

function LoadingScreen() {
  const { loaded, total, currentName } = useTilesetStore((s) => s.loadProgress);
  const loadError = useTilesetStore((s) => s.loadError);
  const pct = total > 0 ? Math.round((loaded / total) * 100) : 0;

  return (
    <div className="w-full h-full flex items-center justify-center bg-gray-900">
      <div className="w-80 space-y-3">
        <p className="text-gray-400 text-sm text-center">Loading tilesets...</p>
        <div className="h-3 bg-gray-700 rounded overflow-hidden">
          <div
            className="h-full bg-blue-500 transition-all duration-150"
            style={{ width: `${pct}%` }}
          />
        </div>
        <p className="text-gray-500 text-xs text-center truncate">
          {loadError
            ? ""
            : loaded < total
              ? `${loaded + 1} / ${total} — ${currentName}`
              : "Finishing up..."}
        </p>
        {loadError && (
          <p className="text-red-400 text-xs text-center mt-2">{loadError}</p>
        )}
      </div>
    </div>
  );
}

const MIN_PANEL_W = 200;
const MAX_PANEL_W = 800;
const DEFAULT_PANEL_W = 320;

const MIN_SECTION_H = 40;

/** Draggable horizontal divider between right-panel sections */
function HDivider({ onDrag }: { onDrag: (dy: number) => void }) {
  const onMouseDown = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      let lastY = e.clientY;

      const onMove = (ev: MouseEvent) => {
        const dy = ev.clientY - lastY;
        lastY = ev.clientY;
        onDrag(dy);
      };

      const onUp = () => {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
      };

      document.body.style.cursor = "row-resize";
      document.body.style.userSelect = "none";
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    },
    [onDrag]
  );

  return (
    <div
      className="h-1.5 bg-gray-700 hover:bg-blue-500 cursor-row-resize flex-shrink-0 transition-colors"
      onMouseDown={onMouseDown}
    />
  );
}

export default function App() {
  const loaded = useTilesetStore((s) => s.loaded);
  const panelWidth = useEditorStore((s) => s.panelWidth);
  const setPanelWidth = useEditorStore((s) => s.setPanelWidth);
  const layerH = useEditorStore((s) => s.layerH);
  const setLayerH = useEditorStore((s) => s.setLayerH);
  const objectH = useEditorStore((s) => s.objectH);
  const setObjectH = useEditorStore((s) => s.setObjectH);
  const buildingH = useEditorStore((s) => s.buildingH);
  const setBuildingH = useEditorStore((s) => s.setBuildingH);
  const npcH = useEditorStore((s) => s.npcH);
  const setNpcH = useEditorStore((s) => s.setNpcH);
  const npcCollapsed = useEditorStore((s) => s.npcCollapsed);
  const setNpcCollapsed = useEditorStore((s) => s.setNpcCollapsed);
  const dragging = useRef(false);

  useKeyboardShortcuts();

  useEffect(() => {
    useTilesetStore.getState().loadTilesets("/assets");
  }, []);

  const onDividerDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    dragging.current = true;
    const startX = e.clientX;
    const startW = panelWidth;

    const onMove = (ev: MouseEvent) => {
      if (!dragging.current) return;
      const delta = startX - ev.clientX;
      const newW = Math.min(MAX_PANEL_W, Math.max(MIN_PANEL_W, startW + delta));
      setPanelWidth(newW);
    };

    const onUp = () => {
      dragging.current = false;
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };

    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  }, [panelWidth, setPanelWidth]);

  if (!loaded) {
    return <LoadingScreen />;
  }

  return (
    <div className="flex flex-col h-screen">
      <MenuBar />
      <div className="flex flex-1 overflow-hidden">
        {/* Left: toolbar */}
        <div className="bg-gray-800 border-r border-gray-700">
          <ToolBar />
        </div>

        {/* Center: map canvas */}
        <MapCanvas />

        {/* Draggable vertical divider */}
        <div
          className="w-1.5 bg-gray-700 hover:bg-blue-500 cursor-col-resize flex-shrink-0 transition-colors"
          onMouseDown={onDividerDown}
        />

        {/* Right: panels with resizable sections */}
        <div
          className="bg-gray-800 flex flex-col overflow-hidden flex-shrink-0"
          style={{ width: panelWidth }}
        >
          {/* Layers section */}
          <div className="overflow-auto flex-shrink-0" style={{ height: layerH }}>
            <LayerPanel />
          </div>

          <HDivider onDrag={(dy) => {
            const h = useEditorStore.getState().layerH;
            setLayerH(Math.max(MIN_SECTION_H, h + dy));
          }} />

          {/* Objects section */}
          <div className="overflow-auto flex-shrink-0" style={{ height: objectH }}>
            <ObjectPanel />
          </div>

          <HDivider onDrag={(dy) => {
            const h = useEditorStore.getState().objectH;
            setObjectH(Math.max(MIN_SECTION_H, h + dy));
          }} />

          {/* Buildings section */}
          <div className="overflow-auto flex-shrink-0" style={{ height: buildingH }}>
            <BuildingPanel />
          </div>

          <HDivider onDrag={(dy) => {
            const h = useEditorStore.getState().buildingH;
            setBuildingH(Math.max(MIN_SECTION_H, h + dy));
          }} />

          {/* NPCs section */}
          <div
            className="overflow-hidden flex-shrink-0"
            style={{ height: npcCollapsed ? "auto" : npcH }}
          >
            <NPCBrowser
              collapsed={npcCollapsed}
              onToggle={() => setNpcCollapsed(!npcCollapsed)}
            />
          </div>

          {!npcCollapsed && (
            <HDivider onDrag={(dy) => {
              const h = useEditorStore.getState().npcH;
              setNpcH(Math.max(MIN_SECTION_H, h + dy));
            }} />
          )}

          {/* Tileset section — takes remaining space */}
          <div className="flex-1 overflow-hidden min-h-0">
            <TilesetPanel />
          </div>
        </div>
      </div>
    </div>
  );
}
