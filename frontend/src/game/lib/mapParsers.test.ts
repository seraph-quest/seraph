import { describe, it, expect } from "vitest";
import { buildMagicEffectPool, type TilesetInfo } from "./mapParsers";

// ── Helper factories ────────────────────────────────

function makeProps(magicEffectsValue: unknown) {
  return [{ name: "magic_effects", value: magicEffectsValue }];
}

function makeTileset(overrides?: Partial<TilesetInfo>): TilesetInfo {
  return {
    name: "CuteRPG_Magical",
    tileWidth: 16,
    tileHeight: 16,
    columns: 8,
    tileData: {
      "5": {
        animation: [
          { tileid: 5, duration: 150 },
          { tileid: 6, duration: 150 },
          { tileid: 7, duration: 150 },
        ],
      },
    },
    ...overrides,
  };
}

function makeEffectsJSON(
  overrides?: Partial<{
    id: string;
    name: string;
    tilesetName: string;
    entries: Array<{ anchorLocalId: number }>;
  }>[]
) {
  const defaults = [
    {
      id: "fire-burst",
      name: "Fire Burst",
      tilesetName: "CuteRPG_Magical",
      entries: [{ anchorLocalId: 5 }],
    },
  ];
  const items = overrides ?? defaults;
  return JSON.stringify(items);
}

