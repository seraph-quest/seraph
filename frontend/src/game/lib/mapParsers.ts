import type { MagicEffectDef } from "../../types";

/**
 * Minimal tileset info needed for magic-effect parsing —
 * no Phaser types so this stays a pure, testable function.
 */
export interface TilesetInfo {
  name: string;
  tileWidth: number;
  tileHeight: number;
  columns: number;
  tileData?: Record<
    string,
    { animation?: Array<{ tileid: number; duration: number }> }
  >;
}

/**
 * Build the magic-effect pool from Tiled map custom properties and tileset metadata.
 *
 * The map JSON stores a `magic_effects` property (stringified JSON) that lists
 * which tileset animations are spell overlays. This function cross-references
 * that list with each tileset's `tileData` animation entries to produce a flat
 * array of `MagicEffectDef` objects ready for runtime use.
 */
export function buildMagicEffectPool(
  mapProperties: Array<{ name: string; value: unknown }> | undefined,
  tilesets: TilesetInfo[]
): MagicEffectDef[] {
  if (!mapProperties) return [];

  const magicProp = mapProperties.find((p) => p.name === "magic_effects");
  if (!magicProp || typeof magicProp.value !== "string") return [];

  try {
    const effects = JSON.parse(magicProp.value) as Array<{
      id: string;
      name: string;
      tilesetName: string;
      entries: Array<{ anchorLocalId: number }>;
    }>;

    // Build lookup: tilesetName → Set of anchor local IDs
    const fxAnchors = new Map<string, Set<number>>();
    const fxMeta = new Map<string, { id: string; name: string }>();
    for (const fx of effects) {
      if (!fx.entries || fx.entries.length === 0) continue;
      const anchorId = fx.entries[0].anchorLocalId;
      if (!fxAnchors.has(fx.tilesetName)) {
        fxAnchors.set(fx.tilesetName, new Set());
      }
      fxAnchors.get(fx.tilesetName)!.add(anchorId);
      fxMeta.set(`${fx.tilesetName}:${anchorId}`, {
        id: fx.id,
        name: fx.name,
      });
    }

    const pool: MagicEffectDef[] = [];

    for (const tileset of tilesets) {
      const anchors = fxAnchors.get(tileset.name);
      if (!anchors) continue;

      const tileData = tileset.tileData;
      if (!tileData) continue;

      for (const [localIdStr, data] of Object.entries(tileData)) {
        if (!data.animation || data.animation.length === 0) continue;
        const localId = parseInt(localIdStr, 10);
        if (!anchors.has(localId)) continue;

        const meta = fxMeta.get(`${tileset.name}:${localId}`);
        pool.push({
          id: meta?.id ?? `${tileset.name}-${localId}`,
          name: meta?.name ?? tileset.name,
          tilesetKey: tileset.name,
          tileWidth: tileset.tileWidth,
          tileHeight: tileset.tileHeight,
          columns: tileset.columns,
          frameDuration: data.animation[0].duration,
          frames: data.animation.map((f) => f.tileid),
        });
      }
    }

    return pool;
  } catch {
    return [];
  }
}
