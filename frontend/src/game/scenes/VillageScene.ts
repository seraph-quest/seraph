import Phaser from "phaser";
import { EventBus } from "../EventBus";
import { AgentSprite } from "../objects/AgentSprite";
import { MagicEffect } from "../objects/MagicEffect";
import { SpeechBubble } from "../objects/SpeechBubble";
import { UserSprite } from "../objects/UserSprite";
import { Pathfinder } from "../lib/Pathfinder";
import { SCENE } from "../../config/constants";
import type { MagicEffectDef } from "../../types";

interface CastEffectPayload {
  tool: string;
  effectIndex: number;
  text: string;
}

interface FinalAnswerPayload {
  text: string;
}

/** Tileset file names to load */
const TILESET_FILES = [
  "CuteRPG_Field_Tiles",
  "CuteRPG_Village",
  "CuteRPG_Forest",
  "CuteRPG_Houses_A",
  "CuteRPG_Houses_B",
  "CuteRPG_Houses_C",
  "CuteRPG_Harbor",
];

/** Layer depth mapping */
const LAYER_DEPTHS: Record<string, number> = {
  ground: 0,
  terrain: 1,
  buildings: 2,
  decorations: 3,
  treetops: 20,
};

export class VillageScene extends Phaser.Scene {
  private agent!: AgentSprite;
  private userAvatar!: UserSprite;
  private speechBubble!: SpeechBubble;
  private label!: Phaser.GameObjects.Text;
  private pathfinder!: Pathfinder;

  // Magic effect pool
  private magicEffectPool: MagicEffectDef[] = [];
  private activeMagicEffect: MagicEffect | null = null;

  // Spawn points
  private agentSpawn = { x: 512, y: 350 };
  private userSpawn = { x: 832, y: 340 };

  // Wander zone
  private wanderZone: { x: number; y: number; width: number; height: number } | null = null;

  private tooltip!: Phaser.GameObjects.Container;
  private tooltipText!: Phaser.GameObjects.Text;
  private tooltipBg!: Phaser.GameObjects.Rectangle;

  private isWandering = false;
  private wanderTimer: Phaser.Time.TimerEvent | null = null;
  private nudgeTimer: Phaser.Time.TimerEvent | null = null;
  private resizeDebounceTimer: ReturnType<typeof setTimeout> | null = null;

  // EventBus handler references
  private handleThink!: () => void;
  private handleCastEffect!: (payload: CastEffectPayload) => void;
  private handleFinalAnswer!: (payload: FinalAnswerPayload) => void;
  private handleReturnIdle!: () => void;
  private handleNudge!: (payload: { text: string }) => void;
  private handleAmbientState!: (payload: { state: string; tooltip: string }) => void;

  constructor() {
    super("VillageScene");
  }

  // ─── Preload ─────────────────────────────────────────

  preload() {
    // Load Tiled JSON map
    this.load.tilemapTiledJSON("village-map", SCENE.MAP_FILE);

    // Load tileset images
    for (const name of TILESET_FILES) {
      this.load.image(name, `assets/tilesets/${name}.png`);
    }

    // Load character sprites
    AgentSprite.preload(this);
    UserSprite.preload(this);

    this.load.on("loaderror", (_file: { key: string }) => {
      // silently ignore missing assets in dev
    });
  }

  // ─── Create ──────────────────────────────────────────

