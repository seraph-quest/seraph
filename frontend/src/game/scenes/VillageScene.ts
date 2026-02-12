import Phaser from "phaser";
import { EventBus } from "../EventBus";
import { AgentSprite, type SpriteConfig } from "../objects/AgentSprite";
import { MagicEffect } from "../objects/MagicEffect";
import { NpcSprite } from "../objects/NpcSprite";
import { SpeechBubble } from "../objects/SpeechBubble";
import { UserSprite } from "../objects/UserSprite";
import { Pathfinder } from "../lib/Pathfinder";
import { SCENE } from "../../config/constants";
import type { MagicEffectDef } from "../../types";

/** Building types matching editor's BuildingDef (serialized in map JSON) */
interface BuildingPortalDef {
  localCol: number;
  localRow: number;
  kind: "entry" | "stairs_up" | "stairs_down";
}

interface BuildingFloorDef {
  name: string;
  layers: number[][]; // 5 layers, each zoneW * zoneH
  portals: BuildingPortalDef[];
}

interface BuildingDef {
  id: string;
  name: string;
  zoneCol: number;
  zoneRow: number;
  zoneW: number;
  zoneH: number;
  floors: BuildingFloorDef[];
}

interface CastEffectPayload {
  tool: string;
  effectIndex: number;
  text: string;
}

interface FinalAnswerPayload {
  text: string;
}

interface NpcDef {
  name: string;
  x: number;
  y: number;
  spriteSheet: string;
  spriteType: string; // "enemy" | "character"
  frameCol: number;
}

