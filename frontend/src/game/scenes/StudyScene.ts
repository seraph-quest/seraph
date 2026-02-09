import Phaser from "phaser";
import { EventBus } from "../EventBus";
import { AgentSprite } from "../objects/AgentSprite";
import { SpeechBubble } from "../objects/SpeechBubble";
import { UserSprite } from "../objects/UserSprite";
import { SCENE } from "../../config/constants";

interface ToolMovePayload {
  tool: string;
  targetX: number;
  targetY: number;
  anim: string;
  text: string;
}

interface FinalAnswerPayload {
  text: string;
}

type TimeOfDay = "day" | "night";

// ─── Asset manifest ──────────────────────────────────────
const VILLAGE_ASSETS: Record<string, { day: string; night: string }> = {
  "grass-tile":       { day: "grass-tile-day",       night: "grass-tile-night" },
  "ground-tile":      { day: "ground-tile-day",      night: "ground-tile-night" },
  "tree-1":           { day: "tree-1-day",           night: "tree-1-night" },
  "tree-2":           { day: "tree-2-day",           night: "tree-2-night" },
  "tree-3":           { day: "tree-3-day",           night: "tree-3-night" },
  "house-1":          { day: "house-1-day",          night: "house-1-night" },
  "house-2":          { day: "house-2-day",          night: "house-2-night" },
};

// Tool station sprites (no day/night variants — same image for both)
const TOOL_STATION_ASSETS: Record<string, string> = {
  "well":             "well",
  "scroll-desk":      "scroll-desk",
  "shrine":           "shrine",
  "anvil":            "anvil",
  "telescope-tower":  "telescope-tower",
  "sundial":          "sundial",
  "pigeon-post":      "pigeon-post",
};

// Tooltip labels for tool stations
const TOOL_STATION_TOOLTIPS: Record<string, string> = {
  "well":             "Web Search",
  "scroll-desk":      "File Reading & Writing",
  "shrine":           "Soul & Goals",
  "anvil":            "Shell Terminal",
  "telescope-tower":  "Web Browser",
  "sundial":          "Calendar",
  "pigeon-post":      "Email",
};

// Tool station placements — positioned in front of their parent building
const TOOL_STATIONS: Array<{ x: number; y: number; key: string }> = [
  { x: 192, y: 280, key: "well" },              // in front of house-1 (web_search)
  { x: 832, y: 280, key: "scroll-desk" },        // in front of house-2 (read/write)
  { x: 512, y: 240, key: "shrine" },             // in front of church (soul/goals/template)
  { x: 384, y: 330, key: "anvil" },              // in front of forge (shell_execute)
  { x: 640, y: 210, key: "telescope-tower" },    // in front of tower (browse_webpage)
  { x: 576, y: 350, key: "sundial" },            // clock area (calendar)
  { x: 128, y: 350, key: "pigeon-post" },        // mailbox area (email)
];

// ─── Village Layout Data (all coords village-local) ──────

// Ground path segments forming a cross/T shape
const PATH_SEGMENTS = [
  { x: 16,  y: 256, w: 992, h: 48 },      // Main horizontal path
  { x: 488, y: 304, w: 48,  h: 76 },       // Vertical south path from plaza
];

// Buildings (origin: bottom-center)
const BUILDINGS = [
  { x: 192, y: 256, key: "house-1" },      // Left house — web_search
  { x: 832, y: 256, key: "house-2" },       // Right house — read/write
];


// Village trees — clusters and singles for organic feel
const VILLAGE_TREES: Array<{ x: number; y: number; type: number }> = [
  // ── Top tree border (dense canopy) ──
  { x: 32,  y: 56,  type: 1 }, { x: 80,  y: 48,  type: 3 },
  { x: 140, y: 56,  type: 2 }, { x: 220, y: 44,  type: 1 },
  { x: 300, y: 52,  type: 3 }, { x: 400, y: 48,  type: 2 },
  { x: 512, y: 40,  type: 1 }, { x: 620, y: 48,  type: 3 },
  { x: 720, y: 44,  type: 2 }, { x: 810, y: 52,  type: 1 },
  { x: 900, y: 48,  type: 3 }, { x: 980, y: 56,  type: 2 },
  // ── Second row — sparser, varied ──
  { x: 48,  y: 112, type: 2 }, { x: 320, y: 100, type: 3 },
  { x: 680, y: 108, type: 1 }, { x: 960, y: 104, type: 3 },
  // ── Flanking the buildings ──
  { x: 60,  y: 220, type: 1 }, { x: 300, y: 192, type: 2 },
  { x: 404, y: 172, type: 3 }, { x: 624, y: 172, type: 1 },
  { x: 716, y: 192, type: 2 }, { x: 956, y: 220, type: 3 },
  // ── South area — around pond and features ──
  { x: 60,  y: 340, type: 3 }, { x: 140, y: 380, type: 1 },
  { x: 220, y: 360, type: 2 },
  { x: 820, y: 340, type: 1 }, { x: 900, y: 380, type: 3 },
  { x: 960, y: 360, type: 2 },
  // ── Bottom tree border ──
  { x: 48,  y: 470, type: 2 }, { x: 140, y: 488, type: 1 },
  { x: 240, y: 476, type: 3 }, { x: 340, y: 484, type: 2 },
  { x: 680, y: 480, type: 1 }, { x: 780, y: 472, type: 3 },
  { x: 880, y: 486, type: 2 }, { x: 968, y: 478, type: 1 },
];



