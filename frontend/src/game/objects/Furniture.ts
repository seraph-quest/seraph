import Phaser from "phaser";
import { SCENE } from "../../config/constants";

export class Furniture {
  constructor(scene: Phaser.Scene) {
    this.drawComputerDesk(scene);
    this.drawMainDesk(scene);
    this.drawFilingCabinet(scene);
    this.drawDoor(scene);
  }

  private drawComputerDesk(scene: Phaser.Scene) {
    const g = scene.add.graphics();
    const { COLORS } = SCENE;
    const bx = 83; // base X for desk group
    const by = SCENE.FLOOR_Y + 4;

    // Desk surface
    g.fillStyle(COLORS.deskSurface);
    g.fillRect(bx, by, 64, 8);

    // Desk legs
    g.fillStyle(COLORS.deskLegs);
    g.fillRect(bx + 4, by + 8, 6, 24);
    g.fillRect(bx + 54, by + 8, 6, 24);

    // Monitor body
    g.fillStyle(0x2a2a3e);
    g.fillRect(bx + 14, by - 28, 36, 26);

    // Monitor screen
    g.fillStyle(COLORS.monitorScreen);
    g.fillRect(bx + 17, by - 25, 30, 20);

    // Screen text lines (green)
    g.fillStyle(COLORS.monitorGreen, 0.7);
    g.fillRect(bx + 20, by - 22, 20, 2);
    g.fillRect(bx + 20, by - 18, 16, 2);
    g.fillRect(bx + 20, by - 14, 22, 2);
    g.fillRect(bx + 20, by - 10, 12, 2);

    // Monitor stand
    g.fillStyle(0x2a2a3e);
    g.fillRect(bx + 28, by - 2, 8, 4);

    // Keyboard
    g.fillStyle(0x4a4a5e);
    g.fillRect(bx + 18, by + 1, 28, 5);

    // Key details
    g.fillStyle(0x5a5a6e);
    g.fillRect(bx + 20, by + 2, 4, 3);
    g.fillRect(bx + 26, by + 2, 4, 3);
    g.fillRect(bx + 32, by + 2, 4, 3);
    g.fillRect(bx + 38, by + 2, 4, 3);
  }

  private drawMainDesk(scene: Phaser.Scene) {
    const g = scene.add.graphics();
    const { COLORS } = SCENE;
    const bx = 352; // center desk
    const by = SCENE.FLOOR_Y + 4;

    // Desk surface
    g.fillStyle(COLORS.deskSurface);
    g.fillRect(bx, by, 64, 8);

    // Desk legs
    g.fillStyle(COLORS.deskLegs);
    g.fillRect(bx + 4, by + 8, 6, 24);
    g.fillRect(bx + 54, by + 8, 6, 24);

    // Paper
    g.fillStyle(COLORS.paper);
    g.fillRect(bx + 14, by - 2, 20, 14);

    // Paper lines
    g.fillStyle(0xaaaaaa, 0.5);
    g.fillRect(bx + 17, by + 2, 14, 1);
    g.fillRect(bx + 17, by + 5, 12, 1);
    g.fillRect(bx + 17, by + 8, 10, 1);

    // Pen
    g.fillStyle(COLORS.pen);
    g.fillRect(bx + 40, by - 1, 2, 12);
  }

  private drawFilingCabinet(scene: Phaser.Scene) {
    const g = scene.add.graphics();
    const { COLORS } = SCENE;
    const bx = 629;
    const by = SCENE.FLOOR_Y - 16;

    // Cabinet body
    g.fillStyle(COLORS.cabinetBody);
    g.fillRect(bx, by, 48, 56);

    // Cabinet border
    g.lineStyle(1, 0x3a3a4e);
    g.strokeRect(bx, by, 48, 56);

    // Drawers
    for (let i = 0; i < 3; i++) {
      const dy = by + 4 + i * 17;
      g.fillStyle(COLORS.cabinetDrawer);
      g.fillRect(bx + 4, dy, 40, 14);

      // Drawer handle
      g.fillStyle(COLORS.cabinetHandle);
      g.fillRect(bx + 20, dy + 5, 8, 3);
    }

    // Feet
    g.fillStyle(COLORS.deskLegs);
    g.fillRect(bx + 4, by + 56, 8, 4);
    g.fillRect(bx + 36, by + 56, 8, 4);
  }

  private drawDoor(scene: Phaser.Scene) {
    const g = scene.add.graphics();
    const { COLORS } = SCENE;
    const bx = 360;
    const by = SCENE.FLOOR_Y + 38;

    // Door frame
    g.fillStyle(COLORS.doorFrame);
    g.fillRect(bx, by, 48, 56);

    // Door body
    g.fillStyle(COLORS.doorBody);
    g.fillRect(bx + 4, by + 4, 40, 48);

    // Door knob
    g.fillStyle(COLORS.doorKnob);
    g.fillCircle(bx + 36, by + 28, 3);
  }
}
