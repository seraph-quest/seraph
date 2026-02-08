import Phaser from "phaser";
import { EventBus } from "../EventBus";
import { AgentSprite } from "../objects/AgentSprite";
import { SpeechBubble } from "../objects/SpeechBubble";
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
  "water-tile":       { day: "water-tile-day",       night: "water-tile-night" },
  "tree-1":           { day: "tree-1-day",           night: "tree-1-night" },
  "tree-2":           { day: "tree-2-day",           night: "tree-2-night" },
  "tree-3":           { day: "tree-3-day",           night: "tree-3-night" },
  "house-1":          { day: "house-1-day",          night: "house-1-night" },
  "house-2":          { day: "house-2-day",          night: "house-2-night" },
  "church":           { day: "church-day",           night: "church-night" },
  "fence-1":          { day: "fence-1-day",          night: "fence-1-night" },
  "fence-2":          { day: "fence-2-day",          night: "fence-2-night" },
  "bridge":           { day: "bridge-day",           night: "bridge-night" },
  "pit":              { day: "pit-day",              night: "pit-night" },
  "stairs":           { day: "stairs-day",           night: "stairs-night" },
  "grass-detail-1":   { day: "grass-detail-1-day",   night: "grass-detail-1-night" },
  "grass-detail-2":   { day: "grass-detail-2-day",   night: "grass-detail-2-night" },
  "grass-detail-3":   { day: "grass-detail-3-day",   night: "grass-detail-3-night" },
  "grass-detail-4":   { day: "grass-detail-4-day",   night: "grass-detail-4-night" },
  "grass-detail-5":   { day: "grass-detail-5-day",   night: "grass-detail-5-night" },
  "grass-detail-6":   { day: "grass-detail-6-day",   night: "grass-detail-6-night" },
  "ground-detail-1":  { day: "ground-detail-1-day",  night: "ground-detail-1-night" },
  "ground-detail-2":  { day: "ground-detail-2-day",  night: "ground-detail-2-night" },
  "ground-detail-3":  { day: "ground-detail-3-day",  night: "ground-detail-3-night" },
  "ground-detail-4":  { day: "ground-detail-4-day",  night: "ground-detail-4-night" },
  "ground-detail-5":  { day: "ground-detail-5-day",  night: "ground-detail-5-night" },
  "water-detail-1":   { day: "water-detail-1-day",   night: "water-detail-1-night" },
  "water-detail-2":   { day: "water-detail-2-day",   night: "water-detail-2-night" },
  "water-detail-3":   { day: "water-detail-3-day",   night: "water-detail-3-night" },
  "water-detail-4":   { day: "water-detail-4-day",   night: "water-detail-4-night" },
  "water-detail-5":   { day: "water-detail-5-day",   night: "water-detail-5-night" },
  "terrain-set-1":    { day: "terrain-set-1-day",    night: "terrain-set-1-night" },
  "terrain-set-2":    { day: "terrain-set-2-day",    night: "terrain-set-2-night" },
  "terrain-set-5":    { day: "terrain-set-5-day",    night: "terrain-set-5-night" },
};

// ─── Village Layout Data (all coords village-local) ──────

// Ground path segments forming a cross/T shape
const PATH_SEGMENTS = [
  { x: 16,  y: 256, w: 992, h: 48 },      // Main horizontal path
  { x: 448, y: 196, w: 128, h: 60 },       // Church courtyard (connects church to path)
  { x: 488, y: 304, w: 48,  h: 76 },       // Vertical south path from plaza
  { x: 160, y: 244, w: 64,  h: 12 },       // House 1 entrance porch
  { x: 776, y: 244, w: 112, h: 12 },       // House 2 entrance porch
];

// Buildings (origin: bottom-center)
const BUILDINGS = [
  { x: 192, y: 256, key: "house-1" },      // Left house — web_search
  { x: 512, y: 196, key: "church" },        // Center church — fill_template (raised)
  { x: 832, y: 256, key: "house-2" },       // Right house — read/write
];

// Water areas (tile-based rectangles)
const WATER_AREAS = [
  { x: 380, y: 388, cols: 16, rows: 7 },   // Large pond (256×112)
];

// Bridge over pond (origin: bottom-center)
const BRIDGE_POS = { x: 512, y: 440 };

// Village well / pit (origin: bottom-center)
const WELL_POS = { x: 300, y: 356 };

// Stone stairs (origin: bottom-center)
const STAIRS_POS = { x: 720, y: 376 };