export class StudyScene extends Phaser.Scene {
  private agent!: AgentSprite;
  private userAvatar!: UserSprite;
  private speechBubble!: SpeechBubble;
  private label!: Phaser.GameObjects.Text;

  private villageOffsetX = 0;
  private villageOffsetY = 0;

  private grassTileSprite!: Phaser.GameObjects.TileSprite;
  private groundPaths: Phaser.GameObjects.TileSprite[] = [];

  // All env sprites for day/night swapping + repositioning
  private envSprites: Array<{ sprite: Phaser.GameObjects.Image; assetKey: string }> = [];
  private forestSprites: Array<{ sprite: Phaser.GameObjects.Image; treeType: number }> = [];

  private toolStationSprites: Phaser.GameObjects.Image[] = [];
  private tooltip!: Phaser.GameObjects.Container;
  private tooltipText!: Phaser.GameObjects.Text;
  private tooltipBg!: Phaser.GameObjects.Rectangle;

  private isWandering = false;
  private wanderTimer: Phaser.Time.TimerEvent | null = null;

  private resizeDebounceTimer: ReturnType<typeof setTimeout> | null = null;
  private dayNightTimer: Phaser.Time.TimerEvent | null = null;
  private currentTimeOfDay: TimeOfDay = "day";

  private handleThink!: () => void;
  private handleToolMove!: (payload: ToolMovePayload) => void;
  private handleFinalAnswer!: (payload: FinalAnswerPayload) => void;
  private handleReturnIdle!: () => void;

  constructor() {
    super("StudyScene");
  }

  // ─── Helpers ─────────────────────────────────────────

  private getTimeOfDay(): TimeOfDay {
    const hour = new Date().getHours();
    return hour >= SCENE.DAY_START_HOUR && hour < SCENE.DAY_END_HOUR ? "day" : "night";
  }

  private texKey(assetKey: string): string {
    return VILLAGE_ASSETS[assetKey][this.currentTimeOfDay];
  }

  private worldPos(local: { x: number; y: number }): { x: number; y: number } {
    return {
      x: local.x + this.villageOffsetX,
      y: local.y + this.villageOffsetY,
    };
  }

  // ─── Preload ─────────────────────────────────────────

  preload() {
    for (const [, variants] of Object.entries(VILLAGE_ASSETS)) {
      this.load.image(variants.day, `assets/village/${variants.day}.png`);
      this.load.image(variants.night, `assets/village/${variants.night}.png`);
    }
    for (const [, filename] of Object.entries(TOOL_STATION_ASSETS)) {
      this.load.image(filename, `assets/village/${filename}.png`);
    }
    AgentSprite.preload(this);
    UserSprite.preload(this);

    this.load.on("loaderror", (_file: { key: string }) => {
      // silently ignore missing assets
    });
  }


  // ─── Create ──────────────────────────────────────────

