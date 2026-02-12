import { useState, useEffect, useRef } from "react";
import { useEditorStore } from "../stores/editorStore";
import { useTilesetStore } from "../stores/tilesetStore";
import { getTileSourceRect } from "../lib/tileset-loader";
import { Tooltip } from "./Tooltip";
import { getCharacters, getSpriteBasePath } from "../lib/sprite-registry";
import type { MapObject, SpawnPoint } from "../types/editor";
import type { TileAnimationGroup, LoadedTileset } from "../types/editor";

type PanelTab = "placed" | "catalog";

export function ObjectPanel() {
  const { objects, addObject, removeObject, updateObject } = useEditorStore();
  const [activeTab, setActiveTab] = useState<PanelTab>("placed");
  const [spritePicker, setSpritePicker] = useState<number | null>(null);

  const addSpawnPoint = (name: string) => {
    const obj: SpawnPoint = { name, type: "spawn_point", x: 256, y: 256 };
    addObject(obj);
    setActiveTab("placed");
  };

  const assignSprite = (objIndex: number, spriteSheet: string) => {
    const obj = objects[objIndex];
    if (obj.type === "spawn_point") {
      updateObject(objIndex, { ...obj, spriteSheet });
    }
    setSpritePicker(null);
  };

  const clearSprite = (objIndex: number) => {
    const obj = objects[objIndex];
    if (obj.type === "spawn_point") {
      const { spriteSheet: _, ...rest } = obj;
      updateObject(objIndex, rest as SpawnPoint);
    }
  };

  return (
    <div className="flex flex-col h-full">
      <div className="px-2 py-1 text-xs font-bold text-gray-300 border-b border-gray-700 flex items-center justify-between">
        <Tooltip text="Objects" desc="Game-logic entities: spawn points, NPCs, and magic effects." side="left">
          <span>Objects</span>
        </Tooltip>
        <span className="text-[10px] text-gray-500">
          {objects.length > 0 && `${objects.length} placed`}
        </span>
      </div>

      {/* Tabs */}
      <div className="flex border-b border-gray-700 flex-shrink-0">
        <button
          onClick={() => setActiveTab("placed")}
          className={`flex-1 py-1 text-[10px] ${
            activeTab === "placed"
              ? "bg-gray-700 text-gray-200 border-b-2 border-yellow-500"
              : "text-gray-500 hover:text-gray-300 hover:bg-gray-700/50"
          }`}
        >
          Placed ({objects.length})
        </button>
        <button
          onClick={() => setActiveTab("catalog")}
          className={`flex-1 py-1 text-[10px] ${
            activeTab === "catalog"
              ? "bg-gray-700 text-gray-200 border-b-2 border-green-500"
              : "text-gray-500 hover:text-gray-300 hover:bg-gray-700/50"
          }`}
        >
          Catalog
        </button>
      </div>

      {/* Placed objects list */}
      {activeTab === "placed" && (
        <div className="flex-1 overflow-auto">
          {objects.length === 0 && (
            <div className="p-2 text-[10px] text-gray-600">
              No objects placed. Switch to <button onClick={() => setActiveTab("catalog")} className="text-green-400 underline">Catalog</button> to add.
            </div>
          )}
          {objects.map((obj, i) => (
            <div key={i}>
              <div className="flex items-center gap-1 px-2 py-1 text-[10px] hover:bg-gray-700/50">
                <span
                  className={`w-2 h-2 rounded-full flex-shrink-0 ${
                    obj.type === "spawn_point"
                      ? "bg-blue-500"
                      : "bg-purple-500"
                  }`}
                />
                <span className="flex-1 text-gray-300 truncate">
                  {obj.name}
                  {obj.type === "npc" && (
                    <span className="text-gray-500 ml-1">({obj.spriteType})</span>
                  )}
                  {obj.type === "spawn_point" && obj.spriteSheet && (
                    <span className="text-blue-400 ml-1">[{obj.spriteSheet.replace("Character_", "C")}]</span>
                  )}
                </span>
                {obj.type === "spawn_point" && (
                  <Tooltip
                    text="Sprite"
                    desc={obj.spriteSheet ? `Assigned: ${obj.spriteSheet}. Click to change.` : "Click to assign a character sprite."}
                    side="left"
                  >
                    <button
                      onClick={() => setSpritePicker(spritePicker === i ? null : i)}
                      className={`text-[9px] px-1 rounded ${
                        obj.spriteSheet
                          ? "bg-blue-700 text-blue-200"
                          : "bg-gray-600 text-gray-400 hover:bg-gray-500"
                      }`}
                    >
                      {obj.spriteSheet ? "..." : "sprite"}
                    </button>
                  </Tooltip>
                )}
                <span className="text-gray-600 flex-shrink-0">
                  {Math.round(obj.x)},{Math.round(obj.y)}
                </span>
                <button
                  onClick={() => { removeObject(i); if (spritePicker === i) setSpritePicker(null); }}
                  className="text-red-400 hover:text-red-300 ml-1 flex-shrink-0"
                >
                  x
                </button>
              </div>

              {/* Inline sprite picker for spawn points */}
              {spritePicker === i && obj.type === "spawn_point" && (
                <SpritePicker
                  current={obj.spriteSheet}
                  onSelect={(sheet) => assignSprite(i, sheet)}
                  onClear={() => clearSprite(i)}
                />
              )}
            </div>
          ))}
        </div>
      )}

      {/* Catalog — templates to add */}
      {activeTab === "catalog" && (
        <div className="flex-1 overflow-auto p-2 space-y-2">
          <MagicEffectsCatalog />
          <div>
            <div className="text-[10px] text-gray-400 mb-1 font-semibold">Spawn Points</div>
            <div className="space-y-0.5">
              <button
                onClick={() => addSpawnPoint("agent_spawn")}
                className="block w-full text-left text-[10px] px-1.5 py-0.5 bg-gray-700 hover:bg-gray-600 rounded text-blue-300"
              >
                Agent Spawn Point (Seraph)
              </button>
              <button
                onClick={() => addSpawnPoint("user_spawn")}
                className="block w-full text-left text-[10px] px-1.5 py-0.5 bg-gray-700 hover:bg-gray-600 rounded text-blue-300"
              >
                User Spawn Point
              </button>
            </div>
          </div>
          <div className="text-[9px] text-gray-600 pt-1 border-t border-gray-700">
            NPCs (characters & enemies) can be added from the NPCs panel below.
          </div>
        </div>
      )}
    </div>
  );
}

