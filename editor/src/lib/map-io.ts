import type { TiledMap, TiledTileLayer, TiledObjectLayer, TiledTilesetRef, TiledTileDef } from "../types/map";
import type { LoadedTileset, MapObject, TileAnimationGroup, TileAnimationEntry } from "../types/editor";

const TILED_VERSION = "1.10.2";
const MAP_VERSION = "1.10";

export function createEmptyMap(
  width: number,
  height: number,
  tileSize: number,
  tilesets: LoadedTileset[],
): TiledMap {
  const tileLayerNames = ["ground", "terrain", "buildings", "decorations", "treetops"];

  const layers: (TiledTileLayer | TiledObjectLayer)[] = tileLayerNames.map((name, i) => ({
    id: i + 1,
    name,
    type: "tilelayer" as const,
    width,
    height,
    x: 0,
    y: 0,
    data: new Array(width * height).fill(0),
    opacity: 1,
    visible: true,
  }));

  // Object layer
  layers.push({
    id: tileLayerNames.length + 1,
    name: "objects",
    type: "objectgroup",
    x: 0,
    y: 0,
    objects: [],
    opacity: 1,
    visible: true,
  });

  const tilesetRefs: TiledTilesetRef[] = tilesets.map((ts) => ({
    firstgid: ts.firstGid,
    name: ts.name,
    tilewidth: ts.tileWidth,
    tileheight: ts.tileHeight,
    tilecount: ts.tileCount,
    columns: ts.columns,
    image: tilesetImagePath(ts),
    imagewidth: ts.imageWidth,
    imageheight: ts.imageHeight,
    tiles: ts.walkability
      .map((walkable, id) =>
        walkable
          ? null
          : {
              id,
              properties: [{ name: "walkable", type: "bool" as const, value: false }],
            }
      )
      .filter((t): t is NonNullable<typeof t> => t !== null),
  }));

  return {
    width,
    height,
    tilewidth: tileSize,
    tileheight: tileSize,
    orientation: "orthogonal",
    renderorder: "right-down",
    tiledversion: TILED_VERSION,
    version: MAP_VERSION,
    type: "map",
    layers,
    tilesets: tilesetRefs,
  };
}

/** Resolve the relative image path for a tileset based on its category */
function tilesetImagePath(ts: LoadedTileset): string {
  const dir = ts.category === "animations" ? "animations" : "tilesets";
  return `../assets/${dir}/${ts.name}.png`;
}

export function serializeMap(
  layers: number[][],
  layerNames: string[],
  mapWidth: number,
  mapHeight: number,
  tileSize: number,
  tilesets: LoadedTileset[],
  objects: MapObject[],
  animationGroups?: TileAnimationGroup[],
): TiledMap {
  const tileLayers: TiledTileLayer[] = layerNames.map((name, i) => ({
    id: i + 1,
    name,
    type: "tilelayer",
    width: mapWidth,
    height: mapHeight,
    x: 0,
    y: 0,
    data: [...layers[i]],
    opacity: 1,
    visible: true,
  }));

  const objectLayer: TiledObjectLayer = {
    id: layerNames.length + 1,
    name: "objects",
    type: "objectgroup",
    x: 0,
    y: 0,
    objects: objects.map((obj, i) => {
      const base = {
        id: i + 1,
        name: obj.name,
        type: obj.type,
        x: obj.x,
        y: obj.y,
        width: 16,
        height: 16,
        visible: true,
      };

      if (obj.type === "spawn_point" && obj.spriteSheet) {
        return {
          ...base,
          properties: [
            { name: "sprite_sheet", type: "string" as const, value: obj.spriteSheet },
          ],
        };
      }

      if (obj.type === "npc") {
        return {
          ...base,
          properties: [
            { name: "sprite_sheet", type: "string" as const, value: obj.spriteSheet },
            { name: "sprite_type", type: "string" as const, value: obj.spriteType },
            { name: "frame_col", type: "int" as const, value: obj.frameCol },
            { name: "frame_row", type: "int" as const, value: obj.frameRow },
          ],
        };
      }

      return base;
    }),
    opacity: 1,
    visible: true,
  };

  const tilesetRefs: TiledTilesetRef[] = tilesets.map((ts, tsIndex) => {
    // Start with walkability tile defs
    const tileDefMap = new Map<number, TiledTileDef>();

    ts.walkability.forEach((walkable, id) => {
      if (!walkable) {
        tileDefMap.set(id, {
          id,
          properties: [{ name: "walkable", type: "bool" as const, value: false }],
        });
      }
    });

    // Merge animation frames from matching groups
    if (animationGroups) {
      for (const group of animationGroups) {
        if (group.tilesetIndex !== tsIndex) continue;
        for (const entry of group.entries) {
          const existing = tileDefMap.get(entry.anchorLocalId);
          const animFrames = entry.frames.map((localId) => ({
            tileid: localId,
            duration: group.frameDuration,
          }));
          if (existing) {
            existing.animation = animFrames;
          } else {
            tileDefMap.set(entry.anchorLocalId, { id: entry.anchorLocalId, animation: animFrames });
          }
        }
      }
    }

    return {
      firstgid: ts.firstGid,
      name: ts.name,
      tilewidth: ts.tileWidth,
      tileheight: ts.tileHeight,
      tilecount: ts.tileCount,
      columns: ts.columns,
      image: tilesetImagePath(ts),
      imagewidth: ts.imageWidth,
      imageheight: ts.imageHeight,
      tiles: Array.from(tileDefMap.values()),
    };
  });

  // Collect magic effect groups for map-level property
  const magicEffects = (animationGroups ?? []).filter((g) => g.isMagicEffect);
  const properties: TiledMap["properties"] = magicEffects.length > 0
    ? [{
        name: "magic_effects",
        type: "string",
        value: JSON.stringify(
          magicEffects.map((g) => {
            const ts = tilesets[g.tilesetIndex];
            return {
              id: g.id,
              name: g.name,
              tilesetName: ts?.name ?? "",
              tileWidth: ts?.tileWidth ?? 16,
              tileHeight: ts?.tileHeight ?? 16,
              columns: ts?.columns ?? 1,
              frameDuration: g.frameDuration,
              entries: g.entries.map((e) => ({
                anchorLocalId: e.anchorLocalId,
                frames: e.frames,
              })),
            };
          })
        ),
      }]
    : undefined;

  return {
    width: mapWidth,
    height: mapHeight,
    tilewidth: tileSize,
    tileheight: tileSize,
    orientation: "orthogonal",
    renderorder: "right-down",
    tiledversion: TILED_VERSION,
    version: MAP_VERSION,
    type: "map",
    layers: [...tileLayers, objectLayer],
    tilesets: tilesetRefs,
    ...(properties ? { properties } : {}),
  };
}

