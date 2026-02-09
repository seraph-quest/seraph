import Phaser from "phaser";
import { SCENE } from "../../config/constants";
import { EventBus } from "../EventBus";

const SPRITE_KEY = "user-avatar";
const FRAME_W = 32;
const FRAME_H = 32;

/** Clickable user avatar positioned at "home" in the village. */
export class UserSprite {
  sprite: Phaser.GameObjects.Sprite;
  private scene: Phaser.Scene;
  private bobTween: Phaser.Tweens.Tween | null = null;
  private currentTween: Phaser.Tweens.Tween | null = null;
  private glowGraphics: Phaser.GameObjects.Graphics | null = null;
  homeX: number;
  homeY: number;

  constructor(scene: Phaser.Scene, x: number, y: number) {
    this.scene = scene;
    this.homeX = x;
    this.homeY = y;

    if (!scene.textures.exists(SPRITE_KEY) || scene.textures.get(SPRITE_KEY).frameTotal <= 2) {
      if (scene.textures.exists(SPRITE_KEY)) {
        scene.textures.remove(SPRITE_KEY);
      }
      this.createFallbackTexture(scene);
    }

    this.sprite = scene.add.sprite(x, y, SPRITE_KEY, "down");
    this.sprite.setScale(SCENE.SPRITE_SCALE);
    this.sprite.setOrigin(0.5, 1);
    this.sprite.setDepth(10);

    this.createAnimations();
    this.sprite.play("user-idle");

    this.startBob();

    // Make clickable
    this.sprite.setInteractive({ useHandCursor: true });
    this.sprite.on("pointerdown", () => {
      EventBus.emit("toggle-quest-log");
    });

    // Hover glow
    this.glowGraphics = scene.add.graphics();
    this.glowGraphics.setDepth(9);
    this.glowGraphics.setVisible(false);

    this.sprite.on("pointerover", () => {
      this.drawGlow();
      this.glowGraphics?.setVisible(true);
    });
    this.sprite.on("pointerout", () => {
      this.glowGraphics?.setVisible(false);
    });
  }

  private drawGlow() {
    if (!this.glowGraphics) return;
    this.glowGraphics.clear();
    this.glowGraphics.fillStyle(0xe2b714, 0.15);
    this.glowGraphics.fillEllipse(
      this.sprite.x,
      this.sprite.y - 12,
      40,
      48
    );
  }

  static preload(scene: Phaser.Scene) {
    scene.load.atlas(
      SPRITE_KEY,
      "assets/user-avatar.png",
      "assets/user-avatar-atlas.json"
    );
  }

  private createFallbackTexture(scene: Phaser.Scene) {
    const WALK_COLS = 6;
    const canvas = scene.textures.createCanvas(SPRITE_KEY, WALK_COLS * FRAME_W, 4 * FRAME_H);
    if (!canvas) return;
    const ctx = canvas.context;

    const directions = [
      { row: 0, label: "down", eyeOffsets: [[10, 12], [18, 12]] },
      { row: 1, label: "left", eyeOffsets: [[8, 12], [14, 12]] },
      { row: 2, label: "right", eyeOffsets: [[14, 12], [20, 12]] },
      { row: 3, label: "up", eyeOffsets: [[10, 12], [18, 12]] },
    ];

    for (const dir of directions) {
      for (let col = 0; col < WALK_COLS; col++) {
        const ox = col * 32;
        const oy = dir.row * 32;
        const legOffset = col % 2 === 0 ? -1 : 1;

        ctx.fillStyle = "#1e40af";
        ctx.fillRect(ox + 8, oy + 14, 16, 12);

        ctx.fillStyle = "#94a3b8";
        ctx.fillRect(ox + 8, oy + 20, 16, 2);

        ctx.fillStyle = "#f5c078";
        ctx.fillRect(ox + 9, oy + 6, 14, 10);

        ctx.fillStyle = "#78350f";
        ctx.fillRect(ox + 9, oy + 4, 14, 4);

        if (dir.row !== 3) {
          ctx.fillStyle = "#ffffff";
          for (const [ex, ey] of dir.eyeOffsets) {
            ctx.fillRect(ox + ex, oy + ey, 4, 3);
            ctx.fillStyle = "#1e3a5f";
            ctx.fillRect(ox + ex + 1, oy + ey + 1, 2, 2);
            ctx.fillStyle = "#ffffff";
          }
        }

        ctx.fillStyle = "#78350f";
        ctx.fillRect(ox + 10 + legOffset, oy + 26, 4, 4);
        ctx.fillRect(ox + 18 - legOffset, oy + 26, 4, 4);
      }
    }

    canvas.refresh();

    const texture = scene.textures.get(SPRITE_KEY);
    for (const dir of directions) {
      for (let col = 0; col < WALK_COLS; col++) {
        const walkFrame = `${dir.label}-walk.${String(col).padStart(3, "0")}`;
        texture.add(walkFrame, 0, col * FRAME_W, dir.row * FRAME_H, FRAME_W, FRAME_H);
      }
      texture.add(dir.label, 0, 0, dir.row * FRAME_H, FRAME_W, FRAME_H);
    }
  }

  private createAnimations() {
    const scene = this.scene;

    scene.anims.create({
      key: "user-idle",
      frames: [{ key: SPRITE_KEY, frame: "down" }],
      frameRate: 1,
      repeat: 0,
    });

    const dirs = ["down", "left", "right", "up"] as const;
    for (const dir of dirs) {
      scene.anims.create({
        key: `user-walk-${dir}`,
        frames: scene.anims.generateFrameNames(SPRITE_KEY, {
          prefix: `${dir}-walk.`,
          start: 0,
          end: 5,
          zeroPad: 3,
        }),
        frameRate: 8,
        repeat: -1,
      });
    }
  }

  moveTo(x: number, y: number, onComplete?: () => void) {
    if (this.currentTween) {
      this.currentTween.stop();
      this.currentTween = null;
    }
    this.stopBob();

    const dx = x - this.sprite.x;
    const dy = y - this.sprite.y;
    const distance = Math.sqrt(dx * dx + dy * dy);

    if (distance < 4) {
      onComplete?.();
      return;
    }

    // Pick direction based on dominant axis
    let walkAnim: string;
    if (Math.abs(dx) > Math.abs(dy)) {
      walkAnim = dx < 0 ? "user-walk-left" : "user-walk-right";
    } else {
      walkAnim = dy < 0 ? "user-walk-up" : "user-walk-down";
    }
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

  returnHome(onComplete?: () => void) {
    this.moveTo(this.homeX, this.homeY, () => {
      this.sprite.play("user-idle");
      this.startBob();
      onComplete?.();
    });
  }

  cancelMovement() {
    if (this.currentTween) {
      this.currentTween.stop();
      this.currentTween = null;
    }
    this.stopBob();
    this.sprite.stop();
  }

  private startBob() {
    this.stopBob();
    this.bobTween = this.scene.tweens.add({
      targets: this.sprite,
      y: this.sprite.y - 2,
      duration: 1400,
      yoyo: true,
      repeat: -1,
      ease: "Sine.easeInOut",
    });
  }

  private stopBob() {
    if (this.bobTween) {
      this.bobTween.stop();
      this.bobTween = null;
    }
  }

  updatePosition(x: number, y: number) {
    this.sprite.setPosition(x, y);
  }

  destroy() {
    this.stopBob();
    if (this.currentTween) this.currentTween.stop();
    this.glowGraphics?.destroy();
    this.sprite.destroy();
  }
}
