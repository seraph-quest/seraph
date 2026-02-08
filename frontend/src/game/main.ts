import Phaser from "phaser";
import { StudyScene } from "./scenes/StudyScene";

export function StartGame(parent: string): Phaser.Game {
  return new Phaser.Game({
    type: Phaser.AUTO,
    parent,
    pixelArt: true,
    backgroundColor: "#4a8c3f",
    scale: {
      mode: Phaser.Scale.RESIZE,
      width: "100%",
      height: "100%",
      autoCenter: Phaser.Scale.NO_CENTER,
    },
    scene: [StudyScene],
  });
}