// Terrain transition patches along path edges (origin: 0,0)
const TERRAIN_PATCHES: Array<{ x: number; y: number; key: string }> = [
  // Top edge of main path — grass-to-ground transitions
  { x: 16,  y: 240, key: "terrain-set-1" },
  { x: 80,  y: 240, key: "terrain-set-2" },
  { x: 144, y: 240, key: "terrain-set-5" },
  { x: 240, y: 240, key: "terrain-set-1" },
  { x: 304, y: 240, key: "terrain-set-2" },
  { x: 368, y: 240, key: "terrain-set-5" },
  { x: 608, y: 240, key: "terrain-set-1" },
  { x: 672, y: 240, key: "terrain-set-2" },
  { x: 736, y: 240, key: "terrain-set-5" },
  { x: 864, y: 240, key: "terrain-set-1" },
  { x: 928, y: 240, key: "terrain-set-2" },
  // Bottom edge of main path
  { x: 16,  y: 304, key: "terrain-set-2" },
  { x: 80,  y: 304, key: "terrain-set-5" },
  { x: 144, y: 304, key: "terrain-set-1" },
  { x: 240, y: 304, key: "terrain-set-2" },
  { x: 368, y: 304, key: "terrain-set-5" },
  { x: 608, y: 304, key: "terrain-set-1" },
  { x: 672, y: 304, key: "terrain-set-5" },
  { x: 864, y: 304, key: "terrain-set-2" },
  { x: 928, y: 304, key: "terrain-set-1" },
];