  create() {
    // Build tilemap
    const map = this.make.tilemap({ key: "village-map" });

    // Add tilesets
    const tilesets: Phaser.Tilemaps.Tileset[] = [];
    for (const name of TILESET_FILES) {
      const ts = map.addTilesetImage(name, name);
      if (ts) tilesets.push(ts);
    }

    // Create tile layers
    for (const layerData of map.layers) {
      const layer = map.createLayer(layerData.name, tilesets);
      if (layer) {
        const depth = LAYER_DEPTHS[layerData.name] ?? 0;
        layer.setDepth(depth);
      }
    }

    // Build collision grid from tile properties
    this.buildCollisionGrid(map);

    // Parse object layer
    this.parseObjectLayer(map);

    // Parse magic effects from map custom properties
    this.parseMagicEffects(map);

    // Camera setup
    this.cameras.main.setBounds(0, 0, map.widthInPixels, map.heightInPixels);

    // Center the map in the viewport
    const canvasW = this.scale.width;
    const canvasH = this.scale.height;
    if (map.widthInPixels < canvasW || map.heightInPixels < canvasH) {
      // Map is smaller than canvas — center it
      this.cameras.main.centerOn(map.widthInPixels / 2, map.heightInPixels / 2);
    } else {
      this.cameras.main.centerOn(map.widthInPixels / 2, map.heightInPixels / 2);
    }

    // Background color
    this.cameras.main.setBackgroundColor("#4a8c3f");

    // ─── Tooltip ─────────────────────────────────────
    this.tooltipText = this.add.text(0, 0, "", {
      fontFamily: '"Press Start 2P"',
      fontSize: "7px",
      color: "#ffffff",
      padding: { x: 0, y: 0 },
    });
    this.tooltipText.setOrigin(0.5, 0.5);
    this.tooltipBg = this.add.rectangle(0, 0, 1, 1, 0x000000, 0.75);
    this.tooltipBg.setOrigin(0.5, 0.5);
    this.tooltip = this.add.container(0, 0, [this.tooltipBg, this.tooltipText]);
    this.tooltip.setDepth(100);
    this.tooltip.setVisible(false);

    // ─── Agent (Seraph) ──────────────────────────────
    this.agent = new AgentSprite(this, this.agentSpawn.x, this.agentSpawn.y);
    this.agent.sprite.setInteractive({ useHandCursor: true });
    this.agent.sprite.on("pointerdown", () => {
      EventBus.emit("toggle-chat");
    });
    this.attachTooltip(this.agent.sprite, "Seraph");

    // ─── User Avatar ─────────────────────────────────
    this.userAvatar = new UserSprite(this, this.userSpawn.x, this.userSpawn.y);
    this.attachTooltip(this.userAvatar.sprite, "You");

    // ─── Speech Bubble ───────────────────────────────
    this.speechBubble = new SpeechBubble(
      this,
      map.widthInPixels,
      0
    );
    this.speechBubble.setTarget(this.agent.sprite);

    // ─── Village Label ───────────────────────────────
    this.label = this.add.text(map.widthInPixels / 2, 12, "Seraph's Village", {
      fontFamily: '"Press Start 2P"',
      fontSize: "8px",
      color: "#ffffff",
      stroke: "#000000",
      strokeThickness: 3,
    });
    this.label.setOrigin(0.5, 0);
    this.label.setDepth(25);

    // ─── EventBus handlers ───────────────────────────
    this.handleThink = () => {
      this.stopWandering();
      this.speechBubble.hide();
      this.agent.moveAlongPath(this.pathfinder, this.agentSpawn.x, this.agentSpawn.y, () => {
        this.agent.playAnim("think");
      });

      // User walks toward agent
      const userTarget = { x: this.agentSpawn.x + 40, y: this.agentSpawn.y + 10 };
      this.userAvatar.moveAlongPath(this.pathfinder, userTarget.x, userTarget.y, () => {
        this.userAvatar.sprite.play("user-idle");
      });
    };

    this.handleCastEffect = (payload: CastEffectPayload) => {
      this.stopWandering();
      this.speechBubble.hide();
      this.clearMagicEffect();

      // Stay in place, play think animation
      this.agent.playAnim("think");

      // Spawn magic effect overlay if pool has entries
      if (this.magicEffectPool.length > 0) {
        const idx = payload.effectIndex % this.magicEffectPool.length;
        const def = this.magicEffectPool[idx];
        this.activeMagicEffect = new MagicEffect(
          this,
          def,
          this.agent.sprite.x,
          this.agent.sprite.y
        );
      }

      if (payload.text) {
        this.speechBubble.show(payload.text);
      }
    };

    this.handleFinalAnswer = (payload: FinalAnswerPayload) => {
      this.stopWandering();
      this.fadeOutMagicEffect();
      const targetX = this.userAvatar.sprite.x - 40;
      const targetY = this.userAvatar.sprite.y;
      this.agent.moveAlongPath(this.pathfinder, targetX, targetY, () => {
        this.agent.playAnim("idle");
        this.speechBubble.show(payload.text);

        this.time.delayedCall(6000, () => {
          this.speechBubble.hide();
          EventBus.emit("agent-speech-done");
          this.userAvatar.returnHome();
          this.startWandering();
        });
      });
    };

    this.handleReturnIdle = () => {
      this.speechBubble.hide();
      this.fadeOutMagicEffect();
      this.agent.moveAlongPath(this.pathfinder, this.agentSpawn.x, this.agentSpawn.y, () => {
        this.agent.playAnim("idle");
        this.startWandering();
      });
      this.userAvatar.returnHome();
    };

    this.handleNudge = (payload: { text: string }) => {
      if (this.nudgeTimer) {
        this.nudgeTimer.remove(false);
        this.nudgeTimer = null;
      }
      this.speechBubble.show(payload.text);
      this.nudgeTimer = this.time.delayedCall(5000, () => {
        this.speechBubble.hide();
        this.nudgeTimer = null;
      });
    };

    this.handleAmbientState = (payload: { state: string; tooltip: string }) => {
      this.userAvatar.setAmbientState(payload.state, payload.tooltip);
    };

    EventBus.on("agent-think", this.handleThink);
    EventBus.on("agent-cast-effect", this.handleCastEffect);
    EventBus.on("agent-final-answer", this.handleFinalAnswer);
    EventBus.on("agent-return-idle", this.handleReturnIdle);
    EventBus.on("agent-nudge", this.handleNudge);
    EventBus.on("agent-ambient-state", this.handleAmbientState);

    this.scale.on("resize", this.onResize, this);

    this.startWandering();
    EventBus.emit("current-scene-ready", this);
  }

