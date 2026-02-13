/**
 * Lightweight Phaser mock factory for unit-testing game objects
 * (SpeechBubble, MagicEffect, etc.) without a real Phaser runtime.
 */
import { vi } from "vitest";

// ── Tiny helpers ────────────────────────────────────

export function createMockTimerEvent() {
  return { remove: vi.fn() };
}

export function createMockGraphics() {
  const g: Record<string, unknown> = {
    clear: vi.fn().mockReturnThis(),
    fillStyle: vi.fn().mockReturnThis(),
    fillRoundedRect: vi.fn().mockReturnThis(),
    lineStyle: vi.fn().mockReturnThis(),
    strokeRoundedRect: vi.fn().mockReturnThis(),
    fillTriangle: vi.fn().mockReturnThis(),
    lineBetween: vi.fn().mockReturnThis(),
    destroy: vi.fn(),
  };
  return g;
}

export function createMockText() {
  const t: Record<string, unknown> = {
    width: 40,
    height: 10,
    setText: vi.fn().mockImplementation(function (this: Record<string, unknown>, _msg: string) {
      // Simulate text resizing: short messages are narrower
      return this;
    }),
    setOrigin: vi.fn().mockReturnThis(),
    setPosition: vi.fn().mockReturnThis(),
    destroy: vi.fn(),
  };
  return t;
}

export function createMockSprite() {
  const s: Record<string, unknown> = {
    x: 100,
    y: 100,
    displayHeight: 24,
    scene: true, // truthy — MagicEffect checks `sprite.scene`
    setDepth: vi.fn().mockReturnThis(),
    setPosition: vi.fn().mockImplementation(function (this: Record<string, unknown>, x: number, y: number) {
      this.x = x;
      this.y = y;
      return this;
    }),
    setScale: vi.fn().mockReturnThis(),
    play: vi.fn().mockReturnThis(),
    destroy: vi.fn().mockImplementation(function (this: Record<string, unknown>) {
      this.scene = null;
    }),
  };
  return s;
}

/** Stateful container mock — SpeechBubble reads alpha/visible to decide fade vs swap */
export function createMockContainer() {
  const c: Record<string, unknown> = {
    alpha: 0,
    visible: false,
    width: 0,
    height: 0,
    setDepth: vi.fn().mockReturnThis(),
    setAlpha: vi.fn().mockImplementation(function (this: Record<string, unknown>, a: number) {
      this.alpha = a;
      return this;
    }),
    setVisible: vi.fn().mockImplementation(function (this: Record<string, unknown>, v: boolean) {
      this.visible = v;
      return this;
    }),
    setSize: vi.fn().mockImplementation(function (this: Record<string, unknown>, w: number, h: number) {
      this.width = w;
      this.height = h;
      return this;
    }),
    setPosition: vi.fn().mockReturnThis(),
    destroy: vi.fn(),
  };
  return c;
}

// ── Scene factory ───────────────────────────────────

export interface MockScene {
  add: {
    graphics: ReturnType<typeof vi.fn>;
    text: ReturnType<typeof vi.fn>;
    container: ReturnType<typeof vi.fn>;
    sprite: ReturnType<typeof vi.fn>;
  };
  tweens: {
    add: ReturnType<typeof vi.fn>;
    killTweensOf: ReturnType<typeof vi.fn>;
  };
  time: {
    delayedCall: ReturnType<typeof vi.fn>;
  };
  scale: { width: number };
  anims: {
    exists: ReturnType<typeof vi.fn>;
    create: ReturnType<typeof vi.fn>;
  };
  /** Captures last tween config so tests can inspect / fire onComplete */
  _lastTween: Record<string, unknown> | null;
  /** Captures last delayedCall callback */
  _lastDelayedCallback: (() => void) | null;
  /** Refs to created mocks */
  _graphics: ReturnType<typeof createMockGraphics>;
  _text: ReturnType<typeof createMockText>;
  _container: ReturnType<typeof createMockContainer>;
  _sprite: ReturnType<typeof createMockSprite>;
}

export function createMockScene(): MockScene {
  const graphics = createMockGraphics();
  const text = createMockText();
  const container = createMockContainer();
  const sprite = createMockSprite();

  const scene: MockScene = {
    _lastTween: null,
    _lastDelayedCallback: null,
    _graphics: graphics,
    _text: text,
    _container: container,
    _sprite: sprite,

    add: {
      graphics: vi.fn(() => graphics),
      text: vi.fn(() => text),
      container: vi.fn(() => container),
      sprite: vi.fn(() => sprite),
    },
    tweens: {
      add: vi.fn((config: Record<string, unknown>) => {
        scene._lastTween = config;
        return config;
      }),
      killTweensOf: vi.fn(),
    },
    time: {
      delayedCall: vi.fn((_delay: number, cb: () => void) => {
        scene._lastDelayedCallback = cb;
        return createMockTimerEvent();
      }),
    },
    scale: { width: 1024 },
    anims: {
      exists: vi.fn(() => false),
      create: vi.fn(),
    },
  };

  return scene;
}

// ── Default export: Phaser namespace stub ───────────

const PhaserMock = {
  Math: {
    Clamp: (value: number, min: number, max: number) =>
      Math.max(min, Math.min(max, value)),
  },
  GameObjects: {
    Container: class {},
    Graphics: class {},
    Text: class {},
    Sprite: class {},
  },
  Scene: class {},
};

export default PhaserMock;
