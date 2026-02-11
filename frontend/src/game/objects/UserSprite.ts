import Phaser from "phaser";
import { SCENE } from "../../config/constants";
import { EventBus } from "../EventBus";

const SPRITE_KEY = "user-avatar";
const FRAME_W = 32;
const FRAME_H = 32;

const STATUS_FONT_SIZE = 7;
const STATUS_PADDING = 4;
const STATUS_TAIL = 4;

const STATE_VISUALS: Record<string, { color: number; label: string }> = {
  goal_behind: { color: 0xef4444, label: "Behind!" },
  on_track:    { color: 0x22c55e, label: "On Track" },
  has_insight:  { color: 0xeab308, label: "Insight" },
  waiting:     { color: 0x3b82f6, label: "Waiting..." },
};

/** Clickable user avatar positioned at "home" in the village. */
export class UserSprite {
  sprite: Phaser.GameObjects.Sprite;
  private scene: Phaser.Scene;
  private bobTween: Phaser.Tweens.Tween | null = null;
  private currentTween: Phaser.Tweens.Tween | null = null;
  private glowGraphics: Phaser.GameObjects.Graphics | null = null;
  private statusContainer: Phaser.GameObjects.Container | null = null;
  private statusBg: Phaser.GameObjects.Graphics | null = null;
  private statusText: Phaser.GameObjects.Text | null = null;
  private statusPulseTween: Phaser.Tweens.Tween | null = null;
  private currentAmbientState: string = "idle";
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

  /** Show or hide the ambient state bubble above the avatar. */
  setAmbientState(state: string, _tooltip?: string) {
    if (state === this.currentAmbientState) return;
    this.currentAmbientState = state;

    // Hide for idle or unknown states
    const visuals = STATE_VISUALS[state];
    if (!visuals) {
      this.hideStatusBubble();
      return;
    }

    this.showStatusBubble(visuals.color, visuals.label, state);
  }

  private showStatusBubble(color: number, label: string, state: string) {
    // Create container lazily
    if (!this.statusContainer) {
      this.statusBg = this.scene.add.graphics();
      this.statusText = this.scene.add.text(0, 0, "", {
        fontFamily: '"Press Start 2P"',
        fontSize: `${STATUS_FONT_SIZE}px`,
        color: "#ffffff",
        stroke: "#000000",
        strokeThickness: 2,
      });
      this.statusText.setOrigin(0, 0);
      this.statusContainer = this.scene.add.container(0, 0, [
        this.statusBg,
        this.statusText,
      ]);
      this.statusContainer.setDepth(21);
    }

    // Update text
    this.statusText!.setText(label);
    const textW = this.statusText!.width;
    const textH = this.statusText!.height;
    const bubbleW = textW + STATUS_PADDING * 2;
    const bubbleH = textH + STATUS_PADDING * 2;

    // Draw background
    this.statusBg!.clear();

    // Shadow
    this.statusBg!.fillStyle(0x000000, 0.3);
    this.statusBg!.fillRoundedRect(1, 1, bubbleW, bubbleH, 3);

    // Colored background
    this.statusBg!.fillStyle(color, 0.9);
    this.statusBg!.fillRoundedRect(0, 0, bubbleW, bubbleH, 3);

    // Border
    this.statusBg!.lineStyle(1, 0xffffff, 0.5);
    this.statusBg!.strokeRoundedRect(0, 0, bubbleW, bubbleH, 3);

    // Tail
    this.statusBg!.fillStyle(color, 0.9);
    this.statusBg!.fillTriangle(
      bubbleW / 2 - STATUS_TAIL,
      bubbleH,
      bubbleW / 2 + STATUS_TAIL,
      bubbleH,
      bubbleW / 2,
      bubbleH + STATUS_TAIL,
    );

    this.statusText!.setPosition(STATUS_PADDING, STATUS_PADDING);
    this.statusContainer.setSize(bubbleW, bubbleH + STATUS_TAIL);
    this.statusContainer.setVisible(true);
    this.statusContainer.setAlpha(1);

    // Position above sprite
    this.updateStatusPosition();

    // Pulse animation for attention-drawing states
    if (this.statusPulseTween) {
      this.statusPulseTween.stop();
      this.statusPulseTween = null;
    }
    const shouldPulse = state === "goal_behind" || state === "has_insight" || state === "waiting";
    if (shouldPulse) {
      this.statusPulseTween = this.scene.tweens.add({
        targets: this.statusContainer,
        scaleX: 1.08,
        scaleY: 1.08,
        duration: 800,
        yoyo: true,
        repeat: -1,
        ease: "Sine.easeInOut",
      });
    }
  }

  private hideStatusBubble() {
    if (this.statusPulseTween) {
      this.statusPulseTween.stop();
      this.statusPulseTween = null;
    }
    if (this.statusContainer) {
      this.statusContainer.setVisible(false);
      this.statusContainer.setScale(1);
    }
  }

  /** Call from scene update() to keep the bubble tracking the sprite. */
  updateStatusPosition() {
    if (!this.statusContainer || !this.statusContainer.visible) return;
    const bubbleW = this.statusContainer.width || 60;
    const bubbleH = this.statusContainer.height || 20;
    this.statusContainer.setPosition(
      this.sprite.x - bubbleW / 2,
      this.sprite.y - this.sprite.displayHeight - bubbleH - 2,
    );
  }

  destroy() {
    this.stopBob();
    if (this.currentTween) this.currentTween.stop();
    if (this.statusPulseTween) this.statusPulseTween.stop();
    this.statusContainer?.destroy();
    this.glowGraphics?.destroy();
    this.sprite.destroy();
  }
}