  create() {
    const canvasW = this.scale.width;
    const canvasH = this.scale.height;

    this.currentTimeOfDay = this.getTimeOfDay();

    this.villageOffsetX = Math.floor((canvasW - SCENE.MAP_PIXEL_WIDTH) / 2);
    this.villageOffsetY = Math.floor((canvasH - SCENE.MAP_PIXEL_HEIGHT) / 2);

    // Set initial background for time of day
    if (this.currentTimeOfDay === "night") {
      this.cameras.main.setBackgroundColor("#1a2a3a");
    }

    // --- Layer 1: Base grass ---
    this.createGrassBase(canvasW, canvasH);

    // --- Layer 2: Ground paths ---
    this.createGroundPaths();

    // --- Layer 3: Y-sorted objects (buildings, tool stations, village trees) ---
    this.placeBuildings();
    this.placeToolStations();
    this.placeVillageTrees();

    // --- Forest (outside village) ---
    this.generateForest(canvasW, canvasH);

    // --- Agent (Seraph) ---
    const startPos = this.worldPos(SCENE.POSITIONS.bench);
    this.agent = new AgentSprite(this, startPos.x, startPos.y);

    // Make Seraph clickable — toggles chat panel
    this.agent.sprite.setInteractive({ useHandCursor: true });
    this.agent.sprite.on("pointerdown", () => {
      EventBus.emit("toggle-chat");
    });

    // --- User Avatar (clickable — toggles quest log) ---
    const userPos = this.worldPos(SCENE.POSITIONS.userHome);
    this.userAvatar = new UserSprite(this, userPos.x, userPos.y);

    // --- Speech Bubble ---
    this.speechBubble = new SpeechBubble(this, SCENE.MAP_PIXEL_WIDTH, this.villageOffsetX);
    this.speechBubble.setTarget(this.agent.sprite);

    // --- Hover tooltip (shared, repositioned on hover) ---
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

    // --- Village label ---
    this.label = this.add.text(canvasW / 2, 12, "Seraph's Village", {
      fontFamily: '"Press Start 2P"',
      fontSize: "8px",
      color: "#ffffff",
      stroke: "#000000",
      strokeThickness: 3,
    });
    this.label.setOrigin(0.5, 0);
    this.label.setDepth(25);

    // --- EventBus handlers ---
    this.handleThink = () => {
      this.stopWandering();
      this.speechBubble.hide();
      const pos = this.worldPos(SCENE.POSITIONS.bench);
      this.agent.moveTo(pos.x, pos.y, () => {
        this.agent.playAnim("think");
      });

      // User walks toward agent's thinking position, stopping short
      const userTarget = { x: pos.x + 40, y: pos.y + 10 };
      this.userAvatar.moveTo(userTarget.x, userTarget.y, () => {
        this.userAvatar.sprite.play("user-idle");
      });
    };

    this.handleToolMove = (payload: ToolMovePayload) => {
      this.stopWandering();
      this.speechBubble.hide();
      const worldTarget = this.worldPos({ x: payload.targetX, y: payload.targetY });
      this.agent.moveTo(worldTarget.x, worldTarget.y, () => {
        this.agent.playAnim(payload.anim);
        if (payload.text) {
          this.speechBubble.show(payload.text);
        }
      });
    };

    this.handleFinalAnswer = (payload: FinalAnswerPayload) => {
      this.stopWandering();
      // Seraph walks toward User's current position (stopping short)
      const targetX = this.userAvatar.sprite.x - 40;
      const targetY = this.userAvatar.sprite.y;
      this.agent.moveTo(targetX, targetY, () => {
        this.agent.playAnim("idle");
        this.speechBubble.show(payload.text);

        this.time.delayedCall(6000, () => {
          this.speechBubble.hide();
          EventBus.emit("agent-speech-done");
          // Both return: Seraph wanders, User goes home
          this.userAvatar.returnHome();
          this.startWandering();
        });
      });
    };

    this.handleReturnIdle = () => {
      this.speechBubble.hide();
      const pos = this.worldPos(SCENE.POSITIONS.bench);
      this.agent.moveTo(pos.x, pos.y, () => {
        this.agent.playAnim("idle");
        this.startWandering();
      });
      this.userAvatar.returnHome();
    };

    EventBus.on("agent-think", this.handleThink);
    EventBus.on("agent-move-to-tool", this.handleToolMove);
    EventBus.on("agent-final-answer", this.handleFinalAnswer);
    EventBus.on("agent-return-idle", this.handleReturnIdle);

    this.scale.on("resize", this.onResize, this);

    this.dayNightTimer = this.time.addEvent({
      delay: 60000,
      callback: this.checkDayNight,
      callbackScope: this,
      loop: true,
    });

    this.startWandering();
    EventBus.emit("current-scene-ready", this);
  }

  update() {
    this.speechBubble.updatePosition();
  }

