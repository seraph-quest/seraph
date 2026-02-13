import { useEffect, useState } from "react";
import { useChatStore } from "../../stores/chatStore";
import { useQuestStore } from "../../stores/questStore";
import { DialogFrame } from "../chat/DialogFrame";
import { DomainStats } from "./DomainStats";
import { GoalTree } from "./GoalTree";
import { GoalForm } from "./GoalForm";
import type { GoalInfo } from "../../types";

export function QuestPanel() {
  const questPanelOpen = useChatStore((s) => s.questPanelOpen);
  const setQuestPanelOpen = useChatStore((s) => s.setQuestPanelOpen);
  const { goalTree, dashboard, refresh, loading } = useQuestStore();
  const [editingGoal, setEditingGoal] = useState<GoalInfo | "new" | null>(null);

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
            <div className="flex items-center justify-between mb-2">
              <div className="text-[9px] uppercase tracking-wider text-retro-border font-bold">
                Active Quests
              </div>
              <button
                onClick={() => setEditingGoal("new")}
                className="text-[10px] text-retro-text/40 hover:text-retro-highlight px-1"
                title="Add new quest"
              >
                +
              </button>
            </div>
            {loading ? (
              <div className="text-[9px] text-retro-text/40 italic">Loading...</div>
            ) : goalTree.length === 0 ? (
              <div className="text-[9px] text-retro-text/40 italic">
                No quests yet. Chat with Seraph to set goals!
              </div>
            ) : (
              <GoalTree
                goals={goalTree}
                depth={0}
                onEdit={(goal) => setEditingGoal(goal)}
              />
            )}
          </div>
        </div>
      </DialogFrame>

      {editingGoal !== null && (
        <GoalForm
          goal={editingGoal === "new" ? undefined : editingGoal}
          onClose={() => setEditingGoal(null)}
        />
      )}
    </div>
  );
}