function MagicEffectsCatalog() {
  const animationGroups = useTilesetStore((s) => s.animationGroups);
  const tilesets = useTilesetStore((s) => s.tilesets);

  const fxGroups = animationGroups.filter((g) => g.isMagicEffect);

  return (
    <div>
      <div className="text-[10px] text-gray-400 mb-1 font-semibold">Magic Effects</div>
      {fxGroups.length === 0 ? (
        <div className="text-[9px] text-gray-600 px-1 py-1">
          Mark animations as "Magic Effect" in the Animation Definer to see them here.
        </div>
      ) : (
        <div className="space-y-1">
          {fxGroups.map((group) => {
            const tileset = tilesets[group.tilesetIndex];
            if (!tileset) return null;
            return (
              <MagicEffectRow key={group.id} group={group} tileset={tileset} />
            );
          })}
        </div>
      )}
    </div>
  );
}

function MagicEffectRow({
  group,
  tileset,
}: {
  group: TileAnimationGroup;
  tileset: LoadedTileset;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const animRef = useRef<number>(0);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || group.entries.length === 0) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const entry = group.entries[0];
    const size = 32;
    canvas.width = size;
    canvas.height = size;

    const animate = (t: DOMHighResTimeStamp) => {
      const totalDuration = entry.frames.length * group.frameDuration;
      const elapsed = t % totalDuration;
      let accum = 0;
      let frameLocalId = entry.frames[0];
      for (const fId of entry.frames) {
        accum += group.frameDuration;
        if (elapsed < accum) {
          frameLocalId = fId;
          break;
        }
      }

      const { sx, sy, sw, sh } = getTileSourceRect(frameLocalId, tileset);
      ctx.imageSmoothingEnabled = false;
      ctx.clearRect(0, 0, size, size);
      ctx.drawImage(tileset.image, sx, sy, sw, sh, 0, 0, size, size);
      animRef.current = requestAnimationFrame(animate);
    };
    animRef.current = requestAnimationFrame(animate);
    return () => cancelAnimationFrame(animRef.current);
  }, [group, tileset]);

  const frameCount = group.entries[0]?.frames.length ?? 0;

  return (
    <div className="flex items-center gap-2 px-1 py-0.5 bg-gray-700 rounded">
      <canvas
        ref={canvasRef}
        className="flex-shrink-0"
        style={{ imageRendering: "pixelated", width: 32, height: 32 }}
      />
      <div className="flex-1 min-w-0">
        <div className="text-[10px] text-gray-200 truncate">
          {group.name}
          <span className="ml-1 px-1 py-0 text-[8px] bg-fuchsia-700 text-fuchsia-200 rounded">FX</span>
        </div>
        <div className="text-[9px] text-gray-500">
          {frameCount} frames, {group.frameDuration}ms &middot; {tileset.name}
        </div>
      </div>
    </div>
  );
}

