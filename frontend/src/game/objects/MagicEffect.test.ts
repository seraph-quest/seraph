import { describe, it, expect, vi, beforeEach } from "vitest";
import { createMockScene, type MockScene } from "../../test/mocks/phaser";
import type { MagicEffectDef } from "../../types";

vi.mock("phaser", async () => {
  const mocks = await import("../../test/mocks/phaser");
  return { default: mocks.default };
});

import { MagicEffect } from "./MagicEffect";

const OFFSET_X = 12;
const OFFSET_Y = -20;

function makeDef(overrides?: Partial<MagicEffectDef>): MagicEffectDef {
  return {
    id: "fire-1",
    name: "Fire Burst",
    tilesetKey: "CuteRPG_Magical",
    tileWidth: 16,
    tileHeight: 16,
    columns: 8,
    frameDuration: 150,
    frames: [0, 1, 2, 3],
    ...overrides,
  };
}

describe("MagicEffect", () => {
  let scene: MockScene;

  beforeEach(() => {
    scene = createMockScene();
  });

  // ─── Animation registration ───────────────────────

  describe("animation registration", () => {
    it("registers animation with correct key and config", () => {
      scene.anims.exists.mockReturnValue(false);
      const def = makeDef();
      new MagicEffect(scene as never, def, 100, 200);

      expect(scene.anims.create).toHaveBeenCalledWith(
        expect.objectContaining({
          key: `magic-fx-${def.id}`,
          frameRate: 1000 / def.frameDuration,
          repeat: -1,
        })
      );
    });

    it("maps frames to tileset key + local IDs", () => {
      scene.anims.exists.mockReturnValue(false);
      const def = makeDef({ frames: [10, 11, 12] });
      new MagicEffect(scene as never, def, 0, 0);

      const createCall = scene.anims.create.mock.calls[0][0];
      expect(createCall.frames).toEqual([
        { key: def.tilesetKey, frame: 10 },
        { key: def.tilesetKey, frame: 11 },
        { key: def.tilesetKey, frame: 12 },
      ]);
    });

    it("skips registration if animation already exists", () => {
      scene.anims.exists.mockReturnValue(true);
      new MagicEffect(scene as never, makeDef(), 0, 0);

      expect(scene.anims.create).not.toHaveBeenCalled();
    });
  });

  // ─── Sprite creation ──────────────────────────────

  describe("sprite creation", () => {
    it("creates sprite at offset position", () => {
      const def = makeDef();
      new MagicEffect(scene as never, def, 100, 200);

      expect(scene.add.sprite).toHaveBeenCalledWith(
        100 + OFFSET_X,
        200 + OFFSET_Y,
        def.tilesetKey,
        def.frames[0]
      );
    });

    it("sets depth to 50", () => {
      new MagicEffect(scene as never, makeDef(), 0, 0);
      expect(scene._sprite.setDepth).toHaveBeenCalledWith(50);
    });

    it("plays the animation", () => {
      const def = makeDef();
      new MagicEffect(scene as never, def, 0, 0);
      expect(scene._sprite.play).toHaveBeenCalledWith(`magic-fx-${def.id}`);
    });
  });

  // ─── No setScale regression ───────────────────────

  describe("no setScale regression", () => {
    it("never calls setScale on the sprite", () => {
      new MagicEffect(scene as never, makeDef(), 50, 50);
      expect(scene._sprite.setScale).not.toHaveBeenCalled();
    });
  });

  // ─── setPosition ──────────────────────────────────

  describe("setPosition()", () => {
    it("applies offsets correctly", () => {
      const effect = new MagicEffect(scene as never, makeDef(), 0, 0);
      effect.setPosition(200, 300);

      expect(scene._sprite.setPosition).toHaveBeenCalledWith(
        200 + OFFSET_X,
        300 + OFFSET_Y
      );
    });
  });

  // ─── fadeOut ──────────────────────────────────────

  describe("fadeOut()", () => {
    it("creates a tween to alpha 0", () => {
      const effect = new MagicEffect(scene as never, makeDef(), 0, 0);
      effect.fadeOut();

      expect(scene.tweens.add).toHaveBeenCalledWith(
        expect.objectContaining({
          targets: scene._sprite,
          alpha: 0,
          duration: 400,
        })
      );
    });

    it("destroys sprite and calls onComplete callback", () => {
      const effect = new MagicEffect(scene as never, makeDef(), 0, 0);
      const onComplete = vi.fn();
      effect.fadeOut(onComplete);

      // Fire the tween's onComplete
      const tweenConfig = scene._lastTween!;
      (tweenConfig.onComplete as () => void)();

      expect(scene._sprite.destroy).toHaveBeenCalled();
      expect(onComplete).toHaveBeenCalled();
    });
  });

  // ─── destroy ──────────────────────────────────────

  describe("destroy()", () => {
    it("destroys the sprite when scene is truthy", () => {
      const effect = new MagicEffect(scene as never, makeDef(), 0, 0);
      effect.destroy();
      expect(scene._sprite.destroy).toHaveBeenCalled();
    });

    it("is safe when sprite.scene is falsy", () => {
      const effect = new MagicEffect(scene as never, makeDef(), 0, 0);
      // Simulate sprite already destroyed
      scene._sprite.scene = null;
      expect(() => effect.destroy()).not.toThrow();
    });
  });
});
