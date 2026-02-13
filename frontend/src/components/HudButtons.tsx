import { useChatStore } from "../stores/chatStore";

const AMBIENT_DOT_COLORS: Record<string, string> = {
  goal_behind: "bg-red-500 animate-pulse",
  on_track: "bg-green-500",
  has_insight: "bg-yellow-400 animate-pulse",
  waiting: "bg-blue-400 animate-pulse",
};

export function HudButtons() {
  const chatPanelOpen = useChatStore((s) => s.chatPanelOpen);
  const questPanelOpen = useChatStore((s) => s.questPanelOpen);
  const settingsPanelOpen = useChatStore((s) => s.settingsPanelOpen);
  const setChatPanelOpen = useChatStore((s) => s.setChatPanelOpen);
  const setQuestPanelOpen = useChatStore((s) => s.setQuestPanelOpen);
  const setSettingsPanelOpen = useChatStore((s) => s.setSettingsPanelOpen);
  const ambientState = useChatStore((s) => s.ambientState);
  const ambientTooltip = useChatStore((s) => s.ambientTooltip);

  const dotClass = AMBIENT_DOT_COLORS[ambientState];

  return (
    <div className="fixed bottom-4 left-4 z-50 flex gap-2 items-center">
      {!chatPanelOpen && (
        <button
          onClick={() => setChatPanelOpen(true)}
          className="rpg-frame px-3 py-2 text-[11px] text-retro-border hover:text-retro-highlight uppercase tracking-wider transition-colors cursor-pointer"
          title="Open Chat Log (Shift+C)"
        >
          Chat
        </button>
      )}
      {!questPanelOpen && (
        <button
          onClick={() => setQuestPanelOpen(true)}
          className="rpg-frame px-3 py-2 text-[11px] text-retro-border hover:text-retro-highlight uppercase tracking-wider transition-colors cursor-pointer"
          title="Open Quest Log (Shift+Q)"
        >
          Quests
        </button>
      )}
      {!settingsPanelOpen && (
        <button
          onClick={() => setSettingsPanelOpen(true)}
          className="rpg-frame px-3 py-2 text-[11px] text-retro-border hover:text-retro-highlight uppercase tracking-wider transition-colors cursor-pointer"
          title="Open Settings (Shift+S)"
        >
          Settings
        </button>
      )}
      {dotClass && (
        <div
          className={`rpg-frame w-5 h-5 flex items-center justify-center`}
          title={ambientTooltip || ambientState}
        >
          <span className={`block w-2 h-2 rounded-full ${dotClass}`} />
        </div>
      )}
    </div>
  );
}
