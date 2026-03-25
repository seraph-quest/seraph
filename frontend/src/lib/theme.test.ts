import { describe, expect, it, vi } from "vitest";

import { applyThemePreference, readThemePreference, resolveThemePreference } from "./theme";

describe("theme helpers", () => {
  it("resolves system preference using OS light mode", () => {
    expect(resolveThemePreference("system", true)).toBe("light");
    expect(resolveThemePreference("system", false)).toBe("dark");
    expect(resolveThemePreference("light", false)).toBe("light");
  });

  it("reads the persisted theme preference safely", () => {
    const getItem = vi.fn(() => "light");
    vi.stubGlobal("window", {
      localStorage: { getItem },
      matchMedia: vi.fn(),
    });

    expect(readThemePreference()).toBe("light");

    vi.unstubAllGlobals();
  });

  it("falls back to system when localStorage access throws", () => {
    const windowStub = {
      matchMedia: vi.fn(),
    } as unknown as Window & typeof globalThis;

    Object.defineProperty(windowStub, "localStorage", {
      get() {
        throw new DOMException("blocked", "SecurityError");
      },
    });

    vi.stubGlobal("window", windowStub);

    expect(readThemePreference()).toBe("system");

    vi.unstubAllGlobals();
  });

  it("applies the resolved theme to the document root", () => {
    const root = document.createElement("div");
    const matchMedia = vi.fn().mockReturnValue({ matches: true });
    vi.stubGlobal("window", {
      localStorage: { getItem: vi.fn(() => "system") },
      matchMedia,
    });

    expect(applyThemePreference("system", root)).toBe("light");
    expect(root.dataset.theme).toBe("light");
    expect(root.style.colorScheme).toBe("light");

    vi.unstubAllGlobals();
  });
});
