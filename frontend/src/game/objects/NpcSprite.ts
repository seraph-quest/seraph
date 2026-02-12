import Phaser from "phaser";
import { SCENE } from "../../config/constants";
import type { Pathfinder } from "../lib/Pathfinder";

const FRAME_SIZE = 24;

/** Columns per sheet row by sprite type */
const COLS: Record<string, number> = {
  enemy: 3,
  character: 16,
};

const DIR_ROWS: Array<{ dir: string; row: number }> = [
  { dir: "down", row: 0 },
  { dir: "left", row: 1 },
  { dir: "right", row: 2 },
  { dir: "up", row: 3 },
];

/** NPC wander delays — longer than agent for ambient feel */
const NPC_WANDER_MIN_MS = 4000;
const NPC_WANDER_MAX_MS = 10000;

let npcIdCounter = 0;

export interface NpcConfig {
  key: string;        // Texture key (e.g. "Enemy15_03")
  spriteType: string; // "enemy" or "character"
  frameCol: number;   // Starting column offset within the sheet
}

export class NpcSprite {
  sprite: Phaser.GameObjects.Sprite;
  private scene: Phaser.Scene;
  private id: number;
  private config: NpcConfig;
  private currentTween: Phaser.Tweens.Tween | null = null;
  private pathCancelled = false;

  // Wandering
  private isWandering = false;
  private wanderTimer: Phaser.Time.TimerEvent | null = null;
  private pathfinder: Pathfinder | null = null;
  private wanderZone: { x: number; y: number; width: number; height: number } | undefined;

  constructor(scene: Phaser.Scene, x: number, y: number, config: NpcConfig) {
    this.scene = scene;
    this.id = npcIdCounter++;
    this.config = config;

    const idleFrame = config.frameCol;

    this.sprite = scene.add.sprite(x, y, config.key, idleFrame);
    // No SPRITE_SCALE — camera zoom handles display sizing.
    // Native 24px on 16px tiles matches editor proportions (1.5x).
    this.sprite.setOrigin(0.5, 1);

    this.createAnimations();
    this.playAnim("idle");
  }

  /** Load a sprite sheet for an NPC definition (call during preload/create). */
  static loadSheet(
    scene: Phaser.Scene,
    spriteSheet: string,
    spriteType: string
  ) {
    const dir = spriteType === "enemy" ? "enemies" : "characters";
    scene.load.spritesheet(spriteSheet, `assets/${dir}/${spriteSheet}.png`, {
      frameWidth: FRAME_SIZE,
      frameHeight: FRAME_SIZE,
    });
  }

  private prefix(name: string): string {
    return `npc-${this.id}-${name}`;
  }

  private createAnimations() {
    const { key, spriteType, frameCol } = this.config;
    const cols = COLS[spriteType] ?? 3;
    const animCols = spriteType === "enemy" ? 3 : 4;

    for (const { dir, row } of DIR_ROWS) {
      const base = row * cols + frameCol;
      const frames: Phaser.Types.Animations.AnimationFrame[] = [];
      for (let i = 0; i < animCols; i++) {
        frames.push({ key, frame: base + i });
      }

      this.scene.anims.create({
        key: this.prefix(`walk-${dir}`),
        frames,
        frameRate: 5,
        repeat: -1,
      });
    }

    // Idle: first frame of "down" row
    this.scene.anims.create({
      key: this.prefix("idle"),
      frames: [{ key, frame: frameCol }],
      frameRate: 1,
      repeat: 0,
    });
  }

  playAnim(name: string) {
    this.sprite.play(this.prefix(name));
  }

  moveTo(x: number, y: number, onComplete?: () => void) {
    if (this.currentTween) {
      this.currentTween.stop();
      this.currentTween = null;
    }

    const dx = x - this.sprite.x;
    const dy = y - this.sprite.y;
    const distance = Math.sqrt(dx * dx + dy * dy);

    if (distance < 4) {
      onComplete?.();
      return;
    }

    let walkDir: string;
    if (Math.abs(dx) > Math.abs(dy)) {
      walkDir = dx < 0 ? "walk-left" : "walk-right";
    } else {
      walkDir = dy < 0 ? "walk-up" : "walk-down";
    }
    this.playAnim(walkDir);

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
      this.moveTo(targetX, targetY, onComplete);
      return;
    }

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

  cancelMovement() {
    this.pathCancelled = true;
    if (this.currentTween) {
      this.currentTween.stop();
      this.currentTween = null;
    }
    this.sprite.stop();
  }

  // ─── Wandering ─────────────────────────────────────

  startWandering(
    pathfinder: Pathfinder,
    zone?: { x: number; y: number; width: number; height: number }
  ) {
    if (this.isWandering) return;
    this.pathfinder = pathfinder;
    this.wanderZone = zone;
    this.isWandering = true;
    this.scheduleNextWander();
  }

  stopWandering() {
    this.isWandering = false;
    if (this.wanderTimer) {
      this.wanderTimer.remove(false);
      this.wanderTimer = null;
    }
    this.cancelMovement();
  }

  private scheduleNextWander() {
    if (!this.isWandering) return;
    const delay = Phaser.Math.Between(NPC_WANDER_MIN_MS, NPC_WANDER_MAX_MS);
    this.wanderTimer = this.scene.time.delayedCall(delay, () => {
      this.wanderToRandom();
    });
  }

  private wanderToRandom() {
    if (!this.isWandering || !this.pathfinder) return;

    const target = this.pathfinder.getRandomWalkableTile(this.wanderZone);
    if (!target) {
      this.scheduleNextWander();
      return;
    }

    this.moveAlongPath(this.pathfinder, target.x, target.y, () => {
      this.playAnim("idle");
      this.scheduleNextWander();
    });
  }

  destroy() {
    this.stopWandering();
    if (this.currentTween) this.currentTween.stop();
    this.sprite.destroy();
  }
}
