import { useEffect } from "react";
import { QuestPanel } from "./components/quest/QuestPanel";
import { SettingsPanel } from "./components/SettingsPanel";
import { useWebSocket } from "./hooks/useWebSocket";
import { useKeyboardShortcuts } from "./hooks/useKeyboardShortcuts";
import { CockpitView } from "./components/cockpit/CockpitView";
import { applyThemePreference } from "./lib/theme";
import { useChatStore } from "./stores/chatStore";
import { AuthGate } from "./auth/AuthGate";
import { useAuthStore } from "./auth/authStore";

export default function App() {
  const authenticated = useAuthStore((state) => state.status === "authenticated");
  const authRevision = useAuthStore((state) => state.revision);
  const operatorName = useAuthStore((state) => state.operatorName);
  const logout = useAuthStore((state) => state.logout);
  const { sendMessage, skipOnboarding } = useWebSocket(authenticated, authRevision);
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

  return <AuthGate>{(
    <>
      <CockpitView onSend={sendMessage} onSkipOnboarding={skipOnboarding} />
      <QuestPanel />
      <SettingsPanel />
      <button className="auth-logout" onClick={() => void logout()} type="button" title="Lock operator cockpit">
        LOCK{operatorName ? ` · ${operatorName}` : ""}
      </button>
    </>
  )}</AuthGate>;
}