function SpritePicker({
  current,
  onSelect,
  onClear,
}: {
  current?: string;
  onSelect: (sheet: string) => void;
  onClear: () => void;
}) {
  const characters = getCharacters();

  return (
    <div className="px-2 pb-2 border-b border-gray-700">
      <div className="flex items-center justify-between mb-1">
        <span className="text-[9px] text-gray-500">Pick character sprite:</span>
        {current && (
          <button
            onClick={onClear}
            className="text-[9px] text-red-400 hover:text-red-300"
          >
            clear
          </button>
        )}
      </div>
      <div className="grid grid-cols-6 gap-0.5 max-h-32 overflow-auto">
        {characters.map((c) => (
          <SpriteMiniThumb
            key={c.name}
            name={c.name}
            file={c.file}
            frameW={c.frameWidth}
            frameH={c.frameHeight}
            selected={current === c.name}
            onClick={() => onSelect(c.name)}
          />
        ))}
      </div>
    </div>
  );
}

function SpriteMiniThumb({
  name,
  file,
  frameW,
  frameH,
  selected,
  onClick,
}: {
  name: string;
  file: string;
  frameW: number;
  frameH: number;
  selected: boolean;
  onClick: () => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  const basePath = getSpriteBasePath("character");
  const imgPath = `${basePath}/${file}`;

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    useTilesetStore.getState().loadSpriteImage(imgPath).then((img) => {
      const ctx = canvas.getContext("2d");
      if (!ctx) return;
      const size = 32;
      canvas.width = size;
      canvas.height = size;
      ctx.imageSmoothingEnabled = false;
      ctx.clearRect(0, 0, size, size);
      ctx.drawImage(img, 0, 0, frameW, frameH, 0, 0, size, size);
    }).catch(() => {});
  }, [imgPath, frameW, frameH]);

  return (
    <Tooltip text={name} desc="Click to assign this character sprite." side="top">
      <button
        onClick={onClick}
        className={`p-0.5 rounded ${
          selected
            ? "ring-2 ring-blue-400 bg-blue-900/50"
            : "hover:bg-gray-600"
        }`}
      >
        <canvas
          ref={canvasRef}
          width={32}
          height={32}
          className="w-8 h-8"
          style={{ imageRendering: "pixelated" }}
        />
      </button>
    </Tooltip>
  );
}