// Fences — long decorative runs (origin: 0,0)
const FENCE_POSITIONS: Array<{ x: number; y: number; type: number }> = [
  // Garden fence left of house 1
  { x: 96,  y: 160, type: 1 }, { x: 112, y: 160, type: 2 },
  { x: 128, y: 160, type: 1 }, { x: 144, y: 160, type: 2 },
  { x: 160, y: 160, type: 1 }, { x: 176, y: 160, type: 2 },
  { x: 192, y: 160, type: 1 }, { x: 208, y: 160, type: 2 },
  { x: 224, y: 160, type: 1 }, { x: 240, y: 160, type: 2 },
  { x: 256, y: 160, type: 1 },
  // Garden fence right of house 2
  { x: 768, y: 160, type: 2 }, { x: 784, y: 160, type: 1 },
  { x: 800, y: 160, type: 2 }, { x: 816, y: 160, type: 1 },
  { x: 832, y: 160, type: 2 }, { x: 848, y: 160, type: 1 },
  { x: 864, y: 160, type: 2 }, { x: 880, y: 160, type: 1 },
  { x: 896, y: 160, type: 2 }, { x: 912, y: 160, type: 1 },
  // Pond fence (partial, south side)
  { x: 380, y: 500, type: 1 }, { x: 396, y: 500, type: 2 },
  { x: 412, y: 500, type: 1 }, { x: 428, y: 500, type: 2 },
  { x: 572, y: 500, type: 2 }, { x: 588, y: 500, type: 1 },
  { x: 604, y: 500, type: 2 }, { x: 620, y: 500, type: 1 },
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

// Dense grass details scattered everywhere
const GRASS_DETAILS: Array<{ x: number; y: number; type: number }> = [
  // Top area
  { x: 60,  y: 70,  type: 1 }, { x: 160, y: 80,  type: 3 },
  { x: 260, y: 68,  type: 5 }, { x: 360, y: 76,  type: 2 },
  { x: 460, y: 64,  type: 4 }, { x: 560, y: 72,  type: 6 },
  { x: 660, y: 68,  type: 1 }, { x: 760, y: 80,  type: 3 },
  { x: 860, y: 70,  type: 5 }, { x: 940, y: 78,  type: 2 },
  // Mid-upper area (between trees and buildings)
  { x: 80,  y: 132, type: 4 }, { x: 180, y: 140, type: 6 },
  { x: 340, y: 128, type: 2 }, { x: 500, y: 136, type: 1 },
  { x: 640, y: 130, type: 5 }, { x: 760, y: 138, type: 3 },
  { x: 900, y: 132, type: 6 },
  // Around buildings
  { x: 100, y: 200, type: 1 }, { x: 272, y: 208, type: 4 },
  { x: 380, y: 196, type: 2 }, { x: 636, y: 200, type: 5 },
  { x: 740, y: 204, type: 3 }, { x: 936, y: 196, type: 6 },
  // South of path
  { x: 40,  y: 316, type: 3 }, { x: 120, y: 328, type: 1 },
  { x: 200, y: 320, type: 5 }, { x: 640, y: 316, type: 2 },
  { x: 760, y: 324, type: 4 }, { x: 880, y: 312, type: 6 },
  { x: 940, y: 328, type: 1 },
  // Near water / south area
  { x: 80,  y: 400, type: 2 }, { x: 180, y: 420, type: 6 },
  { x: 260, y: 408, type: 4 }, { x: 660, y: 396, type: 1 },
  { x: 800, y: 412, type: 5 }, { x: 920, y: 400, type: 3 },
  // Bottom area
  { x: 100, y: 456, type: 5 }, { x: 300, y: 448, type: 2 },
  { x: 500, y: 460, type: 4 }, { x: 600, y: 452, type: 6 },
  { x: 800, y: 444, type: 1 }, { x: 950, y: 456, type: 3 },
];

// Ground details scattered along paths
const GROUND_DETAILS: Array<{ x: number; y: number; type: number }> = [
  { x: 60,  y: 264, type: 1 }, { x: 160, y: 276, type: 3 },
  { x: 280, y: 268, type: 2 }, { x: 400, y: 280, type: 5 },
  { x: 620, y: 268, type: 4 }, { x: 740, y: 276, type: 1 },
  { x: 860, y: 264, type: 3 }, { x: 960, y: 280, type: 2 },
  // Courtyard details
  { x: 472, y: 212, type: 4 }, { x: 540, y: 220, type: 5 },
  // South path details
  { x: 496, y: 320, type: 1 }, { x: 504, y: 352, type: 3 },
];

// Water details in the pond
const WATER_DETAILS: Array<{ x: number; y: number; type: number }> = [
  { x: 400, y: 400, type: 1 }, { x: 432, y: 420, type: 2 },
  { x: 460, y: 444, type: 4 }, { x: 420, y: 460, type: 3 },
  { x: 560, y: 408, type: 5 }, { x: 580, y: 440, type: 2 },
  { x: 540, y: 468, type: 4 }, { x: 608, y: 456, type: 1 },
];


export class StudyScene extends Phaser.Scene {
  private agent!: AgentSprite;
  private speechBubble!: SpeechBubble;
  private label!: Phaser.GameObjects.Text;

  private villageOffsetX = 0;
  private villageOffsetY = 0;

  private grassTileSprite!: Phaser.GameObjects.TileSprite;
  private groundPaths: Phaser.GameObjects.TileSprite[] = [];
  private waterAreas: Phaser.GameObjects.TileSprite[] = [];

  // All env sprites for day/night swapping + repositioning
  private envSprites: Array<{ sprite: Phaser.GameObjects.Image; assetKey: string }> = [];
  private forestSprites: Array<{ sprite: Phaser.GameObjects.Image; treeType: number }> = [];

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
    AgentSprite.preload(this);
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

    // --- Layer 2: Water areas ---
    this.createWaterAreas();

    // --- Layer 3: Ground paths ---
    this.createGroundPaths();

    // --- Layer 4: Ground-level decorations ---
    this.placeGrassDetails();
    this.placeGroundDetails();
    this.placeWaterDetails();
    this.placeTerrainTransitions();
    this.placeFences();

    // --- Layer 5: Landmarks (pit, stairs, bridge) ---
    this.placeLandmarks();

    // --- Layer 6: Y-sorted objects (buildings, village trees) ---
    this.placeBuildings();
    this.placeVillageTrees();

    // --- Forest (outside village) ---
    this.generateForest(canvasW, canvasH);

    // --- Agent ---
    const startPos = this.worldPos(SCENE.POSITIONS.bench);
    this.agent = new AgentSprite(this, startPos.x, startPos.y);

    // --- Speech Bubble ---
    this.speechBubble = new SpeechBubble(this, SCENE.MAP_PIXEL_WIDTH, this.villageOffsetX);
    this.speechBubble.setTarget(this.agent.sprite);

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
      const pos = this.worldPos(SCENE.POSITIONS.bench);
      this.agent.moveTo(pos.x, pos.y, () => {
        this.agent.playAnim("idle");
        this.speechBubble.show(payload.text);

        this.time.delayedCall(6000, () => {
          this.speechBubble.hide();
          EventBus.emit("agent-speech-done");
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

    if (this.resizeDebounceTimer) clearTimeout(this.resizeDebounceTimer);
    if (this.dayNightTimer) this.dayNightTimer.remove(false);

    this.agent.destroy();
    this.speechBubble.destroy();
  }

  // ─── Grass Base ──────────────────────────────────────

  private createGrassBase(canvasW: number, canvasH: number) {
    this.grassTileSprite = this.add.tileSprite(0, 0, canvasW, canvasH, this.texKey("grass-tile"));
    this.grassTileSprite.setOrigin(0, 0);
    this.grassTileSprite.setDepth(-10);
  }

  // ─── Water Areas ─────────────────────────────────────

  private createWaterAreas() {
    for (const area of WATER_AREAS) {
      const w = area.cols * SCENE.TILE_SIZE;
      const h = area.rows * SCENE.TILE_SIZE;
      const wp = this.worldPos({ x: area.x, y: area.y });
      const ts = this.add.tileSprite(wp.x, wp.y, w, h, this.texKey("water-tile"));
      ts.setOrigin(0, 0);
      ts.setDepth(-8);
      this.waterAreas.push(ts);
    }
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

  // ─── Terrain Transitions ─────────────────────────────

  private placeTerrainTransitions() {
    for (const t of TERRAIN_PATCHES) {
      const wp = this.worldPos(t);
      const sprite = this.add.image(wp.x, wp.y, this.texKey(t.key));
      sprite.setOrigin(0, 0);
      sprite.setDepth(-2);
      this.envSprites.push({ sprite, assetKey: t.key });
    }
  }

  // ─── Grass Details ───────────────────────────────────

  private placeGrassDetails() {
    for (const g of GRASS_DETAILS) {
      const wp = this.worldPos(g);
      const key = `grass-detail-${g.type}`;
      const sprite = this.add.image(wp.x, wp.y, this.texKey(key));
      sprite.setOrigin(0, 0);
      sprite.setDepth(-4);
      this.envSprites.push({ sprite, assetKey: key });
    }
  }

  // ─── Ground Details ──────────────────────────────────

  private placeGroundDetails() {
    for (const g of GROUND_DETAILS) {
      const wp = this.worldPos(g);
      const key = `ground-detail-${g.type}`;
      const sprite = this.add.image(wp.x, wp.y, this.texKey(key));
      sprite.setOrigin(0, 0);
      sprite.setDepth(-3);
      this.envSprites.push({ sprite, assetKey: key });
    }
  }

  // ─── Water Details ───────────────────────────────────

  private placeWaterDetails() {
    for (const w of WATER_DETAILS) {
      const wp = this.worldPos(w);
      const key = `water-detail-${w.type}`;
      const sprite = this.add.image(wp.x, wp.y, this.texKey(key));
      sprite.setOrigin(0, 0);
      sprite.setDepth(-1.5);
      this.envSprites.push({ sprite, assetKey: key });
    }
  }

  // ─── Fences ──────────────────────────────────────────

  private placeFences() {
    for (const f of FENCE_POSITIONS) {
      const wp = this.worldPos(f);
      const key = `fence-${f.type}`;
      const sprite = this.add.image(wp.x, wp.y, this.texKey(key));
      sprite.setOrigin(0, 0);
      sprite.setDepth(-1);
      this.envSprites.push({ sprite, assetKey: key });
    }
  }

  // ─── Landmarks ───────────────────────────────────────

  private placeLandmarks() {
    // Village well (pit)
    const wellWp = this.worldPos(WELL_POS);
    const wellSprite = this.add.image(wellWp.x, wellWp.y, this.texKey("pit"));
    wellSprite.setOrigin(0.5, 1);
    wellSprite.setDepth(-0.3);
    this.envSprites.push({ sprite: wellSprite, assetKey: "pit" });

    // Stone stairs
    const stairsWp = this.worldPos(STAIRS_POS);
    const stairsSprite = this.add.image(stairsWp.x, stairsWp.y, this.texKey("stairs"));
    stairsSprite.setOrigin(0.5, 1);
    stairsSprite.setDepth(-0.3);
    this.envSprites.push({ sprite: stairsSprite, assetKey: "stairs" });

    // Bridge over pond
    const bridgeWp = this.worldPos(BRIDGE_POS);
    const bridgeSprite = this.add.image(bridgeWp.x, bridgeWp.y, this.texKey("bridge"));
    bridgeSprite.setOrigin(0.5, 1);
    bridgeSprite.setDepth(-0.2);
    this.envSprites.push({ sprite: bridgeSprite, assetKey: "bridge" });
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

    for (const ts of this.waterAreas) {
      ts.setTexture(VILLAGE_ASSETS["water-tile"][tod]);
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

    // Reposition water areas
    WATER_AREAS.forEach((area, i) => {
      const wp = this.worldPos({ x: area.x, y: area.y });
      this.waterAreas[i].setPosition(wp.x, wp.y);
    });

    // Move all env sprites by delta
    for (const entry of this.envSprites) {
      entry.sprite.x += dx;
      entry.sprite.y += dy;
    }

    // Regenerate forest
    this.generateForest(canvasW, canvasH);

    // Move agent
    this.agent.sprite.x += dx;
    this.agent.sprite.y += dy;

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