  shutdown() {
    EventBus.off("agent-think", this.handleThink);
    EventBus.off("agent-move-to-tool", this.handleToolMove);
    EventBus.off("agent-final-answer", this.handleFinalAnswer);
    EventBus.off("agent-return-idle", this.handleReturnIdle);

    this.scale.off("resize", this.onResize, this);
    this.stopWandering();
    this.userAvatar.cancelMovement();

    if (this.resizeDebounceTimer) clearTimeout(this.resizeDebounceTimer);
    if (this.dayNightTimer) this.dayNightTimer.remove(false);

    this.agent.destroy();
    this.userAvatar.destroy();
    this.speechBubble.destroy();
  }

  // ─── Grass Base ──────────────────────────────────────

  private createGrassBase(canvasW: number, canvasH: number) {
    this.grassTileSprite = this.add.tileSprite(0, 0, canvasW, canvasH, this.texKey("grass-tile"));
    this.grassTileSprite.setOrigin(0, 0);
    this.grassTileSprite.setDepth(-10);
  }

  // ─── Ground Paths ───────────────────────────────────

  private createGroundPaths() {
    for (const seg of PATH_SEGMENTS) {
      const wp = this.worldPos({ x: seg.x, y: seg.y });
      const ts = this.add.tileSprite(wp.x, wp.y, seg.w, seg.h, this.texKey("ground-tile"));
      ts.setOrigin(0, 0);
      ts.setDepth(-6);
      this.groundPaths.push(ts);
    }
  }

  // ─── Buildings (Y-sorted) ───────────────────────────

  private placeBuildings() {
    for (const b of BUILDINGS) {
      const wp = this.worldPos(b);
      const sprite = this.add.image(wp.x, wp.y, this.texKey(b.key));
      sprite.setOrigin(0.5, 1);
      sprite.setDepth(b.y * 0.01); // Y-sort
      this.envSprites.push({ sprite, assetKey: b.key });
    }
  }

  // ─── Tool Stations (Y-sorted, in front of buildings) ─

  private placeToolStations() {
    for (const ts of TOOL_STATIONS) {
      const wp = this.worldPos(ts);
      const sprite = this.add.image(wp.x, wp.y, ts.key);
      sprite.setOrigin(0.5, 1);
      sprite.setDepth(ts.y * 0.01 + 0.1); // slightly in front of buildings

      const label = TOOL_STATION_TOOLTIPS[ts.key];
      if (label) {
        sprite.setInteractive({ useHandCursor: true });
        sprite.on("pointerover", () => {
          this.tooltipText.setText(label);
          const pad = 6;
          this.tooltipBg.setSize(this.tooltipText.width + pad * 2, this.tooltipText.height + pad * 2);
          this.tooltip.setPosition(sprite.x, sprite.y - sprite.displayHeight - 8);
          this.tooltip.setVisible(true);
        });
        sprite.on("pointerout", () => {
          this.tooltip.setVisible(false);
        });
      }

      this.toolStationSprites.push(sprite);
    }
  }

  // ─── Village Trees (Y-sorted) ───────────────────────

  private placeVillageTrees() {
    for (const t of VILLAGE_TREES) {
      const wp = this.worldPos(t);
      const key = `tree-${t.type}`;
      const sprite = this.add.image(wp.x, wp.y, this.texKey(key));
      sprite.setOrigin(0.5, 1);
      sprite.setDepth(t.y * 0.01); // Y-sort
      this.envSprites.push({ sprite, assetKey: key });
    }
  }

  // ─── Forest Generation ──────────────────────────────

  private generateForest(canvasW: number, canvasH: number) {
    for (const entry of this.forestSprites) {
      entry.sprite.destroy();
    }
    this.forestSprites = [];

    const spacing = SCENE.FOREST.TILE_SPACING;
    const buffer = SCENE.FOREST.BUFFER_TILES * SCENE.TILE_SIZE;
    const treeSize = 48;
    const margin = treeSize + spacing;

    const vLeft = this.villageOffsetX - buffer;
    const vRight = this.villageOffsetX + SCENE.MAP_PIXEL_WIDTH + buffer;
    const vTop = this.villageOffsetY - buffer;
    const vBottom = this.villageOffsetY + SCENE.MAP_PIXEL_HEIGHT + buffer;

    let rng = 42;
    const nextRand = () => {
      rng = (rng * 16807) % 2147483647;
      return rng / 2147483647;
    };

    let treeCount = 0;

    for (let wx = -margin; wx < canvasW + margin; wx += spacing) {
      for (let wy = -margin; wy < canvasH + margin; wy += spacing) {
        if (wx > vLeft && wx < vRight && wy > vTop && wy < vBottom) {
          nextRand(); nextRand(); nextRand();
          continue;
        }

        if (nextRand() > SCENE.FOREST.DENSITY) {
          nextRand(); nextRand();
          continue;
        }

        if (treeCount >= SCENE.FOREST.MAX_TREES) break;

        const jx = Math.round(wx + (nextRand() - 0.5) * spacing * 0.6);
        const jy = Math.round(wy + (nextRand() - 0.5) * spacing * 0.6);

        if (jx + treeSize < 0 || jx - treeSize > canvasW ||
            jy + treeSize < 0 || jy - treeSize > canvasH) {
          continue;
        }

        const treeType = Math.floor(nextRand() * 3) + 1;
        const sprite = this.add.image(jx, jy, this.texKey(`tree-${treeType}`));
        sprite.setOrigin(0.5, 1);
        sprite.setDepth(jy * 0.01);

        this.forestSprites.push({ sprite, treeType });
        treeCount++;
      }
      if (treeCount >= SCENE.FOREST.MAX_TREES) break;
    }
  }

