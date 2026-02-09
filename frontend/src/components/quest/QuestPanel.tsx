import { useEffect } from "react";
import { useChatStore } from "../../stores/chatStore";
import { useQuestStore } from "../../stores/questStore";
import { DialogFrame } from "../chat/DialogFrame";
import { DomainStats } from "./DomainStats";
import { GoalTree } from "./GoalTree";

export function QuestPanel() {
  const questPanelOpen = useChatStore((s) => s.questPanelOpen);
  const setQuestPanelOpen = useChatStore((s) => s.setQuestPanelOpen);
  const { goalTree, dashboard, refresh, loading } = useQuestStore();

  useEffect(() => {
    if (questPanelOpen) refresh();
  }, [questPanelOpen, refresh]);

  if (!questPanelOpen) return null;

  return (
    <div className="quest-overlay">
      <DialogFrame
        title="Quest Log"
        className="flex-1 min-h-0 flex flex-col"
        onClose={() => setQuestPanelOpen(false)}
      >
        <div className="flex-1 min-h-0 overflow-y-auto retro-scrollbar flex flex-col gap-2 pb-1">
          {dashboard && <DomainStats dashboard={dashboard} />}

          <div className="border-t border-retro-border/20 my-1" />

          <div className="px-1">
            <div className="text-[8px] uppercase tracking-wider text-retro-border font-bold mb-2">
              Active Quests
            </div>
            {loading ? (
              <div className="text-[9px] text-retro-text/40 italic">Loading...</div>
            ) : goalTree.length === 0 ? (
              <div className="text-[9px] text-retro-text/40 italic">
                No quests yet. Chat with Seraph to set goals!
              </div>
            ) : (
              <GoalTree goals={goalTree} depth={0} />
            )}
          </div>
        </div>
      </DialogFrame>
    </div>
  );
}
