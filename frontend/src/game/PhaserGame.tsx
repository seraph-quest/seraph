import { forwardRef, useLayoutEffect, useRef } from "react";
import { StartGame } from "./main";
import { EventBus } from "./EventBus";

export interface IRefPhaserGame {
  game: Phaser.Game | null;
  scene: Phaser.Scene | null;
}

export const PhaserGame = forwardRef<IRefPhaserGame>(function PhaserGame(
  _props,
  ref
) {
  const game = useRef<Phaser.Game | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    if (game.current !== null) return;

    game.current = StartGame(containerRef.current!.id);

    const onSceneReady = (scene: Phaser.Scene) => {
      if (typeof ref === "function") {
        ref({ game: game.current, scene });
      } else if (ref) {
        ref.current = { game: game.current, scene };
      }
    };

    EventBus.on("current-scene-ready", onSceneReady);

    return () => {
      EventBus.off("current-scene-ready", onSceneReady);
      if (game.current) {
        game.current.destroy(true);
        game.current = null;
      }
    };
  }, [ref]);

  return (
    <div
      ref={containerRef}
      id="phaser-game-container"
      style={{ width: "100%", height: "100%" }}
    />
  );
});
