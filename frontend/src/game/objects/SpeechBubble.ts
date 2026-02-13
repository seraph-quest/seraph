import Phaser from "phaser";
import { SCENE } from "../../config/constants";

// Dimensions in world pixels (scaled by camera zoom like sprites/tiles)
const PADDING = 4;
const TAIL_SIZE = 3;
const MAX_WIDTH = 80;
const FONT_SIZE = 5;
const MAX_CHARS = 60;
const MIN_DISPLAY_MS = 2000;

export class SpeechBubble {
  private container: Phaser.GameObjects.Container;
  private bg: Phaser.GameObjects.Graphics;
  private text: Phaser.GameObjects.Text;
  private scene: Phaser.Scene;
  private targetSprite: Phaser.GameObjects.Sprite | null = null;
  private clampWidth: number;
  private clampOffsetX: number;

  private queue: string[] = [];
  private displayTimer: Phaser.Time.TimerEvent | null = null;

  constructor(scene: Phaser.Scene, clampWidth?: number, clampOffsetX?: number) {
    this.scene = scene;
    this.clampWidth = clampWidth ?? scene.scale.width;
    this.clampOffsetX = clampOffsetX ?? 0;

    this.bg = scene.add.graphics();
    this.text = scene.add.text(0, 0, "", {
      fontFamily: '"Press Start 2P"',
      fontSize: `${FONT_SIZE}px`,
      color: `#${SCENE.COLORS.bubbleText.toString(16).padStart(6, "0")}`,
      wordWrap: { width: MAX_WIDTH - PADDING * 2, useAdvancedWrap: true },
      lineSpacing: 2,
      resolution: 2,
    });
    this.text.setOrigin(0, 0);

    this.container = scene.add.container(0, 0, [this.bg, this.text]);
    this.container.setDepth(20);
    this.container.setAlpha(0);
    this.container.setVisible(false);
  }

  setTarget(sprite: Phaser.GameObjects.Sprite) {
    this.targetSprite = sprite;
  }

  updateClampBounds(width: number, offsetX: number) {
    this.clampWidth = width;
    this.clampOffsetX = offsetX;
  }

  /** Queue a message. Displays immediately if nothing is showing. */
  show(message: string) {
    const truncated =
      message.length > MAX_CHARS ? message.slice(0, MAX_CHARS) + "..." : message;

    this.queue.push(truncated);

    if (!this.displayTimer) {
      this.displayNext();
    }
  }

  /** Clear the queue and fade out. */
  hide() {
    this.queue = [];
    if (this.displayTimer) {
      this.displayTimer.remove(false);
      this.displayTimer = null;
    }
    this.scene.tweens.killTweensOf(this.container);
    this.scene.tweens.add({
      targets: this.container,
      alpha: 0,
      duration: 200,
      ease: "Sine.easeIn",
      onComplete: () => {
        this.container.setVisible(false);
      },
    });
  }

  private displayNext() {
    if (this.queue.length === 0) {
      this.displayTimer = null;
      return;
    }

    const message = this.queue.shift()!;
    this.renderBubble(message);

    this.displayTimer = this.scene.time.delayedCall(MIN_DISPLAY_MS, () => {
      this.displayTimer = null;
      this.displayNext();
    });
  }

  private renderBubble(message: string) {
    this.scene.tweens.killTweensOf(this.container);

    this.text.setText(message);

    const innerW = MAX_WIDTH - PADDING * 2;
    const textW = Math.min(this.text.width, innerW);
    const textH = this.text.height;
    const bubbleW = textW + PADDING * 2;
    const bubbleH = textH + PADDING * 2;

    this.bg.clear();

    // Shadow
    this.bg.fillStyle(0x000000, 0.3);
    this.bg.fillRoundedRect(1, 1, bubbleW, bubbleH, 3);

    // Background fill
    this.bg.fillStyle(SCENE.COLORS.bubbleBg);
    this.bg.fillRoundedRect(0, 0, bubbleW, bubbleH, 3);

    // Border
    this.bg.lineStyle(1, SCENE.COLORS.bubbleBorder);
    this.bg.strokeRoundedRect(0, 0, bubbleW, bubbleH, 3);

    // Tail triangle
    this.bg.fillStyle(SCENE.COLORS.bubbleBg);
    this.bg.fillTriangle(
      bubbleW / 2 - TAIL_SIZE,
      bubbleH,
      bubbleW / 2 + TAIL_SIZE,
      bubbleH,
      bubbleW / 2,
      bubbleH + TAIL_SIZE
    );
    this.bg.lineStyle(1, SCENE.COLORS.bubbleBorder);
    this.bg.lineBetween(
      bubbleW / 2 - TAIL_SIZE,
      bubbleH,
      bubbleW / 2,
      bubbleH + TAIL_SIZE
    );
    this.bg.lineBetween(
      bubbleW / 2 + TAIL_SIZE,
      bubbleH,
      bubbleW / 2,
      bubbleH + TAIL_SIZE
    );

    this.text.setPosition(PADDING, PADDING);

    this.container.setSize(bubbleW, bubbleH + TAIL_SIZE);
    this.container.setVisible(true);
    this.updatePosition();

    // If already visible, just swap content
    if (this.container.alpha >= 0.9) {
      this.container.setAlpha(1);
      return;
    }

    // Fade in
    this.container.setAlpha(0);
    this.scene.tweens.add({
      targets: this.container,
      alpha: 1,
      duration: 150,
      ease: "Sine.easeOut",
    });
  }

  updatePosition() {
    if (!this.targetSprite || !this.container.visible) return;

    const bubbleW = this.container.width || 100;
    const bubbleH = this.container.height || 40;

    let cx = this.targetSprite.x - bubbleW / 2;
    const cy = this.targetSprite.y - this.targetSprite.displayHeight - bubbleH - 4;

    const minX = this.clampOffsetX + 4;
    const maxX = this.clampOffsetX + this.clampWidth - bubbleW - 4;
    cx = Phaser.Math.Clamp(cx, minX, maxX);

    this.container.setPosition(cx, Math.max(4, cy));
  }

  destroy() {
    if (this.displayTimer) {
      this.displayTimer.remove(false);
      this.displayTimer = null;
    }
    this.queue = [];
    this.container.destroy();
  }
}
