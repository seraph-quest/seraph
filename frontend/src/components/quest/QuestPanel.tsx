import { useEffect, useMemo, useState } from "react";
import { useChatStore } from "../../stores/chatStore";
import { useQuestStore } from "../../stores/questStore";
import { useDragResize } from "../../hooks/useDragResize";
import { ResizeHandles } from "../ResizeHandles";
import { DialogFrame } from "../chat/DialogFrame";
import { DomainStats } from "./DomainStats";
import { GoalTree } from "./GoalTree";
import { GoalForm } from "./GoalForm";
import type { GoalInfo } from "../../types";

const LEVELS = ["daily", "weekly", "monthly", "quarterly", "annual", "vision"] as const;
const DOMAINS = ["productivity", "performance", "health", "influence", "growth"] as const;

function filterGoals(
  goals: GoalInfo[],
  search: string,
  level: string,
  domain: string,
): GoalInfo[] {
  const lowerSearch = search.toLowerCase();
  return goals.reduce<GoalInfo[]>((acc, goal) => {
    const filteredChildren = goal.children
      ? filterGoals(goal.children, search, level, domain)
      : [];
    const matchesSearch =
      !search || goal.title.toLowerCase().includes(lowerSearch);
    const matchesLevel = !level || goal.level === level;
    const matchesDomain = !domain || goal.domain === domain;
    const selfMatches = matchesSearch && matchesLevel && matchesDomain;
    if (selfMatches || filteredChildren.length > 0) {
      acc.push({
        ...goal,
        children: filteredChildren.length > 0 ? filteredChildren : selfMatches ? goal.children : [],
      });
    }
    return acc;
  }, []);
}

export function QuestPanel() {
  const questPanelOpen = useChatStore((s) => s.questPanelOpen);
  const setQuestPanelOpen = useChatStore((s) => s.setQuestPanelOpen);
  const { goalTree, dashboard, refresh, loading } = useQuestStore();
  const [editingGoal, setEditingGoal] = useState<GoalInfo | "new" | null>(null);

  const [search, setSearch] = useState("");
  const [filterLevel, setFilterLevel] = useState("");
  const [filterDomain, setFilterDomain] = useState("");

  const { dragHandleProps, resizeHandleProps, style, bringToFront } = useDragResize("quest", {
    minWidth: 240,
    minHeight: 200,
  });

  useEffect(() => {
    if (questPanelOpen) refresh();
  }, [questPanelOpen, refresh]);

  const filteredTree = useMemo(
    () =>
      search || filterLevel || filterDomain
        ? filterGoals(goalTree, search, filterLevel, filterDomain)
        : goalTree,
    [goalTree, search, filterLevel, filterDomain],
  );

  const hasFilters = !!(search || filterLevel || filterDomain);

  if (!questPanelOpen) return null;

  return (
    <div
      className="quest-overlay"
      style={style}
      onPointerDown={bringToFront}
    >
      <ResizeHandles resizeHandleProps={resizeHandleProps} />
      <DialogFrame
        title="Quest Log"
        className="flex-1 min-h-0 flex flex-col"
        onClose={() => setQuestPanelOpen(false)}
        dragHandleProps={dragHandleProps}
      >
        <div className="flex-1 min-h-0 overflow-y-auto retro-scrollbar flex flex-col gap-2 pb-1">
          {dashboard && <DomainStats dashboard={dashboard} />}

          <div className="border-t border-retro-border/20 my-1" />

          <div className="px-1">
            <div className="flex items-center justify-between mb-2">
              <div className="text-[10px] uppercase tracking-wider text-retro-border font-bold">
                Active Quests
              </div>
              <button
                onClick={() => setEditingGoal("new")}
                className="text-[11px] text-retro-text/40 hover:text-retro-highlight px-1"
                title="Add new quest"
              >
                +
              </button>
            </div>

            <div className="flex flex-col gap-1 mb-2">
              <input
                type="text"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search quests..."
                className="w-full bg-transparent text-[10px] text-retro-text border border-retro-text/20 rounded-sm px-1.5 py-0.5 outline-none focus:border-retro-highlight placeholder:text-retro-text/30"
              />
              <div className="flex gap-1">
                <select
                  value={filterLevel}
                  onChange={(e) => setFilterLevel(e.target.value)}
                  className="flex-1 bg-retro-bg text-[9px] text-retro-text border border-retro-text/20 rounded-sm px-0.5 py-0.5 outline-none focus:border-retro-highlight"
                >
                  <option value="">All levels</option>
                  {LEVELS.map((l) => (
                    <option key={l} value={l}>{l}</option>
                  ))}
                </select>
                <select
                  value={filterDomain}
                  onChange={(e) => setFilterDomain(e.target.value)}
                  className="flex-1 bg-retro-bg text-[9px] text-retro-text border border-retro-text/20 rounded-sm px-0.5 py-0.5 outline-none focus:border-retro-highlight"
                >
                  <option value="">All domains</option>
                  {DOMAINS.map((d) => (
                    <option key={d} value={d}>{d}</option>
                  ))}
                </select>
              </div>
            </div>

            {loading ? (
              <div className="text-[10px] text-retro-text/40 italic">Loading...</div>
            ) : filteredTree.length === 0 ? (
              <div className="text-[10px] text-retro-text/40 italic">
                {hasFilters
                  ? "No quests match filters."
                  : "No quests yet. Chat with Seraph to set goals!"}
              </div>
            ) : (
              <GoalTree
                goals={filteredTree}
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
