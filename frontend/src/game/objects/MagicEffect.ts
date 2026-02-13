import Phaser from "phaser";
import type { MagicEffectDef } from "../../types";

/** Offset from agent position */
const OFFSET_X = 12;
const OFFSET_Y = -20;

export class MagicEffect {
  readonly sprite: Phaser.GameObjects.Sprite;
  private scene: Phaser.Scene;
  private animKey: string;

  constructor(scene: Phaser.Scene, def: MagicEffectDef, x: number, y: number) {
    this.scene = scene;
    this.animKey = `magic-fx-${def.id}`;

    // Register animation if not already defined
    if (!scene.anims.exists(this.animKey)) {
      scene.anims.create({
        key: this.animKey,
        frames: def.frames.map((localId) => ({
          key: def.tilesetKey,
          frame: localId,
        })),
        frameRate: 1000 / def.frameDuration,
        repeat: -1,
      });
    }

    this.sprite = scene.add.sprite(x + OFFSET_X, y + OFFSET_Y, def.tilesetKey, def.frames[0]);
    this.sprite.setDepth(50);
    this.sprite.play(this.animKey);
  }

  setPosition(x: number, y: number) {
    this.sprite.setPosition(x + OFFSET_X, y + OFFSET_Y);
  }

  fadeOut(onComplete?: () => void) {
    this.scene.tweens.add({
      targets: this.sprite,
      alpha: 0,
      duration: 400,
      onComplete: () => {
        this.destroy();
        onComplete?.();
      },
    });
  }

  destroy() {
    if (this.sprite && this.sprite.scene) {
      this.sprite.destroy();
    }
  }
}