export function saveMapToFile(map: TiledMap, filename: string) {
  const json = JSON.stringify(map, null, 2);
  const blob = new Blob([json], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

export function parseMapFromJson(json: string): {
  layers: number[][];
  objects: MapObject[];
  width: number;
  height: number;
  animationGroups: TileAnimationGroup[];
} | null {
  try {
    const map: TiledMap = JSON.parse(json);
    const layers: number[][] = [];
    const objects: MapObject[] = [];
    const animationGroups: TileAnimationGroup[] = [];

    for (const layer of map.layers) {
      if (layer.type === "tilelayer") {
        layers.push([...layer.data]);
      } else if (layer.type === "objectgroup") {
        for (const obj of layer.objects) {
          if (obj.type === "spawn_point") {
            const props = obj.properties ?? [];
            const spriteSheet = props.find((p) => p.name === "sprite_sheet")?.value;
            const sp: MapObject = {
              name: obj.name,
              type: "spawn_point",
              x: obj.x,
              y: obj.y,
              ...(spriteSheet ? { spriteSheet: String(spriteSheet) } : {}),
            };
            objects.push(sp);
          } else if (obj.type === "npc") {
            const props = obj.properties ?? [];
            objects.push({
              name: obj.name,
              type: "npc",
              x: obj.x,
              y: obj.y,
              spriteSheet: String(props.find((p) => p.name === "sprite_sheet")?.value ?? ""),
              spriteType: (String(props.find((p) => p.name === "sprite_type")?.value ?? "character") as "character" | "enemy"),
              frameCol: Number(props.find((p) => p.name === "frame_col")?.value ?? 0),
              frameRow: Number(props.find((p) => p.name === "frame_row")?.value ?? 0),
            });
          }
        }
      }
    }

    // Reconstruct animation groups from tileset tile definitions
    for (let tsIndex = 0; tsIndex < map.tilesets.length; tsIndex++) {
      const tsRef = map.tilesets[tsIndex];
      if (!tsRef.tiles) continue;

      // Group animated tiles by frameDuration
      const byDuration = new Map<number, TileAnimationEntry[]>();

      for (const tileDef of tsRef.tiles) {
        if (!tileDef.animation || tileDef.animation.length === 0) continue;
        const duration = tileDef.animation[0].duration;
        const entry: TileAnimationEntry = {
          anchorLocalId: tileDef.id,
          frames: tileDef.animation.map((f) => f.tileid),
        };
        let arr = byDuration.get(duration);
        if (!arr) {
          arr = [];
          byDuration.set(duration, arr);
        }
        arr.push(entry);
      }

      for (const [frameDuration, entries] of byDuration) {
        animationGroups.push({
          id: crypto.randomUUID(),
          name: `${tsRef.name ?? "Tileset"} ${frameDuration}ms`,
          tilesetIndex: tsIndex,
          frameDuration,
          entries,
        });
      }
    }

    // Restore magic effect groups from map-level properties
    const magicProp = map.properties?.find((p) => p.name === "magic_effects");
    if (magicProp && typeof magicProp.value === "string") {
      try {
        const effects = JSON.parse(magicProp.value) as Array<{
          id: string;
          name: string;
          tilesetName: string;
          frameDuration: number;
          entries: TileAnimationEntry[];
        }>;
        for (const fx of effects) {
          // Match tilesetName to tileset index
          const tsIndex = map.tilesets.findIndex((ts) => ts.name === fx.tilesetName);
          if (tsIndex < 0) continue;
          // Check if this group was already reconstructed from tile animations
          const existing = animationGroups.find(
            (g) =>
              g.tilesetIndex === tsIndex &&
              g.frameDuration === fx.frameDuration &&
              g.entries.length === fx.entries.length &&
              g.entries.every((e, i) => e.anchorLocalId === fx.entries[i].anchorLocalId)
          );
          if (existing) {
            existing.isMagicEffect = true;
            existing.name = fx.name;
            existing.id = fx.id;
          } else {
            animationGroups.push({
              id: fx.id,
              name: fx.name,
              tilesetIndex: tsIndex,
              frameDuration: fx.frameDuration,
              entries: fx.entries,
              isMagicEffect: true,
            });
          }
        }
      } catch {
        // Ignore malformed magic_effects
      }
    }

    // Also restore walkability from tileset tile properties
    return { layers, objects, width: map.width, height: map.height, animationGroups };
  } catch {
    return null;
  }
}
