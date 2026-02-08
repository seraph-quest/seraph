import Phaser from "phaser";
import { StudyScene } from "./scenes/StudyScene";
import { SCENE } from "../config/constants";

export function StartGame(parent: string): Phaser.Game {
  return new Phaser.Game({
    type: Phaser.AUTO,
    width: SCENE.WIDTH,
    height: SCENE.HEIGHT,
    parent,
    pixelArt: true,
    backgroundColor: "#0a0a2e",
    scale: {
      mode: Phaser.Scale.FIT,
      autoCenter: Phaser.Scale.CENTER_HORIZONTALLY,
    },
    scene: [StudyScene],
  });
}
