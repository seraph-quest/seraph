import { useState } from "react";
import { useEditorStore } from "../stores/editorStore";
import { Tooltip } from "./Tooltip";
import type { BuildingDef, BuildingFloor, BuildingPortal } from "../types/editor";

const TILE_LAYER_COUNT = 5;

function createEmptyFloor(name: string, w: number, h: number): BuildingFloor {
  return {
    name,
    layers: Array.from({ length: TILE_LAYER_COUNT }, () => new Array(w * h).fill(0)),
    portals: [],
  };
}

export function BuildingPanel() {
  const buildings = useEditorStore((s) => s.buildings);
  const activeBuildingId = useEditorStore((s) => s.activeBuildingId);
  const activeFloorIndex = useEditorStore((s) => s.activeFloorIndex);

  if (activeBuildingId) {
    const building = buildings.find((b) => b.id === activeBuildingId);
    if (!building) return null;
    return (
      <InteriorEditor
        building={building}
        activeFloorIndex={activeFloorIndex}
      />
    );
  }

  return <BuildingList />;
}

function BuildingList() {
  const buildings = useEditorStore((s) => s.buildings);
  const addBuilding = useEditorStore((s) => s.addBuilding);
  const removeBuilding = useEditorStore((s) => s.removeBuilding);
  const setActiveBuilding = useEditorStore((s) => s.setActiveBuilding);
  const [isDrawing, setIsDrawing] = useState(false);

  const handleAdd = () => {
    const name = prompt("Building name:");
    if (!name) return;
    setIsDrawing(true);
    // Store the pending building name — the zone-draw handler in useCanvasInteraction
    // will read this and call addBuilding with the drawn rectangle
    useEditorStore.setState({
      _pendingBuildingName: name,
    } as never);
  };

  const handleQuickAdd = () => {
    const name = prompt("Building name:");
    if (!name) return;
    const colStr = prompt("Zone top-left column:", "10");
    const rowStr = prompt("Zone top-left row:", "10");
    const wStr = prompt("Zone width (tiles):", "6");
    const hStr = prompt("Zone height (tiles):", "6");
    if (!colStr || !rowStr || !wStr || !hStr) return;
    const zoneCol = parseInt(colStr, 10);
    const zoneRow = parseInt(rowStr, 10);
    const zoneW = parseInt(wStr, 10);
    const zoneH = parseInt(hStr, 10);
    if ([zoneCol, zoneRow, zoneW, zoneH].some(isNaN) || zoneW < 2 || zoneH < 2) {
      alert("Invalid zone dimensions.");
      return;
    }

    const building: BuildingDef = {
      id: crypto.randomUUID(),
      name,
      zoneCol,
      zoneRow,
      zoneW,
      zoneH,
      floors: [createEmptyFloor("Ground Floor", zoneW, zoneH)],
    };
    addBuilding(building);
  };

  return (
    <div className="flex flex-col h-full">
      <div className="px-2 py-1 text-xs font-bold text-gray-300 border-b border-gray-700 flex items-center justify-between">
        <Tooltip text="Buildings" desc="Define building zones with interior floors and portals." side="left">
          <span>Buildings</span>
        </Tooltip>
        <span className="text-[10px] text-gray-500">
          {buildings.length > 0 && `${buildings.length} defined`}
        </span>
      </div>

      <div className="flex-1 overflow-auto">
        {buildings.length === 0 && (
          <div className="p-2 text-[10px] text-gray-600">
            No buildings defined. Click "Add" to create one.
          </div>
        )}
        {buildings.map((b) => (
          <div
            key={b.id}
            className="flex items-center gap-1 px-2 py-1 text-[10px] hover:bg-gray-700/50 group"
          >
            <span className="w-2 h-2 rounded-sm flex-shrink-0 bg-yellow-500" />
            <span className="flex-1 text-gray-300 truncate">
              {b.name}
              <span className="text-gray-500 ml-1">
                ({b.zoneW}x{b.zoneH}, {b.floors.length}F)
              </span>
            </span>
            <button
              onClick={() => setActiveBuilding(b.id)}
              className="text-[9px] px-1 rounded bg-yellow-700 text-yellow-200 hover:bg-yellow-600"
            >
              Edit
            </button>
            <button
              onClick={() => {
                if (confirm(`Delete building "${b.name}"?`)) removeBuilding(b.id);
              }}
              className="text-red-400 hover:text-red-300 ml-0.5 flex-shrink-0 opacity-0 group-hover:opacity-100"
            >
              x
            </button>
          </div>
        ))}
      </div>

      <div className="p-1.5 border-t border-gray-700 space-y-1">
        <button
          onClick={handleQuickAdd}
          className="w-full text-[10px] px-2 py-1 bg-yellow-700 hover:bg-yellow-600 rounded text-yellow-100"
        >
          + Add Building
        </button>
      </div>
    </div>
  );
}