/** Tracks an animated tile definition for manual frame cycling */
interface AnimatedTileDef {
  /** All tiles sharing this animation (same layer, same base index) */
  tiles: Phaser.Tilemaps.Tile[];
  /** Animation frames: each has a global tile index and duration */
  frames: Array<{ gid: number; duration: number }>;
  /** Current frame index */
  currentFrame: number;
  /** Time elapsed on current frame (ms) */
  elapsed: number;
}

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

  // Character sheet sprite configs (parsed from spawn point properties)
  private agentSpriteConfig: (SpriteConfig & { file: string }) | null = null;
  private userSpriteConfig: (SpriteConfig & { file: string }) | null = null;

  // WASD / arrow key movement
  private cursors: Phaser.Types.Input.Keyboard.CursorKeys | null = null;
  private wasd: Record<string, Phaser.Input.Keyboard.Key> | null = null;
  private userMoving = false;
  private userMoveTween: Phaser.Tweens.Tween | null = null;

  // Wander zone
  private wanderZone: { x: number; y: number; width: number; height: number } | null = null;

  // NPCs
  private npcDefs: NpcDef[] = [];
  private npcs: NpcSprite[] = [];

  // Buildings
  private buildings: BuildingDef[] = [];
  private currentBuilding: BuildingDef | null = null;
  private currentFloor = 0;
  private interiorContainer: Phaser.GameObjects.Container | null = null;
  private savedExteriorGrid: number[][] | null = null;
  private hiddenExteriorTiles: Array<{
    layer: Phaser.Tilemaps.TilemapLayer;
    tiles: Array<{ col: number; row: number; origIndex: number }>;
  }> = [];
  private mapRef!: Phaser.Tilemaps.Tilemap;
  private tilesetsRef: Phaser.Tilemaps.Tileset[] = [];

  private tooltip!: Phaser.GameObjects.Container;
  private tooltipText!: Phaser.GameObjects.Text;
  private tooltipBg!: Phaser.GameObjects.Rectangle;

  private debugGridGraphics: Phaser.GameObjects.Graphics | null = null;
  private animatedTiles: AnimatedTileDef[] = [];

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
  private handleToggleDebugWalkability!: (on: boolean) => void;

  constructor() {
    super("VillageScene");
  }

  // ─── Preload ─────────────────────────────────────────

  preload() {
    // Load Tiled JSON map
    this.load.tilemapTiledJSON("village-map", SCENE.MAP_FILE);

    // Once map JSON is parsed, dynamically queue ALL tileset images
    this.load.on("filecomplete-tilemapJSON-village-map", () => {
      const mapData = this.cache.tilemap.get("village-map");
      if (mapData?.data?.tilesets) {
        for (const ts of mapData.data.tilesets) {
          if (ts.image) {
            // Map paths are relative to map file: "../assets/tilesets/X.png"
            // Strip leading "../" to get path relative to public/
            const imagePath = ts.image.replace(/^\.\.\//, "");
            this.load.image(ts.name, imagePath);
          }
        }
      }
    });

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

    this.mapRef = map;

    // Add tilesets (dynamically from map data)
    const tilesets: Phaser.Tilemaps.Tileset[] = [];
    for (const tsData of map.tilesets) {
      const ts = map.addTilesetImage(tsData.name, tsData.name);
      if (ts) tilesets.push(ts);
    }
    this.tilesetsRef = tilesets;

    // Create tile layers (handle sublayer names like "terrain__2")
    for (const layerData of map.layers) {
      const layer = map.createLayer(layerData.name, tilesets);
      if (layer) {
        const baseName = layerData.name.replace(/__\d+$/, "");
        const depth = LAYER_DEPTHS[baseName] ?? 0;
        layer.setDepth(depth);
      }
    }

    // Build animated tile registry for manual frame cycling
    this.initAnimatedTiles(map);

    // Build collision grid from tile properties
    this.buildCollisionGrid(map);

    // Parse object layer (reads spawn points + sprite_sheet properties)
    this.parseObjectLayer(map);

    // Queue character sheet loads if sprite_sheet was specified
    let needsSpriteLoad = false;
    const queuedKeys = new Set<string>();
    for (const cfg of [this.agentSpriteConfig, this.userSpriteConfig]) {
      if (cfg && !queuedKeys.has(cfg.key) && !this.textures.exists(cfg.key)) {
        this.load.spritesheet(cfg.key, `assets/characters/${cfg.file}`, {
          frameWidth: 24,
          frameHeight: 24,
        });
        queuedKeys.add(cfg.key);
        needsSpriteLoad = true;
      }
    }

    // Queue NPC sprite sheet loads
    for (const npcDef of this.npcDefs) {
      if (!queuedKeys.has(npcDef.spriteSheet) && !this.textures.exists(npcDef.spriteSheet)) {
        NpcSprite.loadSheet(this, npcDef.spriteSheet, npcDef.spriteType);
        queuedKeys.add(npcDef.spriteSheet);
        needsSpriteLoad = true;
      }
    }

    // Parse magic effects from map custom properties (may also queue loads + call start)
    this.parseMagicEffects(map);

    // Parse building definitions from map custom properties
    this.parseBuildings(map);

    // Camera setup
    this.cameras.main.setBounds(0, 0, map.widthInPixels, map.heightInPixels);
    this.cameras.main.centerOn(map.widthInPixels / 2, map.heightInPixels / 2);
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

    // ─── Keyboard Input ──────────────────────────────
    if (this.input.keyboard) {
      this.cursors = this.input.keyboard.createCursorKeys();
      this.wasd = this.input.keyboard.addKeys("W,A,S,D") as Record<
        string,
        Phaser.Input.Keyboard.Key
      >;
    }

    // Defer sprite creation if character sheets need loading
    if (needsSpriteLoad) {
      this.load.once("complete", () => this.initSpritesAndEvents(map));
      this.load.start();
    } else {
      this.initSpritesAndEvents(map);
    }
  }

  // ─── Deferred Sprite + Event Init ─────────────────

  private initSpritesAndEvents(map: Phaser.Tilemaps.Tilemap) {
    // Validate sprite configs — fall back to atlas if texture failed to load
    const agentCfg =
      this.agentSpriteConfig && this.textures.exists(this.agentSpriteConfig.key)
        ? { key: this.agentSpriteConfig.key, colOffset: this.agentSpriteConfig.colOffset }
        : undefined;
    const userCfg =
      this.userSpriteConfig && this.textures.exists(this.userSpriteConfig.key)
        ? { key: this.userSpriteConfig.key, colOffset: this.userSpriteConfig.colOffset }
        : undefined;

    // ─── Agent (Seraph) ──────────────────────────────
    this.agent = new AgentSprite(this, this.agentSpawn.x, this.agentSpawn.y, agentCfg);
    this.agent.sprite.setInteractive({ useHandCursor: true });
    this.agent.sprite.on("pointerdown", () => {
      EventBus.emit("toggle-chat");
    });
    this.attachTooltip(this.agent.sprite, "Seraph");

    // ─── User Avatar ─────────────────────────────────
    this.userAvatar = new UserSprite(this, this.userSpawn.x, this.userSpawn.y, userCfg);
    this.attachTooltip(this.userAvatar.sprite, "You");

    // ─── NPCs ─────────────────────────────────────────
    for (const def of this.npcDefs) {
      if (!this.textures.exists(def.spriteSheet)) continue;
      const npc = new NpcSprite(this, def.x, def.y, {
        key: def.spriteSheet,
        spriteType: def.spriteType,
        frameCol: def.frameCol,
      });
      this.npcs.push(npc);
    }

    // ─── Camera Follow ───────────────────────────────
    this.cameras.main.startFollow(this.userAvatar.sprite, true, 0.1, 0.1);

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

    this.fitCamera();

    // ─── EventBus handlers ───────────────────────────
    this.handleThink = () => {
      this.cancelUserMove();
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

    this.handleToggleDebugWalkability = (on: boolean) => {
      if (on) this.drawDebugGrid();
      else this.clearDebugGrid();
    };

    EventBus.on("agent-think", this.handleThink);
    EventBus.on("agent-cast-effect", this.handleCastEffect);
    EventBus.on("agent-final-answer", this.handleFinalAnswer);
    EventBus.on("agent-return-idle", this.handleReturnIdle);
    EventBus.on("agent-nudge", this.handleNudge);
    EventBus.on("agent-ambient-state", this.handleAmbientState);
    EventBus.on("toggle-debug-walkability", this.handleToggleDebugWalkability);

    this.scale.on("resize", this.onResize, this);

    this.startWandering();
    for (const npc of this.npcs) {
      npc.startWandering(this.pathfinder, this.wanderZone ?? undefined);
    }
    EventBus.emit("current-scene-ready", this);
  }

  update(_time: number, delta: number) {
    // Cycle animated tile frames
    this.updateAnimatedTiles(delta);

    // Y-sort characters: depth = 5 + normalized y
    if (this.agent) {
      this.agent.sprite.setDepth(5 + this.agent.sprite.y * 0.001);
    }
    if (this.userAvatar) {
      this.userAvatar.sprite.setDepth(5 + this.userAvatar.sprite.y * 0.001);

      // WASD / arrow key movement
      this.handlePlayerInput();
      this.userAvatar.updateStatusPosition();
    }

    for (const npc of this.npcs) {
      npc.sprite.setDepth(5 + npc.sprite.y * 0.001);
    }

    if (this.activeMagicEffect) {
      this.activeMagicEffect.setPosition(this.agent.sprite.x, this.agent.sprite.y);
    }

    if (this.speechBubble) this.speechBubble.updatePosition();

    // Portal detection for agent sprite
    this.checkPortalCollision();
  }

  shutdown() {
    EventBus.off("agent-think", this.handleThink);
    EventBus.off("agent-cast-effect", this.handleCastEffect);
    EventBus.off("agent-final-answer", this.handleFinalAnswer);
    EventBus.off("agent-return-idle", this.handleReturnIdle);
    EventBus.off("agent-nudge", this.handleNudge);
    EventBus.off("agent-ambient-state", this.handleAmbientState);
    EventBus.off("toggle-debug-walkability", this.handleToggleDebugWalkability);

    this.scale.off("resize", this.onResize, this);
    this.stopWandering();
    this.clearMagicEffect();
    this.clearDebugGrid();
    this.cancelUserMove();

    if (this.userAvatar) this.userAvatar.cancelMovement();
    if (this.resizeDebounceTimer) clearTimeout(this.resizeDebounceTimer);
    if (this.nudgeTimer) this.nudgeTimer.remove(false);

    for (const npc of this.npcs) npc.destroy();
    this.npcs = [];

    if (this.agent) this.agent.destroy();
    if (this.userAvatar) this.userAvatar.destroy();
    if (this.speechBubble) this.speechBubble.destroy();
  }

  // ─── Animated Tiles ─────────────────────────────────

  private initAnimatedTiles(map: Phaser.Tilemaps.Tilemap) {
    this.animatedTiles = [];

    // Build a map of base GID → animation frames for all tilesets
    // tileData stores animation keyed by local tile ID
    const animMap = new Map<number, Array<{ gid: number; duration: number }>>();

    for (const tileset of this.tilesetsRef) {
      const tileData = (tileset as unknown as { tileData: Record<string, { animation?: Array<{ tileid: number; duration: number }> }> }).tileData;
      if (!tileData) continue;

      for (const [localIdStr, data] of Object.entries(tileData)) {
        if (!data.animation || data.animation.length === 0) continue;
        const localId = parseInt(localIdStr, 10);
        const baseGid = tileset.firstgid + localId;
        const frames = data.animation.map((f) => ({
          gid: tileset.firstgid + f.tileid,
          duration: f.duration,
        }));
        animMap.set(baseGid, frames);
      }
    }

    if (animMap.size === 0) return;

    // Scan all tile layers and collect tiles whose index matches an animated base GID
    // Group by (layer + baseGid) so we update them together
    const groupKey = (layerName: string, baseGid: number) => `${layerName}:${baseGid}`;
    const groups = new Map<string, { tiles: Phaser.Tilemaps.Tile[]; frames: Array<{ gid: number; duration: number }> }>();

    for (const layerData of map.layers) {
      const tilemapLayer = layerData.tilemapLayer;
      if (!tilemapLayer) continue;

      for (let row = 0; row < map.height; row++) {
        for (let col = 0; col < map.width; col++) {
          const tile = tilemapLayer.getTileAt(col, row);
          if (!tile || tile.index < 0) continue;

          const frames = animMap.get(tile.index);
          if (!frames) continue;

          const key = groupKey(layerData.name, tile.index);
          let group = groups.get(key);
          if (!group) {
            group = { tiles: [], frames };
            groups.set(key, group);
          }
          group.tiles.push(tile);
        }
      }
    }

    for (const group of groups.values()) {
      this.animatedTiles.push({
        tiles: group.tiles,
        frames: group.frames,
        currentFrame: 0,
        elapsed: 0,
      });
    }
  }

  private updateAnimatedTiles(delta: number) {
    for (const anim of this.animatedTiles) {
      anim.elapsed += delta;
      const frame = anim.frames[anim.currentFrame];
      if (anim.elapsed >= frame.duration) {
        anim.elapsed -= frame.duration;
        anim.currentFrame = (anim.currentFrame + 1) % anim.frames.length;
        const newGid = anim.frames[anim.currentFrame].gid;
        for (const tile of anim.tiles) {
          tile.index = newGid;
        }
      }
    }
  }

  // ─── Collision Grid ────────────────────────────────

  private buildCollisionGrid(map: Phaser.Tilemaps.Tilemap) {
    const grid: number[][] = [];

    for (let row = 0; row < map.height; row++) {
      const gridRow: number[] = [];
      for (let col = 0; col < map.width; col++) {
        let walkable = true;

        // Check all tile layers (including sublayers like "terrain__2")
        for (const layerData of map.layers) {
          const tile = map.getTileAt(col, row, false, layerData.name);
          if (tile) {
            // Check tile property
            const tileWalkable = tile.properties?.walkable;
            if (tileWalkable === false) {
              walkable = false;
              break;
            }
            // Buildings and treetops layers (and their sublayers) are blocked by default if tile exists
            const baseName = layerData.name.replace(/__\d+$/, "");
            if (
              (baseName === "buildings" || baseName === "treetops") &&
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
        const spriteSheet = getProp("sprite_sheet") as string | undefined;
        const cfg = spriteSheet ? this.parseSpriteSheetName(spriteSheet) : null;

        if (obj.name === "agent_spawn") {
          this.agentSpawn = { x: obj.x!, y: obj.y! };
          if (cfg) this.agentSpriteConfig = cfg;
        } else if (obj.name === "user_spawn") {
          this.userSpawn = { x: obj.x!, y: obj.y! };
          if (cfg) this.userSpriteConfig = cfg;
        }
      } else if (obj.type === "wander_zone") {
        this.wanderZone = {
          x: obj.x!,
          y: obj.y!,
          width: obj.width!,
          height: obj.height!,
        };
      } else if (obj.type === "npc") {
        const sheet = getProp("sprite_sheet") as string | undefined;
        const sType = (getProp("sprite_type") as string) || "enemy";
        const fCol = (getProp("frame_col") as number) ?? 0;
        if (sheet) {
          this.npcDefs.push({
            name: obj.name ?? sheet,
            x: obj.x!,
            y: obj.y!,
            spriteSheet: sheet,
            spriteType: sType,
            frameCol: fCol,
          });
        }
      }
    }
  }

  private parseSpriteSheetName(
    name: string
  ): (SpriteConfig & { file: string }) | null {
    const match = name.match(/^(Character_\d{3})_(\d+)$/);
    if (!match) return null;
    const sheetName = match[1];
    const charNum = parseInt(match[2], 10); // 1-based
    if (charNum < 1 || charNum > 4) return null;
    return {
      key: sheetName,
      file: `${sheetName}.png`,
      colOffset: (charNum - 1) * 4,
    };
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
      this.fitCamera();
    }, 100);
  }

  private fitCamera() {
    const mapW = this.mapRef.widthInPixels;
    const mapH = this.mapRef.heightInPixels;
    const viewW = this.scale.width;
    const viewH = this.scale.height;
    const zoom = Math.max(viewW / mapW, viewH / mapH, SCENE.SPRITE_SCALE);
    this.cameras.main.setZoom(zoom);
    this.cameras.main.centerOn(mapW / 2, mapH / 2);
    this.label.setX(mapW / 2);
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

  // ─── Buildings ──────────────────────────────────────

  private parseBuildings(map: Phaser.Tilemaps.Tilemap) {
    const props = (map as unknown as { properties?: Array<{ name: string; value: unknown }> }).properties;
    if (!props) return;

    const buildingsProp = props.find((p) => p.name === "buildings");
    if (!buildingsProp || typeof buildingsProp.value !== "string") return;

    try {
      this.buildings = JSON.parse(buildingsProp.value) as BuildingDef[];
    } catch {
      // Ignore malformed buildings
    }
  }

  enterBuilding(buildingId: string, floorIndex = 0) {
    const building = this.buildings.find((b) => b.id === buildingId);
    if (!building || !building.floors[floorIndex]) return;

    this.stopWandering();
    this.currentBuilding = building;
    this.currentFloor = floorIndex;

    // Save the current collision grid for restoration
    this.savedExteriorGrid = this.pathfinder
      ? JSON.parse(JSON.stringify((this.pathfinder as unknown as { grid: number[][] }).grid))
      : null;

    // Hide exterior tiles within the zone (buildings, decorations, treetops layers + sublayers)
    this.hiddenExteriorTiles = [];
    const hideBaseLayers = ["buildings", "decorations", "treetops"];
    for (const layerData of this.mapRef.layers) {
      const baseName = layerData.name.replace(/__\d+$/, "");
      if (!hideBaseLayers.includes(baseName)) continue;
      if (!layerData.tilemapLayer) continue;
      const tilemapLayer = layerData.tilemapLayer;
      const savedTiles: Array<{ col: number; row: number; origIndex: number }> = [];

      for (let r = 0; r < building.zoneH; r++) {
        for (let c = 0; c < building.zoneW; c++) {
          const col = building.zoneCol + c;
          const row = building.zoneRow + r;
          const tile = this.mapRef.getTileAt(col, row, true, layerData.name);
          if (tile && tile.index >= 0) {
            savedTiles.push({ col, row, origIndex: tile.index });
            tilemapLayer.putTileAt(-1, col, row);
          }
        }
      }

      if (savedTiles.length > 0) {
        this.hiddenExteriorTiles.push({ layer: tilemapLayer, tiles: savedTiles });
      }
    }

    // Render interior tiles
    this.renderInteriorFloor(building, floorIndex);

    // Rebuild collision grid for the zone using all-walkable within zone
    this.rebuildCollisionGridForInterior(building, floorIndex);

    // Center camera on zone
    const zoneCenterX = (building.zoneCol + building.zoneW / 2) * this.mapRef.tileWidth;
    const zoneCenterY = (building.zoneRow + building.zoneH / 2) * this.mapRef.tileHeight;
    this.cameras.main.centerOn(zoneCenterX, zoneCenterY);

    // Update character building state
    this.agent.currentBuilding = buildingId;
    this.agent.currentFloor = floorIndex;

    // Hide user avatar if not in same building
    if (this.userAvatar.currentBuilding !== buildingId) {
      this.userAvatar.sprite.setVisible(false);
    }
  }

  exitBuilding() {
    if (!this.currentBuilding) return;

    // Destroy interior container
    if (this.interiorContainer) {
      this.interiorContainer.destroy(true);
      this.interiorContainer = null;
    }

    // Restore hidden exterior tiles
    for (const entry of this.hiddenExteriorTiles) {
      for (const t of entry.tiles) {
        entry.layer.putTileAt(t.origIndex, t.col, t.row);
      }
    }
    this.hiddenExteriorTiles = [];

    // Restore collision grid
    if (this.savedExteriorGrid) {
      this.pathfinder.setGrid(this.savedExteriorGrid);
      this.savedExteriorGrid = null;
    }

    // Reset building state
    this.currentBuilding = null;
    this.currentFloor = 0;
    this.agent.currentBuilding = null;
    this.agent.currentFloor = 0;

    // Show user avatar
    this.userAvatar.sprite.setVisible(true);

    // Re-center camera
    this.cameras.main.centerOn(
      this.mapRef.widthInPixels / 2,
      this.mapRef.heightInPixels / 2
    );

    this.startWandering();
  }

  changeFloor(newFloorIndex: number) {
    if (!this.currentBuilding) return;
    const building = this.currentBuilding;
    if (!building.floors[newFloorIndex]) return;

    // Destroy current interior
    if (this.interiorContainer) {
      this.interiorContainer.destroy(true);
      this.interiorContainer = null;
    }

    this.currentFloor = newFloorIndex;
    this.agent.currentFloor = newFloorIndex;

    // Render new floor
    this.renderInteriorFloor(building, newFloorIndex);

    // Rebuild collision grid for new floor
    this.rebuildCollisionGridForInterior(building, newFloorIndex);
  }

  private renderInteriorFloor(building: BuildingDef, floorIndex: number) {
    if (this.interiorContainer) {
      this.interiorContainer.destroy(true);
    }

    const floor = building.floors[floorIndex];
    if (!floor) return;

    this.interiorContainer = this.add.container(0, 0);
    this.interiorContainer.setDepth(2.5); // Between buildings (2) and decorations (3)

    const tileWidth = this.mapRef.tileWidth;
    const tileHeight = this.mapRef.tileHeight;

    for (let li = 0; li < floor.layers.length; li++) {
      const layerData = floor.layers[li];
      if (!layerData) continue;

      for (let r = 0; r < building.zoneH; r++) {
        for (let c = 0; c < building.zoneW; c++) {
          const gid = layerData[r * building.zoneW + c];
          if (gid <= 0) continue;

          // Resolve GID to tileset
          let resolvedTileset: Phaser.Tilemaps.Tileset | null = null;
          let localId = 0;
          for (let ti = this.tilesetsRef.length - 1; ti >= 0; ti--) {
            if (gid >= this.tilesetsRef[ti].firstgid) {
              resolvedTileset = this.tilesetsRef[ti];
              localId = gid - resolvedTileset.firstgid;
              break;
            }
          }
          if (!resolvedTileset) continue;

          const worldX = (building.zoneCol + c) * tileWidth + tileWidth / 2;
          const worldY = (building.zoneRow + r) * tileHeight + tileHeight / 2;

          const img = this.add.image(worldX, worldY, resolvedTileset.name, localId);
          img.setOrigin(0.5, 0.5);
          this.interiorContainer.add(img);
        }
      }
    }
  }

  private rebuildCollisionGridForInterior(building: BuildingDef, floorIndex: number) {
    if (!this.savedExteriorGrid) return;

    // Clone the exterior grid
    const grid = this.savedExteriorGrid.map((row) => [...row]);

    // Make all zone tiles walkable by default
    for (let r = 0; r < building.zoneH; r++) {
      for (let c = 0; c < building.zoneW; c++) {
        const gr = building.zoneRow + r;
        const gc = building.zoneCol + c;
        if (gr >= 0 && gr < grid.length && gc >= 0 && gc < grid[0].length) {
          grid[gr][gc] = 0;
        }
      }
    }

    // Block everything outside the zone
    for (let r = 0; r < grid.length; r++) {
      for (let c = 0; c < grid[0].length; c++) {
        if (
          r < building.zoneRow ||
          r >= building.zoneRow + building.zoneH ||
          c < building.zoneCol ||
          c >= building.zoneCol + building.zoneW
        ) {
          grid[r][c] = 1;
        }
      }
    }

    this.pathfinder.setGrid(grid);
  }

  private checkPortalCollision() {
    if (this.buildings.length === 0) return;

    const agentCol = Math.floor(this.agent.sprite.x / this.mapRef.tileWidth);
    const agentRow = Math.floor((this.agent.sprite.y - 1) / this.mapRef.tileHeight); // -1 because origin is bottom

    if (this.currentBuilding) {
      // Inside a building — check current floor portals
      const floor = this.currentBuilding.floors[this.currentFloor];
      if (!floor) return;

      const localCol = agentCol - this.currentBuilding.zoneCol;
      const localRow = agentRow - this.currentBuilding.zoneRow;

      for (const portal of floor.portals) {
        if (portal.localCol === localCol && portal.localRow === localRow) {
          if (portal.kind === "entry") {
            this.exitBuilding();
            return;
          } else if (portal.kind === "stairs_up" && this.currentFloor < this.currentBuilding.floors.length - 1) {
            this.changeFloor(this.currentFloor + 1);
            return;
          } else if (portal.kind === "stairs_down" && this.currentFloor > 0) {
            this.changeFloor(this.currentFloor - 1);
            return;
          }
        }
      }
    } else {
      // Outside — check entry portals on ground floor of all buildings
      for (const building of this.buildings) {
        const floor = building.floors[0];
        if (!floor) continue;

        for (const portal of floor.portals) {
          if (portal.kind !== "entry") continue;
          const portalWorldCol = building.zoneCol + portal.localCol;
          const portalWorldRow = building.zoneRow + portal.localRow;
          if (agentCol === portalWorldCol && agentRow === portalWorldRow) {
            this.enterBuilding(building.id, 0);
            return;
          }
        }
      }
    }
  }

  // ─── Player Input (WASD / Arrow Keys) ────────────

  private handlePlayerInput() {
    if (this.userMoving || !this.cursors || !this.wasd) return;

    let dx = 0;
    let dy = 0;
    let dir: string | null = null;

    if (this.cursors.left.isDown || this.wasd["A"].isDown) {
      dx = -1; dir = "left";
    } else if (this.cursors.right.isDown || this.wasd["D"].isDown) {
      dx = 1; dir = "right";
    } else if (this.cursors.up.isDown || this.wasd["W"].isDown) {
      dy = -1; dir = "up";
    } else if (this.cursors.down.isDown || this.wasd["S"].isDown) {
      dy = 1; dir = "down";
    }

    if (!dir) return;

    const ts = this.mapRef.tileWidth;
    const col = Math.floor(this.userAvatar.sprite.x / ts);
    const row = Math.floor((this.userAvatar.sprite.y - 1) / ts);
    const tc = col + dx;
    const tr = row + dy;
    const tx = tc * ts + ts / 2;
    const ty = tr * ts + ts / 2;

    if (!this.pathfinder.isWalkable(tx, ty)) return;

    this.userMoving = true;
    this.userAvatar.cancelMovement();
    this.userAvatar.sprite.play(`user-walk-${dir}`);

    const duration = Math.max((ts / SCENE.WALK_SPEED) * 1000, 100);
    this.userMoveTween = this.tweens.add({
      targets: this.userAvatar.sprite,
      x: tx,
      y: ty,
      duration,
      ease: "Linear",
      onComplete: () => {
        this.userMoveTween = null;
        this.userMoving = false;
        if (!this.isAnyMoveKeyDown()) {
          this.userAvatar.sprite.play("user-idle");
        }
      },
    });
  }

  private isAnyMoveKeyDown(): boolean {
    if (!this.cursors || !this.wasd) return false;
    return (
      this.cursors.left.isDown ||
      this.cursors.right.isDown ||
      this.cursors.up.isDown ||
      this.cursors.down.isDown ||
      this.wasd["A"].isDown ||
      this.wasd["D"].isDown ||
      this.wasd["W"].isDown ||
      this.wasd["S"].isDown
    );
  }

  private cancelUserMove() {
    this.userMoving = false;
    if (this.userMoveTween) {
      this.userMoveTween.stop();
      this.userMoveTween = null;
    }
  }

  // ─── Debug Walkability Grid ──────────────────────

  private drawDebugGrid() {
    this.clearDebugGrid();

    const grid = this.pathfinder.getGrid();
    const ts = this.mapRef.tileWidth;
    const gfx = this.add.graphics();
    gfx.setDepth(50);

    for (let r = 0; r < grid.length; r++) {
      for (let c = 0; c < grid[r].length; c++) {
        const color = grid[r][c] === 0 ? 0x00ff00 : 0xff0000;
        gfx.fillStyle(color, 0.3);
        gfx.fillRect(c * ts, r * ts, ts, ts);
      }
    }

    this.debugGridGraphics = gfx;
  }

  private clearDebugGrid() {
    if (this.debugGridGraphics) {
      this.debugGridGraphics.destroy();
      this.debugGridGraphics = null;
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
