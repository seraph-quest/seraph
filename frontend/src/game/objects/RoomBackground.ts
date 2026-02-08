import Phaser from "phaser";
import { SCENE } from "../../config/constants";

export class RoomBackground {
  private graphics: Phaser.GameObjects.Graphics;

  constructor(scene: Phaser.Scene) {
    this.graphics = scene.add.graphics();
    this.draw();

    scene.add
      .text(SCENE.WIDTH - 12, 8, "Seraph's Study", {
        fontFamily: '"Press Start 2P"',
        fontSize: "7px",
        color: "#e2b71466",
      })
      .setOrigin(1, 0);
  }

  private draw() {
    const g = this.graphics;
    const { WIDTH, HEIGHT, FLOOR_Y, COLORS } = SCENE;

    // Wall gradient (top to floor line)
    const wallSteps = 32;
    const wallStepH = FLOOR_Y / wallSteps;
    for (let i = 0; i < wallSteps; i++) {
      const t = i / wallSteps;
      const color = Phaser.Display.Color.Interpolate.ColorWithColor(
        Phaser.Display.Color.IntegerToColor(COLORS.wallTop),
        Phaser.Display.Color.IntegerToColor(COLORS.wallBottom),
        1,
        t
      );
      const hex = Phaser.Display.Color.GetColor(color.r, color.g, color.b);
      g.fillStyle(hex);
      g.fillRect(0, i * wallStepH, WIDTH, wallStepH + 1);
    }

    // Floor gradient (floor line to bottom)
    const floorH = HEIGHT - FLOOR_Y;
    const floorSteps = 16;
    const floorStepH = floorH / floorSteps;
    for (let i = 0; i < floorSteps; i++) {
      const t = i / floorSteps;
      const color = Phaser.Display.Color.Interpolate.ColorWithColor(
        Phaser.Display.Color.IntegerToColor(COLORS.floorTop),
        Phaser.Display.Color.IntegerToColor(COLORS.floorBottom),
        1,
        t
      );
      const hex = Phaser.Display.Color.GetColor(color.r, color.g, color.b);
      g.fillStyle(hex);
      g.fillRect(0, FLOOR_Y + i * floorStepH, WIDTH, floorStepH + 1);
    }

    // Floor divider line
    g.fillStyle(COLORS.floorLine, 0.3);
    g.fillRect(0, FLOOR_Y, WIDTH, 2);

    // Decorative wall dots
    const dots = [
      { x: 60, y: 80 },
      { x: 280, y: 50 },
      { x: 500, y: 70 },
      { x: 700, y: 45 },
    ];
    g.fillStyle(COLORS.wallDot, 0.4);
    for (const dot of dots) {
      g.fillRect(dot.x, dot.y, 4, 4);
    }
  }
}