function InteriorEditor({
  building,
  activeFloorIndex,
}: {
  building: BuildingDef;
  activeFloorIndex: number;
}) {
  const setActiveBuilding = useEditorStore((s) => s.setActiveBuilding);
  const setActiveFloor = useEditorStore((s) => s.setActiveFloor);
  const updateBuilding = useEditorStore((s) => s.updateBuilding);

  const floor = building.floors[activeFloorIndex];

  const addFloor = () => {
    const name = prompt("Floor name:", `Floor ${building.floors.length + 1}`);
    if (!name) return;
    const newFloor = createEmptyFloor(name, building.zoneW, building.zoneH);
    updateBuilding(building.id, {
      floors: [...building.floors, newFloor],
    });
    setActiveFloor(building.floors.length);
  };

  const removeFloor = (index: number) => {
    if (building.floors.length <= 1) return;
    if (!confirm(`Delete floor "${building.floors[index].name}"?`)) return;
    const newFloors = building.floors.filter((_, i) => i !== index);
    updateBuilding(building.id, { floors: newFloors });
    if (activeFloorIndex >= newFloors.length) {
      setActiveFloor(newFloors.length - 1);
    }
  };

  const addPortal = () => {
    if (!floor) return;
    const kindStr = prompt("Portal kind (entry / stairs_up / stairs_down):", "entry");
    if (!kindStr || !["entry", "stairs_up", "stairs_down"].includes(kindStr)) {
      alert("Invalid kind. Use: entry, stairs_up, or stairs_down");
      return;
    }
    const colStr = prompt("Local column (0-based):", "0");
    const rowStr = prompt("Local row (0-based):", String(building.zoneH - 1));
    if (!colStr || !rowStr) return;
    const localCol = parseInt(colStr, 10);
    const localRow = parseInt(rowStr, 10);
    if (isNaN(localCol) || isNaN(localRow)) return;

    const portal: BuildingPortal = {
      localCol,
      localRow,
      kind: kindStr as BuildingPortal["kind"],
    };
    const newFloor: BuildingFloor = {
      ...floor,
      portals: [...floor.portals, portal],
    };
    const newFloors = building.floors.map((f, i) => (i === activeFloorIndex ? newFloor : f));
    updateBuilding(building.id, { floors: newFloors });
  };

  const removePortal = (portalIndex: number) => {
    if (!floor) return;
    const newFloor: BuildingFloor = {
      ...floor,
      portals: floor.portals.filter((_, i) => i !== portalIndex),
    };
    const newFloors = building.floors.map((f, i) => (i === activeFloorIndex ? newFloor : f));
    updateBuilding(building.id, { floors: newFloors });
  };

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="px-2 py-1 text-xs font-bold text-yellow-300 border-b border-gray-700 flex items-center justify-between bg-yellow-900/20">
        <span className="truncate">{building.name}</span>
        <button
          onClick={() => setActiveBuilding(null)}
          className="text-[9px] px-1.5 py-0.5 rounded bg-gray-600 text-gray-300 hover:bg-gray-500"
        >
          Back
        </button>
      </div>

      {/* Floor tabs */}
      <div className="flex flex-wrap gap-0.5 px-1.5 py-1 border-b border-gray-700 bg-gray-800/50">
        {building.floors.map((f, i) => (
          <div key={i} className="flex items-center">
            <button
              onClick={() => setActiveFloor(i)}
              className={`text-[10px] px-1.5 py-0.5 rounded-l ${
                i === activeFloorIndex
                  ? "bg-yellow-600 text-white"
                  : "bg-gray-700 text-gray-400 hover:bg-gray-600"
              }`}
            >
              {f.name}
            </button>
            {building.floors.length > 1 && (
              <button
                onClick={() => removeFloor(i)}
                className="text-[9px] px-0.5 py-0.5 rounded-r bg-gray-700 text-red-400 hover:bg-red-800 hover:text-red-200"
              >
                x
              </button>
            )}
          </div>
        ))}
        <button
          onClick={addFloor}
          className="text-[10px] px-1.5 py-0.5 rounded bg-gray-700 text-green-400 hover:bg-gray-600"
        >
          +
        </button>
      </div>

      {/* Zone info */}
      <div className="px-2 py-1 text-[9px] text-gray-500 border-b border-gray-700">
        Zone: ({building.zoneCol},{building.zoneRow}) {building.zoneW}x{building.zoneH} tiles
      </div>

      {/* Portals */}
      <div className="flex-1 overflow-auto">
        <div className="px-2 py-1 text-[10px] text-gray-400 font-semibold flex items-center justify-between">
          <span>Portals</span>
          <button
            onClick={addPortal}
            className="text-[9px] px-1 rounded bg-gray-700 text-blue-300 hover:bg-gray-600"
          >
            + Add
          </button>
        </div>
        {floor && floor.portals.length === 0 && (
          <div className="px-2 text-[9px] text-gray-600">
            No portals on this floor.
          </div>
        )}
        {floor?.portals.map((p, i) => (
          <div key={i} className="flex items-center gap-1 px-2 py-0.5 text-[10px] hover:bg-gray-700/50">
            <span className={`w-2 h-2 rounded-full flex-shrink-0 ${
              p.kind === "entry" ? "bg-green-500" :
              p.kind === "stairs_up" ? "bg-blue-400" :
              "bg-orange-400"
            }`} />
            <span className="flex-1 text-gray-300">
              {p.kind} ({p.localCol},{p.localRow})
            </span>
            <button
              onClick={() => removePortal(i)}
              className="text-red-400 hover:text-red-300"
            >
              x
            </button>
          </div>
        ))}
      </div>

      <div className="px-2 py-1 text-[9px] text-gray-600 border-t border-gray-700">
        Use brush/eraser tools to paint interior tiles for this floor.
      </div>
    </div>
  );
}