  update() {
    // Y-sort characters: depth = 5 + normalized y
    if (this.agent) {
      this.agent.sprite.setDepth(5 + this.agent.sprite.y * 0.001);
    }
    if (this.userAvatar) {
      this.userAvatar.sprite.setDepth(5 + this.userAvatar.sprite.y * 0.001);
    }

    if (this.activeMagicEffect) {
      this.activeMagicEffect.setPosition(this.agent.sprite.x, this.agent.sprite.y);
    }

    this.speechBubble.updatePosition();
    this.userAvatar.updateStatusPosition();
  }

  shutdown() {
    EventBus.off("agent-think", this.handleThink);
    EventBus.off("agent-cast-effect", this.handleCastEffect);
    EventBus.off("agent-final-answer", this.handleFinalAnswer);
    EventBus.off("agent-return-idle", this.handleReturnIdle);
    EventBus.off("agent-nudge", this.handleNudge);
    EventBus.off("agent-ambient-state", this.handleAmbientState);

    this.scale.off("resize", this.onResize, this);
    this.stopWandering();
    this.clearMagicEffect();
    this.userAvatar.cancelMovement();

    if (this.resizeDebounceTimer) clearTimeout(this.resizeDebounceTimer);
    if (this.nudgeTimer) this.nudgeTimer.remove(false);

    this.agent.destroy();
    this.userAvatar.destroy();
    this.speechBubble.destroy();
  }

  // ─── Collision Grid ────────────────────────────────

  private buildCollisionGrid(map: Phaser.Tilemaps.Tilemap) {
    const grid: number[][] = [];

    for (let row = 0; row < map.height; row++) {
      const gridRow: number[] = [];
      for (let col = 0; col < map.width; col++) {
        let walkable = true;

        // Check all tile layers
        for (const layerData of map.layers) {
          const tile = map.getTileAt(col, row, false, layerData.name);
          if (tile) {
            // Check tile property
            const tileWalkable = tile.properties?.walkable;
            if (tileWalkable === false) {
              walkable = false;
              break;
            }
            // Buildings and treetops layers are blocked by default if tile exists
            if (
              (layerData.name === "buildings" || layerData.name === "treetops") &&
              tileWalkable === undefined
            ) {
              walkable = false;
              break;
            }
          }
        }

        gridRow.push(walkable ? 0 : 1);
      }
      grid.push(gridRow);
    }

    this.pathfinder = new Pathfinder(grid, map.width, map.height, map.tileWidth);
  }

  // ─── Object Layer Parsing ─────────────────────────

  private parseObjectLayer(map: Phaser.Tilemaps.Tilemap) {
    const objectLayer = map.getObjectLayer("objects");
    if (!objectLayer) return;

    for (const obj of objectLayer.objects) {
      const props = obj.properties as Array<{ name: string; value: unknown }> | undefined;
      const getProp = (name: string) => props?.find((p) => p.name === name)?.value;

      if (obj.type === "spawn_point") {
        if (obj.name === "agent_spawn") {
          this.agentSpawn = { x: obj.x!, y: obj.y! };
        } else if (obj.name === "user_spawn") {
          this.userSpawn = { x: obj.x!, y: obj.y! };
        }
      } else if (obj.type === "wander_zone") {
        this.wanderZone = {
          x: obj.x!,
          y: obj.y!,
          width: obj.width!,
          height: obj.height!,
        };
      }
    }
  }

  // ─── Tooltip ───────────────────────────────────────

  private attachTooltip(
    obj: Phaser.GameObjects.Image | Phaser.GameObjects.Sprite,
    label: string
  ) {
    if (!obj.input) {
      obj.setInteractive({ useHandCursor: true });
    }
    obj.on("pointerover", () => {
      this.tooltipText.setText(label);
      const pad = 6;
      this.tooltipBg.setSize(
        this.tooltipText.width + pad * 2,
        this.tooltipText.height + pad * 2
      );
      this.tooltip.setPosition(obj.x, obj.y - obj.displayHeight - 8);
      this.tooltip.setVisible(true);
    });
    obj.on("pointerout", () => {
      this.tooltip.setVisible(false);
    });
  }