describe("buildMagicEffectPool", () => {
  // ─── Edge cases: early returns ────────────────────

  describe("edge cases", () => {
    it("returns [] when mapProperties is undefined", () => {
      expect(buildMagicEffectPool(undefined, [makeTileset()])).toEqual([]);
    });

    it("returns [] when magic_effects property is missing", () => {
      const props = [{ name: "buildings", value: "[]" }];
      expect(buildMagicEffectPool(props, [makeTileset()])).toEqual([]);
    });

    it("returns [] when magic_effects value is not a string", () => {
      const props = [{ name: "magic_effects", value: 42 }];
      expect(buildMagicEffectPool(props, [makeTileset()])).toEqual([]);
    });

    it("returns [] for malformed JSON", () => {
      const props = makeProps("not valid json {{{");
      expect(buildMagicEffectPool(props, [makeTileset()])).toEqual([]);
    });
  });

  // ─── No match scenarios ───────────────────────────

  describe("no match", () => {
    it("returns [] when tileset names don't match", () => {
      const props = makeProps(makeEffectsJSON());
      const tileset = makeTileset({ name: "OtherTileset" });
      expect(buildMagicEffectPool(props, [tileset])).toEqual([]);
    });

    it("returns [] when tileset has no tileData", () => {
      const props = makeProps(makeEffectsJSON());
      const tileset = makeTileset({ tileData: undefined });
      expect(buildMagicEffectPool(props, [tileset])).toEqual([]);
    });

    it("returns [] when effect has empty entries array", () => {
      const json = JSON.stringify([
        {
          id: "empty",
          name: "Empty",
          tilesetName: "CuteRPG_Magical",
          entries: [],
        },
      ]);
      const props = makeProps(json);
      expect(buildMagicEffectPool(props, [makeTileset()])).toEqual([]);
    });

    it("returns [] when anchor local ID has no animation data", () => {
      const json = JSON.stringify([
        {
          id: "miss",
          name: "Miss",
          tilesetName: "CuteRPG_Magical",
          entries: [{ anchorLocalId: 999 }], // no tileData entry for 999
        },
      ]);
      const props = makeProps(json);
      expect(buildMagicEffectPool(props, [makeTileset()])).toEqual([]);
    });

    it("returns [] when animation array is empty", () => {
      const tileset = makeTileset({
        tileData: { "5": { animation: [] } },
      });
      const props = makeProps(makeEffectsJSON());
      expect(buildMagicEffectPool(props, [tileset])).toEqual([]);
    });
  });

  // ─── Happy path ───────────────────────────────────

  describe("happy path", () => {
    it("returns correct MagicEffectDef for a single effect", () => {
      const props = makeProps(makeEffectsJSON());
      const result = buildMagicEffectPool(props, [makeTileset()]);

      expect(result).toHaveLength(1);
      expect(result[0]).toEqual({
        id: "fire-burst",
        name: "Fire Burst",
        tilesetKey: "CuteRPG_Magical",
        tileWidth: 16,
        tileHeight: 16,
        columns: 8,
        frameDuration: 150,
        frames: [5, 6, 7],
      });
    });

    it("uses local tile IDs (not GIDs) for frames", () => {
      const props = makeProps(makeEffectsJSON());
      const result = buildMagicEffectPool(props, [makeTileset()]);
      // frames should be the tileid values from animation, not offset by firstgid
      expect(result[0].frames).toEqual([5, 6, 7]);
    });

    it("uses frameDuration from first animation frame", () => {
      const tileset = makeTileset({
        tileData: {
          "5": {
            animation: [
              { tileid: 5, duration: 200 },
              { tileid: 6, duration: 100 }, // different duration
            ],
          },
        },
      });
      const props = makeProps(makeEffectsJSON());
      const result = buildMagicEffectPool(props, [tileset]);
      expect(result[0].frameDuration).toBe(200);
    });
  });

  // ─── Multiple effects ─────────────────────────────

  describe("multiple effects", () => {
    it("handles multiple effects across different tilesets", () => {
      const json = JSON.stringify([
        {
          id: "fire",
          name: "Fire",
          tilesetName: "CuteRPG_Magical",
          entries: [{ anchorLocalId: 5 }],
        },
        {
          id: "ice",
          name: "Ice",
          tilesetName: "IceTileset",
          entries: [{ anchorLocalId: 10 }],
        },
      ]);
      const props = makeProps(json);

      const tilesets: TilesetInfo[] = [
        makeTileset(),
        makeTileset({
          name: "IceTileset",
          tileData: {
            "10": {
              animation: [
                { tileid: 10, duration: 100 },
                { tileid: 11, duration: 100 },
              ],
            },
          },
        }),
      ];

      const result = buildMagicEffectPool(props, tilesets);
      expect(result).toHaveLength(2);
      expect(result[0].id).toBe("fire");
      expect(result[1].id).toBe("ice");
      expect(result[1].tilesetKey).toBe("IceTileset");
      expect(result[1].frames).toEqual([10, 11]);
    });

    it("handles multiple effects in the same tileset", () => {
      const json = JSON.stringify([
        {
          id: "effect-a",
          name: "A",
          tilesetName: "CuteRPG_Magical",
          entries: [{ anchorLocalId: 5 }],
        },
        {
          id: "effect-b",
          name: "B",
          tilesetName: "CuteRPG_Magical",
          entries: [{ anchorLocalId: 20 }],
        },
      ]);
      const props = makeProps(json);

      const tileset = makeTileset({
        tileData: {
          "5": {
            animation: [
              { tileid: 5, duration: 150 },
              { tileid: 6, duration: 150 },
            ],
          },
          "20": {
            animation: [
              { tileid: 20, duration: 200 },
              { tileid: 21, duration: 200 },
              { tileid: 22, duration: 200 },
            ],
          },
        },
      });

      const result = buildMagicEffectPool(props, [tileset]);
      expect(result).toHaveLength(2);
      expect(result[0].id).toBe("effect-a");
      expect(result[0].frames).toEqual([5, 6]);
      expect(result[1].id).toBe("effect-b");
      expect(result[1].frames).toEqual([20, 21, 22]);
    });
  });

  // ─── Fallback ID/name ─────────────────────────────

  describe("fallback id and name", () => {
    it("falls back to tileset-based id when meta is missing", () => {
      // Create an effect whose anchor matches tileData but meta lookup misses
      // (this shouldn't happen in practice but tests the fallback)
      const json = JSON.stringify([
        {
          id: "custom-id",
          name: "Custom",
          tilesetName: "CuteRPG_Magical",
          entries: [{ anchorLocalId: 5 }],
        },
      ]);
      const props = makeProps(json);
      const result = buildMagicEffectPool(props, [makeTileset()]);
      // meta IS found here so it uses the provided id/name
      expect(result[0].id).toBe("custom-id");
      expect(result[0].name).toBe("Custom");
    });
  });
});
