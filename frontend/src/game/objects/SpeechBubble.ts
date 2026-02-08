import Phaser from "phaser";
import { SCENE } from "../../config/constants";

const PADDING = 10;
const TAIL_SIZE = 6;
const MAX_WIDTH = 220;
const FONT_SIZE = 8;

export class SpeechBubble {
  private container: Phaser.GameObjects.Container;
  private bg: Phaser.GameObjects.Graphics;
  private text: Phaser.GameObjects.Text;
  private scene: Phaser.Scene;
  private targetSprite: Phaser.GameObjects.Sprite | null = null;
  private clampWidth: number;
  private clampOffsetX: number;

  constructor(scene: Phaser.Scene, clampWidth?: number, clampOffsetX?: number) {
    this.scene = scene;
    this.clampWidth = clampWidth ?? scene.scale.width;
    this.clampOffsetX = clampOffsetX ?? 0;

    this.bg = scene.add.graphics();
    this.text = scene.add.text(0, 0, "", {
      fontFamily: '"Press Start 2P"',
      fontSize: `${FONT_SIZE}px`,
      color: `#${SCENE.COLORS.bubbleText.toString(16).padStart(6, "0")}`,
      wordWrap: { width: MAX_WIDTH - PADDING * 2 },
      lineSpacing: 4,
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

  show(message: string) {
    const truncated =
      message.length > 80 ? message.slice(0, 80) + "..." : message;

    this.text.setText(truncated);

    // Measure text bounds
    const textW = Math.min(this.text.width, MAX_WIDTH - PADDING * 2);
    const textH = this.text.height;
    const bubbleW = textW + PADDING * 2;
    const bubbleH = textH + PADDING * 2;

    // Draw bubble background
    this.bg.clear();

    // Shadow
    this.bg.fillStyle(0x000000, 0.3);
    this.bg.fillRoundedRect(2, 2, bubbleW, bubbleH, 4);

    // Background fill
    this.bg.fillStyle(SCENE.COLORS.bubbleBg);
    this.bg.fillRoundedRect(0, 0, bubbleW, bubbleH, 4);

    // Border
    this.bg.lineStyle(2, SCENE.COLORS.bubbleBorder);
    this.bg.strokeRoundedRect(0, 0, bubbleW, bubbleH, 4);

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
    this.bg.lineStyle(2, SCENE.COLORS.bubbleBorder);
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

    // Center bubble over target
    this.container.setSize(bubbleW, bubbleH + TAIL_SIZE);
    this.container.setVisible(true);
    this.updatePosition();

    // Animate in
    this.container.setAlpha(0);
    this.container.setScale(0.8);
    this.scene.tweens.add({
      targets: this.container,
      alpha: 1,
      scaleX: 1,
      scaleY: 1,
      duration: 300,
      ease: "Back.easeOut",
    });
  }

  hide() {
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

  updatePosition() {
    if (!this.targetSprite || !this.container.visible) return;

    const bubbleW = this.container.width || 100;
    const bubbleH = this.container.height || 40;

    let cx = this.targetSprite.x - bubbleW / 2;
    const cy = this.targetSprite.y - this.targetSprite.displayHeight - bubbleH - 4;

    // Clamp to canvas edges using dynamic bounds
    const minX = this.clampOffsetX + 4;
    const maxX = this.clampOffsetX + this.clampWidth - bubbleW - 4;
    cx = Phaser.Math.Clamp(cx, minX, maxX);

    this.container.setPosition(cx, Math.max(4, cy));
  }

  destroy() {
    this.container.destroy();
  }
}
