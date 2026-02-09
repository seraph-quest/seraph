import { useChatStore } from "../stores/chatStore";

export function HudButtons() {
  const chatPanelOpen = useChatStore((s) => s.chatPanelOpen);
  const questPanelOpen = useChatStore((s) => s.questPanelOpen);
  const setChatPanelOpen = useChatStore((s) => s.setChatPanelOpen);
  const setQuestPanelOpen = useChatStore((s) => s.setQuestPanelOpen);

  return (
    <div className="fixed bottom-4 left-4 z-50 flex gap-2">
      {!chatPanelOpen && (
        <button
          onClick={() => setChatPanelOpen(true)}
          className="rpg-frame px-3 py-2 text-[9px] text-retro-border hover:text-retro-highlight uppercase tracking-wider transition-colors cursor-pointer"
          title="Open Chat Log"
        >
          Chat
        </button>
      )}
      {!questPanelOpen && (
        <button
          onClick={() => setQuestPanelOpen(true)}
          className="rpg-frame px-3 py-2 text-[9px] text-retro-border hover:text-retro-highlight uppercase tracking-wider transition-colors cursor-pointer"
          title="Open Quest Log"
        >
          Quests
        </button>
      )}
    </div>
  );
}
