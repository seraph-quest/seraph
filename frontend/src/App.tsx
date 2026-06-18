import { useEffect } from "react";
import { QuestPanel } from "./components/quest/QuestPanel";
import { SettingsPanel } from "./components/SettingsPanel";
import { useWebSocket } from "./hooks/useWebSocket";
import { useKeyboardShortcuts } from "./hooks/useKeyboardShortcuts";
import { CockpitView } from "./components/cockpit/CockpitView";
import { applyThemePreference } from "./lib/theme";
import { useChatStore } from "./stores/chatStore";

export default function App() {
  const { sendMessage, skipOnboarding } = useWebSocket();
  const themePreference = useChatStore((s) => s.themePreference);
  useKeyboardShortcuts();

  useEffect(() => {
    if (typeof document === "undefined") return;

    const root = document.documentElement;
    const media = typeof window !== "undefined" && typeof window.matchMedia === "function"
      ? window.matchMedia("(prefers-color-scheme: light)")
      : null;

    const applyTheme = () => {
      applyThemePreference(themePreference, root);
    };

    applyTheme();

    if (themePreference !== "system" || !media) {
      return () => {};
    }

    const handleChange = () => applyTheme();
    if (typeof media.addEventListener === "function") {
      media.addEventListener("change", handleChange);
      return () => media.removeEventListener("change", handleChange);
    }
    media.addListener(handleChange);
    return () => media.removeListener(handleChange);
  }, [themePreference]);

  return (
    <>
      <CockpitView onSend={sendMessage} onSkipOnboarding={skipOnboarding} />
      <QuestPanel />
      <SettingsPanel />
    </>
  );
}
