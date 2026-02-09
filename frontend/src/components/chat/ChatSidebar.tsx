import { SessionList } from "./SessionList";

export function ChatSidebar() {
  return (
    <div className="w-[160px] min-w-[160px] flex flex-col border-r border-retro-border/20 h-full">
      <div className="flex-1 min-h-0 overflow-y-auto retro-scrollbar">
        <SessionList />
      </div>
    </div>
  );
}
