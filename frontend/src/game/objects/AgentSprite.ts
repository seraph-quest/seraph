import Phaser from "phaser";
import { SCENE } from "../../config/constants";
import type { Pathfinder } from "../lib/Pathfinder";

const SPRITE_KEY = "agent";
const FRAME_W = 32;
const FRAME_H = 32;
const CHAR_SHEET_COLS = 16;

export interface SpriteConfig {
  key: string;       // Spritesheet texture key (e.g. "Character_010")
  colOffset: number; // Column offset within the sheet (0, 4, 8, or 12)
}

export class AgentSprite {
  sprite: Phaser.GameObjects.Sprite;
  private scene: Phaser.Scene;
  private currentTween: Phaser.Tweens.Tween | null = null;
  private bobTween: Phaser.Tweens.Tween | null = null;
  private pathCancelled = false;
  currentBuilding: string | null = null;
  currentFloor: number = 0;

  constructor(scene: Phaser.Scene, x: number, y: number, spriteConfig?: SpriteConfig) {
    this.scene = scene;

    if (spriteConfig && scene.textures.exists(spriteConfig.key)) {
      this.sprite = scene.add.sprite(x, y, spriteConfig.key, spriteConfig.colOffset);
      this.sprite.setOrigin(0.5, 1);
      this.sprite.setDepth(10);
      this.createCharSheetAnimations(spriteConfig);
    } else {
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
      this.sprite.setOrigin(0.5, 1);
      this.sprite.setDepth(10);
      this.createAnimations();
    }

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

    const texture = scene.textures.get(SPRITE_KEY);
    for (const dir of directions) {
      for (let col = 0; col < WALK_COLS; col++) {
        const walkFrame = `${dir.label}-walk.${String(col).padStart(3, "0")}`;
        texture.add(walkFrame, 0, col * FRAME_W, dir.row * FRAME_H, FRAME_W, FRAME_H);
      }
      // Idle frame = col 0
      texture.add(dir.label, 0, 0, dir.row * FRAME_H, FRAME_W, FRAME_H);
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
        end: 5,
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
        end: 5,
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
        end: 5,
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
        end: 5,
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
        end: 5,
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
        end: 5,
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
        end: 5,
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
        end: 5,
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
        end: 5,
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
        end: 5,
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
        end: 5,
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
        end: 5,
        zeroPad: 3,
      }),
      frameRate: 3,
      repeat: -1,
    });
  }

  private createCharSheetAnimations(config: SpriteConfig) {
    const scene = this.scene;
    const { key, colOffset } = config;
    const cols = CHAR_SHEET_COLS;

    const dirMap: Array<{ dir: string; row: number }> = [
      { dir: "down", row: 0 },
      { dir: "left", row: 1 },
      { dir: "right", row: 2 },
      { dir: "up", row: 3 },
    ];

    const framesByDir: Record<string, Phaser.Types.Animations.AnimationFrame[]> = {};

    for (const { dir, row } of dirMap) {
      const frames: Phaser.Types.Animations.AnimationFrame[] = [];
      for (let i = 0; i < 4; i++) {
        frames.push({ key, frame: row * cols + colOffset + i });
      }
      framesByDir[dir] = frames;

      scene.anims.create({
        key: `walk-${dir}`,
        frames,
        frameRate: 8,
        repeat: -1,
      });
    }

    scene.anims.create({
      key: "idle",
      frames: [{ key, frame: colOffset }],
      frameRate: 1,
      repeat: 0,
    });

    scene.anims.create({ key: "think", frames: framesByDir["down"], frameRate: 3, repeat: -1 });
    scene.anims.create({ key: "at-well", frames: framesByDir["left"], frameRate: 4, repeat: -1 });
    scene.anims.create({ key: "at-mailbox", frames: framesByDir["left"], frameRate: 3, repeat: -1 });
    scene.anims.create({ key: "at-signpost", frames: framesByDir["right"], frameRate: 4, repeat: -1 });
    scene.anims.create({ key: "at-clock", frames: framesByDir["right"], frameRate: 3, repeat: -1 });
    scene.anims.create({ key: "at-bench", frames: framesByDir["up"], frameRate: 3, repeat: -1 });
    scene.anims.create({ key: "at-tower", frames: framesByDir["up"], frameRate: 4, repeat: -1 });
    scene.anims.create({ key: "at-forge", frames: framesByDir["down"], frameRate: 5, repeat: -1 });
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

    // Pick walk direction based on dominant axis
    let walkAnim: string;
    if (Math.abs(dx) > Math.abs(dy)) {
      walkAnim = dx < 0 ? "walk-left" : "walk-right";
    } else {
      walkAnim = dy < 0 ? "walk-up" : "walk-down";
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

  /**
   * Move along a pathfinding route. Falls back to direct tween if no path found.
   */
  async moveAlongPath(
    pathfinder: Pathfinder,
    targetX: number,
    targetY: number,
    onComplete?: () => void
  ) {
    this.cancelMovement();
    this.pathCancelled = false;

    const path = await pathfinder.findPath(
      this.sprite.x,
      this.sprite.y,
      targetX,
      targetY
    );

    if (this.pathCancelled) return;

    if (!path || path.length === 0) {
      // Fallback: direct move
      this.moveTo(targetX, targetY, onComplete);
      return;
    }

    // Walk along path waypoints sequentially
    const walkSegment = (index: number) => {
      if (this.pathCancelled || index >= path.length) {
        onComplete?.();
        return;
      }

      const target = path[index];
      this.moveTo(target.x, target.y, () => walkSegment(index + 1));
    };

    walkSegment(0);
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
    this.pathCancelled = true;
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
