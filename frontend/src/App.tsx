import { QuestPanel } from "./components/quest/QuestPanel";
import { SettingsPanel } from "./components/SettingsPanel";
import { useWebSocket } from "./hooks/useWebSocket";
import { useKeyboardShortcuts } from "./hooks/useKeyboardShortcuts";
import { CockpitView } from "./components/cockpit/CockpitView";

export default function App() {
  const { sendMessage, skipOnboarding } = useWebSocket();
  useKeyboardShortcuts();

  return (
    <>
      <CockpitView onSend={sendMessage} onSkipOnboarding={skipOnboarding} />
      <QuestPanel />
      <SettingsPanel />
    </>
  );
}