  // ─── Day/Night Cycle ─────────────────────────────────

  private checkDayNight() {
    const newTime = this.getTimeOfDay();
    if (newTime === this.currentTimeOfDay) return;
    this.currentTimeOfDay = newTime;
    this.swapAllTextures();
  }

  private swapAllTextures() {
    const tod = this.currentTimeOfDay;

    this.grassTileSprite.setTexture(VILLAGE_ASSETS["grass-tile"][tod]);

    for (const ts of this.groundPaths) {
      ts.setTexture(VILLAGE_ASSETS["ground-tile"][tod]);
    }

    for (const entry of this.envSprites) {
      const variants = VILLAGE_ASSETS[entry.assetKey];
      if (variants) {
        entry.sprite.setTexture(variants[tod]);
      }
    }

    for (const entry of this.forestSprites) {
      entry.sprite.setTexture(VILLAGE_ASSETS[`tree-${entry.treeType}`][tod]);
    }

    if (tod === "night") {
      this.cameras.main.setBackgroundColor("#1a2a3a");
    } else {
      this.cameras.main.setBackgroundColor("#4a8c3f");
    }
  }

  // ─── Resize Handler ─────────────────────────────────

  private onResize(gameSize: Phaser.Structs.Size) {
    if (this.resizeDebounceTimer) clearTimeout(this.resizeDebounceTimer);
    this.resizeDebounceTimer = setTimeout(() => {
      this.handleResize(gameSize.width, gameSize.height);
    }, 100);
  }

  private handleResize(canvasW: number, canvasH: number) {
    const oldOffsetX = this.villageOffsetX;
    const oldOffsetY = this.villageOffsetY;

    this.villageOffsetX = Math.floor((canvasW - SCENE.MAP_PIXEL_WIDTH) / 2);
    this.villageOffsetY = Math.floor((canvasH - SCENE.MAP_PIXEL_HEIGHT) / 2);

    const dx = this.villageOffsetX - oldOffsetX;
    const dy = this.villageOffsetY - oldOffsetY;

    // Resize grass
    this.grassTileSprite.setSize(canvasW, canvasH);

    // Reposition ground paths
    PATH_SEGMENTS.forEach((seg, i) => {
      const wp = this.worldPos({ x: seg.x, y: seg.y });
      this.groundPaths[i].setPosition(wp.x, wp.y);
    });

    // Move all env sprites by delta
    for (const entry of this.envSprites) {
      entry.sprite.x += dx;
      entry.sprite.y += dy;
    }

    // Move tool station sprites by delta
    for (const sprite of this.toolStationSprites) {
      sprite.x += dx;
      sprite.y += dy;
    }

    // Regenerate forest
    this.generateForest(canvasW, canvasH);

    // Move agent and user avatar
    this.agent.sprite.x += dx;
    this.agent.sprite.y += dy;
    this.userAvatar.sprite.x += dx;
    this.userAvatar.sprite.y += dy;

    // Reposition label
    this.label.setX(canvasW / 2);

    // Update speech bubble bounds
    this.speechBubble.updateClampBounds(SCENE.MAP_PIXEL_WIDTH, this.villageOffsetX);
  }

  // ─── Wandering System ───────────────────────────────

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
    if (!this.isWandering) return;

    const waypoints = SCENE.WANDERING.WAYPOINTS;
    const target = waypoints[Math.floor(Math.random() * waypoints.length)];
    const worldTarget = this.worldPos(target);

    this.agent.moveTo(worldTarget.x, worldTarget.y, () => {
      this.agent.playAnim("idle");
      this.scheduleNextWander();
    });
  }
}
