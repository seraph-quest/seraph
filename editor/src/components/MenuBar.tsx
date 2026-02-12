import { useCallback } from "react";
import { useEditorStore } from "../stores/editorStore";
import { useTilesetStore } from "../stores/tilesetStore";
import { Tooltip } from "./Tooltip";
import { serializeMap, saveMapToFile, parseMapFromJson } from "../lib/map-io";

export function MenuBar() {
  const store = useEditorStore();

  const handleNew = useCallback(() => {
    if (!confirm("Create a new map? Unsaved changes will be lost.")) return;
    store.newMap(64, 40);
  }, [store]);

  const handleSave = useCallback(() => {
    const { layers, layerNames, mapWidth, mapHeight, tileSize, objects, buildings } = useEditorStore.getState();
    const { tilesets, animationGroups } = useTilesetStore.getState();
    const map = serializeMap(layers, layerNames, mapWidth, mapHeight, tileSize, tilesets, objects, animationGroups, buildings);
    saveMapToFile(map, "village.json");
  }, []);

  const handleLoad = useCallback(() => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = ".json";
    input.onchange = async (e) => {
      const file = (e.target as HTMLInputElement).files?.[0];
      if (!file) return;
      const text = await file.text();
      const parsed = parseMapFromJson(text);
      if (parsed) {
        const editorState = useEditorStore.getState();
        editorState.loadMapData(
          parsed.layers,
          parsed.objects,
          parsed.width,
          parsed.height
        );
        if (parsed.buildings.length > 0) {
          useEditorStore.setState({ buildings: parsed.buildings });
        }
        if (parsed.animationGroups.length > 0) {
          useTilesetStore.getState().setAnimationGroups(parsed.animationGroups);
        }
      } else {
        alert("Failed to parse map file.");
      }
    };
    input.click();
  }, []);

  const handleSaveToDisk = useCallback(async () => {
    const { layers, layerNames, mapWidth, mapHeight, tileSize, objects, buildings } = useEditorStore.getState();
    const { tilesets, animationGroups } = useTilesetStore.getState();
    const map = serializeMap(layers, layerNames, mapWidth, mapHeight, tileSize, tilesets, objects, animationGroups, buildings);
    const json = JSON.stringify(map, null, 2);

    // Try to save directly to frontend/public/maps/village.json via download
    const blob = new Blob([json], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "village.json";
    a.click();
    URL.revokeObjectURL(url);
  }, []);

  return (
    <div className="flex items-center gap-1 px-2 h-8 bg-gray-900 border-b border-gray-700 text-xs">
      <span className="text-yellow-400 font-bold mr-2" style={{ fontFamily: '"Press Start 2P"', fontSize: "10px" }}>
        Village Editor
      </span>
      <div className="w-px h-4 bg-gray-700" />
      <Tooltip text="New Map" desc="Create a blank 64x40 map. Unsaved changes will be lost." side="bottom">
        <button onClick={handleNew} className="px-2 py-0.5 hover:bg-gray-700 rounded text-gray-300">
          New
        </button>
      </Tooltip>
      <Tooltip text="Load Map" desc="Open a Tiled JSON file (.json) from disk." side="bottom">
        <button onClick={handleLoad} className="px-2 py-0.5 hover:bg-gray-700 rounded text-gray-300">
          Load
        </button>
      </Tooltip>
      <Tooltip text="Save" desc="Download the map as a Tiled JSON file." side="bottom">
        <button onClick={handleSave} className="px-2 py-0.5 hover:bg-gray-700 rounded text-gray-300">
          Save
        </button>
      </Tooltip>
      <Tooltip text="Export" desc="Download as village.json. Copy it to frontend/public/maps/ for the game to use." side="bottom">
        <button
          onClick={handleSaveToDisk}
          className="px-2 py-0.5 hover:bg-gray-700 rounded text-yellow-400"
        >
          Export village.json
        </button>
      </Tooltip>
      <div className="flex-1" />
      {useEditorStore((s) => {
        if (!s.activeBuildingId) return null;
        const building = s.buildings.find((b) => b.id === s.activeBuildingId);
        if (!building) return null;
        const floor = building.floors[s.activeFloorIndex];
        return (
          <span className="text-yellow-300 text-[10px] mr-2 bg-yellow-900/40 px-2 py-0.5 rounded">
            Editing: {building.name} / {floor?.name ?? `Floor ${s.activeFloorIndex}`}
          </span>
        );
      })}
      <span className="text-gray-600 text-[10px]">
        {store.mapWidth}x{store.mapHeight} | Ctrl+Z: Undo | Ctrl+Shift+Z: Redo
      </span>
    </div>
  );
}
