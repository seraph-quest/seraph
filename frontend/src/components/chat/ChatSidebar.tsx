import { useChatStore } from "../../stores/chatStore";
import { SessionList } from "./SessionList";
import { SettingsView } from "./SettingsView";

export function ChatSidebar() {
  const settingsOpen = useChatStore((s) => s.settingsOpen);
  const setSettingsOpen = useChatStore((s) => s.setSettingsOpen);

  return (
    <div className="w-[160px] min-w-[160px] flex flex-col border-r border-retro-border/20 h-full">
      {settingsOpen ? (
        <SettingsView />
      ) : (
        <>
          <div className="flex-1 min-h-0 overflow-y-auto retro-scrollbar">
            <SessionList />
          </div>
          <div className="border-t border-retro-border/20">
            <button
              onClick={() => setSettingsOpen(true)}
              className="w-full text-[8px] text-retro-text/40 hover:text-retro-highlight text-left px-2 py-1.5 uppercase tracking-wider"
            >
              Settings
            </button>
          </div>
        </>
      )}
    </div>
  );
}
