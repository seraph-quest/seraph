import Phaser from "phaser";
import { SCENE } from "../../config/constants";

const SPRITE_KEY = "character";
const FRAME_W = 32;
const FRAME_H = 32;

/**
 * Pipoya 32x32 spritesheet layout (3 cols x 4 rows):
 *   Row 0 (frames 0-2): facing down
 *   Row 1 (frames 3-5): facing left
 *   Row 2 (frames 6-8): facing right
 *   Row 3 (frames 9-11): facing up
 *
 * Frame 1 in each row = idle pose, frames 0+2 = walk cycle
 */
export class AgentSprite {
  sprite: Phaser.GameObjects.Sprite;
  private scene: Phaser.Scene;
  private currentTween: Phaser.Tweens.Tween | null = null;
  private bobTween: Phaser.Tweens.Tween | null = null;

  constructor(scene: Phaser.Scene, x: number, y: number) {
    this.scene = scene;

    if (!scene.textures.exists(SPRITE_KEY)) {
      this.createFallbackTexture(scene);
    }
    this.sprite = scene.add.sprite(x, y, SPRITE_KEY, 1);

    this.sprite.setScale(SCENE.SPRITE_SCALE);
    this.sprite.setOrigin(0.5, 1);
    this.sprite.setDepth(10);

    this.createAnimations();
    this.playAnim("idle");
  }

  static preload(scene: Phaser.Scene) {
    scene.load.spritesheet(SPRITE_KEY, "assets/character.png", {
      frameWidth: FRAME_W,
      frameHeight: FRAME_H,
    });
  }

  private createFallbackTexture(scene: Phaser.Scene) {
    // Generate a 96x128 placeholder spritesheet (3 cols x 4 rows of 32x32)
    const canvas = scene.textures.createCanvas(SPRITE_KEY, 96, 128);
    if (!canvas) return;
    const ctx = canvas.context;

    const directions = [
      { row: 0, eyeOffsets: [[10, 12], [18, 12]] },         // down
      { row: 1, eyeOffsets: [[8, 12], [14, 12]] },          // left
      { row: 2, eyeOffsets: [[14, 12], [20, 12]] },         // right
      { row: 3, eyeOffsets: [[10, 12], [18, 12]] },         // up
    ];

    for (const dir of directions) {
      for (let col = 0; col < 3; col++) {
        const ox = col * 32;
        const oy = dir.row * 32;
        const legOffset = col === 1 ? 0 : (col === 0 ? -1 : 1);

        // Body/robe (indigo)
        ctx.fillStyle = "#3b0764";
        ctx.fillRect(ox + 8, oy + 14, 16, 12);

        // Belt
        ctx.fillStyle = "#e2b714";
        ctx.fillRect(ox + 8, oy + 20, 16, 2);

        // Head (amber)
        ctx.fillStyle = "#d97706";
        ctx.fillRect(ox + 9, oy + 6, 14, 10);

        // Hair
        ctx.fillStyle = "#1e1b4b";
        ctx.fillRect(ox + 9, oy + 4, 14, 4);

        // Eyes (only for non-up-facing)
        if (dir.row !== 3) {
          ctx.fillStyle = "#ffffff";
          for (const [ex, ey] of dir.eyeOffsets) {
            ctx.fillRect(ox + ex, oy + ey, 4, 3);
            ctx.fillStyle = "#1a1a2e";
            ctx.fillRect(ox + ex + 1, oy + ey + 1, 2, 2);
            ctx.fillStyle = "#ffffff";
          }
        }

        // Legs
        ctx.fillStyle = "#6b7280";
        ctx.fillRect(ox + 10 + legOffset, oy + 26, 4, 4);
        ctx.fillRect(ox + 18 - legOffset, oy + 26, 4, 4);
      }
    }

    canvas.refresh();
  }

  private createAnimations() {
    const scene = this.scene;

    // Walk animations (3 frames per direction)
    scene.anims.create({
      key: "walk-down",
      frames: scene.anims.generateFrameNumbers(SPRITE_KEY, { frames: [0, 1, 2, 1] }),
      frameRate: 8,
      repeat: -1,
    });
    scene.anims.create({
      key: "walk-left",
      frames: scene.anims.generateFrameNumbers(SPRITE_KEY, { frames: [3, 4, 5, 4] }),
      frameRate: 8,
      repeat: -1,
    });
    scene.anims.create({
      key: "walk-right",
      frames: scene.anims.generateFrameNumbers(SPRITE_KEY, { frames: [6, 7, 8, 7] }),
      frameRate: 8,
      repeat: -1,
    });
    scene.anims.create({
      key: "walk-up",
      frames: scene.anims.generateFrameNumbers(SPRITE_KEY, { frames: [9, 10, 11, 10] }),
      frameRate: 8,
      repeat: -1,
    });

    // Idle: facing down, single frame
    scene.anims.create({
      key: "idle",
      frames: [{ key: SPRITE_KEY, frame: 1 }],
      frameRate: 1,
      repeat: 0,
    });

    // Think: slow cycle facing down
    scene.anims.create({
      key: "think",
      frames: scene.anims.generateFrameNumbers(SPRITE_KEY, { frames: [1, 0, 1, 2] }),
      frameRate: 3,
      repeat: -1,
    });

    // At-computer: facing left, slow idle
    scene.anims.create({
      key: "at-computer",
      frames: scene.anims.generateFrameNumbers(SPRITE_KEY, { frames: [3, 4, 5, 4] }),
      frameRate: 4,
      repeat: -1,
    });

    // At-cabinet: facing right, slow idle
    scene.anims.create({
      key: "at-cabinet",
      frames: scene.anims.generateFrameNumbers(SPRITE_KEY, { frames: [6, 7, 8, 7] }),
      frameRate: 4,
      repeat: -1,
    });

    // At-desk: facing up, slow cycle
    scene.anims.create({
      key: "at-desk",
      frames: scene.anims.generateFrameNumbers(SPRITE_KEY, { frames: [9, 10, 11, 10] }),
      frameRate: 3,
      repeat: -1,
    });
  }

  moveTo(x: number, y: number, onComplete?: () => void) {
    if (this.currentTween) {
      this.currentTween.stop();
      this.currentTween = null;
    }
    this.stopBob();

    const dx = x - this.sprite.x;
    const distance = Math.abs(dx);

    if (distance < 4) {
      onComplete?.();
      return;
    }

    // Pick walk direction anim
    const walkAnim = dx < 0 ? "walk-left" : "walk-right";
    this.sprite.play(walkAnim);

    const duration = (distance / SCENE.WALK_SPEED) * 1000;

    this.currentTween = this.scene.tweens.add({
      targets: this.sprite,
      x,
      y,
      duration,
      ease: "Sine.easeInOut",
      onComplete: () => {
        this.currentTween = null;
        onComplete?.();
      },
    });
  }

  playAnim(key: string) {
    this.stopBob();
    this.sprite.play(key);

    if (key === "idle" || key === "think") {
      this.bobTween = this.scene.tweens.add({
        targets: this.sprite,
        y: this.sprite.y - 2,
        duration: key === "idle" ? 1200 : 1500,
        yoyo: true,
        repeat: -1,
        ease: "Sine.easeInOut",
      });
    }
  }

  private stopBob() {
    if (this.bobTween) {
      this.bobTween.stop();
      this.bobTween = null;
    }
  }

  destroy() {
    this.stopBob();
    if (this.currentTween) this.currentTween.stop();
    this.sprite.destroy();
  }
}
