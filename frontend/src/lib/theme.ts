import type { ThemePreference } from "../stores/chatStore";

export type ResolvedTheme = "dark" | "light";

export function resolveThemePreference(themePreference: ThemePreference, prefersLight: boolean): ResolvedTheme {
  if (themePreference === "system") {
    return prefersLight ? "light" : "dark";
  }
  return themePreference;
}

export function readThemePreference(): ThemePreference {
  if (typeof window === "undefined") {
    return "system";
  }
  let value: string | null = null;
  try {
    value = window.localStorage?.getItem("seraph_theme_preference") ?? null;
  } catch {
    return "system";
  }
  if (value === "dark" || value === "light" || value === "system") {
    return value;
  }
  return "system";
}

export function systemPrefersLight(): boolean {
  return typeof window !== "undefined"
    && typeof window.matchMedia === "function"
    && window.matchMedia("(prefers-color-scheme: light)").matches;
}

export function applyThemePreference(themePreference: ThemePreference, root: HTMLElement = document.documentElement): ResolvedTheme {
  const resolvedTheme = resolveThemePreference(themePreference, systemPrefersLight());
  root.dataset.theme = resolvedTheme;
  root.style.colorScheme = resolvedTheme;
  return resolvedTheme;
}