  // ─── Resize Handler ───────────────────────────────

  private onResize(_gameSize: Phaser.Structs.Size) {
    if (this.resizeDebounceTimer) clearTimeout(this.resizeDebounceTimer);
    this.resizeDebounceTimer = setTimeout(() => {
      // Camera auto-adjusts with bounds — just re-center label
      this.label.setX(this.cameras.main.midPoint.x);
    }, 100);
  }

  // ─── Magic Effects ──────────────────────────────────

  private parseMagicEffects(map: Phaser.Tilemaps.Tilemap) {
    // Read magic_effects from map custom properties
    const props = (map as unknown as { properties?: Array<{ name: string; value: unknown }> }).properties;
    if (!props) return;

    const magicProp = props.find((p) => p.name === "magic_effects");
    if (!magicProp || typeof magicProp.value !== "string") return;

    try {
      const effects = JSON.parse(magicProp.value) as Array<{
        id: string;
        name: string;
        tilesetName: string;
        tileWidth: number;
        tileHeight: number;
        columns: number;
        frameDuration: number;
        entries: Array<{ anchorLocalId: number; frames: number[] }>;
      }>;

      // Collect unique tilesets needed and load them as spritesheets
      const tilesetKeys = new Set<string>();
      for (const fx of effects) {
        if (!tilesetKeys.has(fx.tilesetName)) {
          tilesetKeys.add(fx.tilesetName);
          // Load spritesheet if not already loaded
          if (!this.textures.exists(fx.tilesetName)) {
            // Determine the asset directory based on naming convention
            const assetPath = `assets/animations/${fx.tilesetName}.png`;
            this.load.spritesheet(fx.tilesetName, assetPath, {
              frameWidth: fx.tileWidth,
              frameHeight: fx.tileHeight,
            });
          }
        }
      }

      // Convert to MagicEffectDef array (one per entry, using first entry's frames)
      for (const fx of effects) {
        if (fx.entries.length === 0) continue;
        // Use the first entry's frames as the animation sequence
        this.magicEffectPool.push({
          id: fx.id,
          name: fx.name,
          tilesetKey: fx.tilesetName,
          tileWidth: fx.tileWidth,
          tileHeight: fx.tileHeight,
          columns: fx.columns,
          frameDuration: fx.frameDuration,
          frames: fx.entries[0].frames,
        });
      }

      // If we need to load new spritesheets, start the loader
      if (tilesetKeys.size > 0) {
        this.load.once("complete", () => {
          EventBus.emit("magic-effects-loaded", this.magicEffectPool.length);
        });
        this.load.start();
      } else {
        EventBus.emit("magic-effects-loaded", this.magicEffectPool.length);
      }
    } catch {
      // Ignore malformed magic_effects
    }
  }

  private clearMagicEffect() {
    if (this.activeMagicEffect) {
      this.activeMagicEffect.destroy();
      this.activeMagicEffect = null;
    }
  }

  private fadeOutMagicEffect() {
    if (this.activeMagicEffect) {
      const effect = this.activeMagicEffect;
      this.activeMagicEffect = null;
      effect.fadeOut();
    }
  }

  // ─── Wandering System ─────────────────────────────

  private startWandering() {
    if (this.isWandering) return;
    this.isWandering = true;
    this.scheduleNextWander();
  }

  private stopWandering() {
    this.isWandering = false;
    if (this.wanderTimer) {
      this.wanderTimer.remove(false);
      this.wanderTimer = null;
    }
    this.agent.cancelMovement();
  }

  private scheduleNextWander() {
    if (!this.isWandering) return;

    const delay = Phaser.Math.Between(
      SCENE.WANDERING.MIN_DELAY_MS,
      SCENE.WANDERING.MAX_DELAY_MS
    );

    this.wanderTimer = this.time.delayedCall(delay, () => {
      this.wanderToRandomPoint();
    });
  }

  private wanderToRandomPoint() {
    if (!this.isWandering || !this.pathfinder) return;

    const target = this.pathfinder.getRandomWalkableTile(
      this.wanderZone ?? undefined
    );
    if (!target) {
      this.scheduleNextWander();
      return;
    }

    this.agent.moveAlongPath(this.pathfinder, target.x, target.y, () => {
      this.agent.playAnim("idle");
      this.scheduleNextWander();
    });
  }
}
