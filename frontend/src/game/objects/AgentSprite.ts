import Phaser from "phaser";
import { SCENE } from "../../config/constants";

const SPRITE_KEY = "agent";
const FRAME_W = 32;
const FRAME_H = 32;

export class AgentSprite {
  sprite: Phaser.GameObjects.Sprite;
  private scene: Phaser.Scene;
  private currentTween: Phaser.Tweens.Tween | null = null;
  private bobTween: Phaser.Tweens.Tween | null = null;

  constructor(scene: Phaser.Scene, x: number, y: number) {
    this.scene = scene;

    const hasValidFrames =
      scene.textures.exists(SPRITE_KEY) &&
      scene.textures.get(SPRITE_KEY).frameTotal > 2;

    if (!hasValidFrames) {
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
    this.playAnim("idle");
  }

  static preload(scene: Phaser.Scene) {
    scene.load.atlas(
      SPRITE_KEY,
      "assets/agent.png",
      "assets/agent-atlas.json"
    );
  }

  private createFallbackTexture(scene: Phaser.Scene) {
    const canvas = scene.textures.createCanvas(SPRITE_KEY, 96, 128);
    if (!canvas) return;
    const ctx = canvas.context;

    const directions = [
      { row: 0, label: "down", eyeOffsets: [[10, 12], [18, 12]] },
      { row: 1, label: "left", eyeOffsets: [[8, 12], [14, 12]] },
      { row: 2, label: "right", eyeOffsets: [[14, 12], [20, 12]] },
      { row: 3, label: "up", eyeOffsets: [[10, 12], [18, 12]] },
    ];

    for (const dir of directions) {
      for (let col = 0; col < 3; col++) {
        const ox = col * 32;
        const oy = dir.row * 32;
        const legOffset = col === 1 ? 0 : col === 0 ? -1 : 1;

        ctx.fillStyle = "#3b0764";
        ctx.fillRect(ox + 8, oy + 14, 16, 12);

        ctx.fillStyle = "#e2b714";
        ctx.fillRect(ox + 8, oy + 20, 16, 2);

        ctx.fillStyle = "#d97706";
        ctx.fillRect(ox + 9, oy + 6, 14, 10);

        ctx.fillStyle = "#1e1b4b";
        ctx.fillRect(ox + 9, oy + 4, 14, 4);

        if (dir.row !== 3) {
          ctx.fillStyle = "#ffffff";
          for (const [ex, ey] of dir.eyeOffsets) {
            ctx.fillRect(ox + ex, oy + ey, 4, 3);
            ctx.fillStyle = "#1a1a2e";
            ctx.fillRect(ox + ex + 1, oy + ey + 1, 2, 2);
            ctx.fillStyle = "#ffffff";
          }
        }

        ctx.fillStyle = "#6b7280";
        ctx.fillRect(ox + 10 + legOffset, oy + 26, 4, 4);
        ctx.fillRect(ox + 18 - legOffset, oy + 26, 4, 4);
      }
    }

    canvas.refresh();

    // Register named frames matching atlas format
    const texture = scene.textures.get(SPRITE_KEY);
    for (const dir of directions) {
      for (let col = 0; col < 3; col++) {
        const walkFrame = `${dir.label}-walk.00${col}`;
        texture.add(walkFrame, 0, col * FRAME_W, dir.row * FRAME_H, FRAME_W, FRAME_H);
      }
      // Frame 3 duplicates frame 1 (same as atlas)
      const walk3 = `${dir.label}-walk.003`;
      texture.add(walk3, 0, 1 * FRAME_W, dir.row * FRAME_H, FRAME_W, FRAME_H);
      // Idle frame = col 1
      texture.add(dir.label, 0, 1 * FRAME_W, dir.row * FRAME_H, FRAME_W, FRAME_H);
    }
  }

  private createAnimations() {
    const scene = this.scene;

    // Walk animations using atlas named frames
    scene.anims.create({
      key: "walk-down",
      frames: scene.anims.generateFrameNames(SPRITE_KEY, {
        prefix: "down-walk.",
        start: 0,
        end: 3,
        zeroPad: 3,
      }),
      frameRate: 8,
      repeat: -1,
    });
    scene.anims.create({
      key: "walk-left",
      frames: scene.anims.generateFrameNames(SPRITE_KEY, {
        prefix: "left-walk.",
        start: 0,
        end: 3,
        zeroPad: 3,
      }),
      frameRate: 8,
      repeat: -1,
    });
    scene.anims.create({
      key: "walk-right",
      frames: scene.anims.generateFrameNames(SPRITE_KEY, {
        prefix: "right-walk.",
        start: 0,
        end: 3,
        zeroPad: 3,
      }),
      frameRate: 8,
      repeat: -1,
    });
    scene.anims.create({
      key: "walk-up",
      frames: scene.anims.generateFrameNames(SPRITE_KEY, {
        prefix: "up-walk.",
        start: 0,
        end: 3,
        zeroPad: 3,
      }),
      frameRate: 8,
      repeat: -1,
    });

    // Idle: facing down, single frame
    scene.anims.create({
      key: "idle",
      frames: [{ key: SPRITE_KEY, frame: "down" }],
      frameRate: 1,
      repeat: 0,
    });

    // Think: slow cycle facing down
    scene.anims.create({
      key: "think",
      frames: scene.anims.generateFrameNames(SPRITE_KEY, {
        prefix: "down-walk.",
        start: 0,
        end: 3,
        zeroPad: 3,
      }),
      frameRate: 3,
      repeat: -1,
    });

    // At-well: facing left, slow idle
    scene.anims.create({
      key: "at-well",
      frames: scene.anims.generateFrameNames(SPRITE_KEY, {
        prefix: "left-walk.",
        start: 0,
        end: 3,
        zeroPad: 3,
      }),
      frameRate: 4,
      repeat: -1,
    });

    // At-signpost: facing right, slow idle
    scene.anims.create({
      key: "at-signpost",
      frames: scene.anims.generateFrameNames(SPRITE_KEY, {
        prefix: "right-walk.",
        start: 0,
        end: 3,
        zeroPad: 3,
      }),
      frameRate: 4,
      repeat: -1,
    });

    // At-bench: facing up, slow cycle
    scene.anims.create({
      key: "at-bench",
      frames: scene.anims.generateFrameNames(SPRITE_KEY, {
        prefix: "up-walk.",
        start: 0,
        end: 3,
        zeroPad: 3,
      }),
      frameRate: 3,
      repeat: -1,
    });

    // At-forge: facing down, medium cycle (hammering)
    scene.anims.create({
      key: "at-forge",
      frames: scene.anims.generateFrameNames(SPRITE_KEY, {
        prefix: "down-walk.",
        start: 0,
        end: 3,
        zeroPad: 3,
      }),
      frameRate: 5,
      repeat: -1,
    });

    // At-tower: facing up, slow cycle (looking up/observing)
    scene.anims.create({
      key: "at-tower",
      frames: scene.anims.generateFrameNames(SPRITE_KEY, {
        prefix: "up-walk.",
        start: 0,
        end: 3,
        zeroPad: 3,
      }),
      frameRate: 4,
      repeat: -1,
    });

    // At-clock: facing right, slow cycle (checking time)
    scene.anims.create({
      key: "at-clock",
      frames: scene.anims.generateFrameNames(SPRITE_KEY, {
        prefix: "right-walk.",
        start: 0,
        end: 3,
        zeroPad: 3,
      }),
      frameRate: 3,
      repeat: -1,
    });

    // At-mailbox: facing left, slow cycle (reading letter)
    scene.anims.create({
      key: "at-mailbox",
      frames: scene.anims.generateFrameNames(SPRITE_KEY, {
        prefix: "left-walk.",
        start: 0,
        end: 3,
        zeroPad: 3,
      }),
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

  cancelMovement() {
    if (this.currentTween) {
      this.currentTween.stop();
      this.currentTween = null;
    }
    this.stopBob();
    this.sprite.stop();
  }

  destroy() {
    this.stopBob();
    if (this.currentTween) this.currentTween.stop();
    this.sprite.destroy();
  }
}
