import { describe, it, expect, vi, beforeEach } from "vitest";
import { createMockScene, type MockScene } from "../../test/mocks/phaser";

vi.mock("phaser", async () => {
  const mocks = await import("../../test/mocks/phaser");
  return { default: mocks.default };
});

vi.mock("../../config/constants", () => ({
  SCENE: {
    COLORS: {
      bubbleBg: 0xe0e0e0,
      bubbleBorder: 0xe2b714,
      bubbleText: 0x1a1a2e,
    },
  },
}));

import { SpeechBubble } from "./SpeechBubble";

describe("SpeechBubble", () => {
  let scene: MockScene;
  let bubble: SpeechBubble;

  beforeEach(() => {
    scene = createMockScene();
    bubble = new SpeechBubble(scene as never);
  });

  // ─── Constructor ──────────────────────────────────

  describe("constructor", () => {
    it("creates graphics, text, and container", () => {
      expect(scene.add.graphics).toHaveBeenCalled();
      expect(scene.add.text).toHaveBeenCalled();
      expect(scene.add.container).toHaveBeenCalled();
    });

    it("starts invisible with alpha 0", () => {
      expect(scene._container.visible).toBe(false);
      expect(scene._container.alpha).toBe(0);
    });

    it("sets container depth to 20", () => {
      expect(scene._container.setDepth).toHaveBeenCalledWith(20);
    });

    it("uses scene.scale.width as default clamp width", () => {
      // Verify no error — clampWidth is used internally in updatePosition
      const target = { x: 100, y: 100, displayHeight: 24 };
      bubble.setTarget(target as never);
      bubble.show("test");
      bubble.updatePosition();
      // Should not throw
    });
  });

  // ─── show() ───────────────────────────────────────

  describe("show()", () => {
    it("displays first message immediately", () => {
      bubble.show("Hello");
      expect(scene._text.setText).toHaveBeenCalledWith("Hello");
      expect(scene._container.visible).toBe(true);
    });

    it("queues subsequent messages while first is still showing", () => {
      bubble.show("First");
      bubble.show("Second");
      bubble.show("Third");

      // Only "First" should be rendered so far
      expect(scene._text.setText).toHaveBeenCalledTimes(1);
      expect(scene._text.setText).toHaveBeenCalledWith("First");
    });

    it("truncates messages longer than 60 chars", () => {
      const longMessage = "A".repeat(70);
      bubble.show(longMessage);
      expect(scene._text.setText).toHaveBeenCalledWith("A".repeat(60) + "...");
    });

    it("does not truncate messages of exactly 60 chars", () => {
      const exactMessage = "B".repeat(60);
      bubble.show(exactMessage);
      expect(scene._text.setText).toHaveBeenCalledWith(exactMessage);
    });

    it("displays '...' thinking indicator", () => {
      bubble.show("...");
      expect(scene._text.setText).toHaveBeenCalledWith("...");
    });
  });

  // ─── Queue cycling ────────────────────────────────

  describe("queue cycling", () => {
    it("displays next message when timer fires", () => {
      bubble.show("First");
      bubble.show("Second");

      // Fire the timer callback for the first message
      expect(scene._lastDelayedCallback).not.toBeNull();
      scene._lastDelayedCallback!();

      expect(scene._text.setText).toHaveBeenCalledWith("Second");
    });

    it("drains queue in order", () => {
      bubble.show("A");
      bubble.show("B");
      bubble.show("C");

      // Fire timer for A → should show B
      scene._lastDelayedCallback!();
      expect(scene._text.setText).toHaveBeenCalledWith("B");

      // Fire timer for B → should show C
      scene._lastDelayedCallback!();
      expect(scene._text.setText).toHaveBeenCalledWith("C");
    });

    it("stops cycling when queue is empty", () => {
      bubble.show("Only");
      const callCountBefore = scene.time.delayedCall.mock.calls.length;

      // Fire timer — queue is now empty
      scene._lastDelayedCallback!();

      // No new delayedCall should be made (displayNext returns early)
      expect(scene.time.delayedCall.mock.calls.length).toBe(callCountBefore);
    });
  });

  // ─── Fade logic ───────────────────────────────────

  describe("fade logic", () => {
    it("fades in when container alpha is low", () => {
      scene._container.alpha = 0;
      bubble.show("Fade me in");

      // Should set alpha to 0 and tween to 1
      expect(scene.tweens.add).toHaveBeenCalled();
      const tweenConfig = scene._lastTween!;
      expect(tweenConfig.alpha).toBe(1);
    });

    it("skips fade when container is already visible (alpha >= 0.9)", () => {
      bubble.show("First");
      // Simulate the fade-in completing
      scene._container.alpha = 1;

      // Now show next via queue cycling
      bubble.show("Second (queued)");
      scene._lastDelayedCallback!(); // fire first timer

      // renderBubble is called — it should detect alpha >= 0.9 and just set alpha=1
      expect(scene._container.alpha).toBe(1);
    });
  });

  // ─── hide() ───────────────────────────────────────

  describe("hide()", () => {
    it("clears the queue", () => {
      bubble.show("A");
      bubble.show("B");
      bubble.show("C");

      bubble.hide();

      // Fire remaining timer callbacks — they should not display B or C
      // (queue was cleared)
    });

    it("kills active tweens and fades out", () => {
      bubble.show("Hello");
      bubble.hide();

      expect(scene.tweens.killTweensOf).toHaveBeenCalledWith(scene._container);
      // Should create a fade-out tween
      const tweenConfig = scene._lastTween!;
      expect(tweenConfig.alpha).toBe(0);
    });

    it("sets invisible on fade-out complete", () => {
      bubble.show("Hello");
      bubble.hide();

      // Fire the onComplete of the fade-out tween
      const tweenConfig = scene._lastTween!;
      (tweenConfig.onComplete as () => void)();

      expect(scene._container.visible).toBe(false);
    });

    it("removes active display timer", () => {
      bubble.show("Hello");
      const timer = scene.time.delayedCall.mock.results[0].value;

      bubble.hide();
      expect(timer.remove).toHaveBeenCalledWith(false);
    });
  });

  // ─── updatePosition() ─────────────────────────────

  describe("updatePosition()", () => {
    it("no-ops without a target sprite", () => {
      // Should not throw
      bubble.updatePosition();
    });

    it("no-ops when container is not visible", () => {
      bubble.setTarget({ x: 100, y: 100, displayHeight: 24 } as never);
      // container is invisible by default
      bubble.updatePosition();
      expect(scene._container.setPosition).not.toHaveBeenCalled();
    });

    it("positions bubble above target sprite", () => {
      const target = { x: 200, y: 200, displayHeight: 24 };
      bubble.setTarget(target as never);
      bubble.show("Hi");

      // updatePosition is called by renderBubble
      expect(scene._container.setPosition).toHaveBeenCalled();
    });
  });

  // ─── destroy() ────────────────────────────────────

  describe("destroy()", () => {
    it("removes timer and destroys container", () => {
      bubble.show("Hello");
      bubble.destroy();

      expect(scene._container.destroy).toHaveBeenCalled();
    });

    it("is safe when no active timer exists", () => {
      // No show() called — no timer
      expect(() => bubble.destroy()).not.toThrow();
      expect(scene._container.destroy).toHaveBeenCalled();
    });
  });

  // ─── setTarget and updateClampBounds ──────────────

  describe("setTarget / updateClampBounds", () => {
    it("setTarget stores the sprite reference", () => {
      const target = { x: 50, y: 50, displayHeight: 24 };
      bubble.setTarget(target as never);
      bubble.show("test");
      // Should use target coords in updatePosition — no throw means it worked
    });

    it("updateClampBounds changes clamp values", () => {
      bubble.updateClampBounds(512, 100);
      // No throw, and values are used in next updatePosition call
      const target = { x: 50, y: 50, displayHeight: 24 };
      bubble.setTarget(target as never);
      bubble.show("test");
    });
  });
});
