import { useQuestStore } from "../../stores/questStore";
import type { GoalInfo } from "../../types";

const STATUS_ICONS: Record<string, string> = {
  active: "[ ]",
  completed: "[x]",
  paused: "[-]",
  abandoned: "[~]",
};

const LEVEL_COLORS: Record<string, string> = {
  vision: "text-retro-highlight",
  annual: "text-retro-border",
  quarterly: "text-retro-text",
  monthly: "text-retro-text/80",
  weekly: "text-retro-text/70",
  daily: "text-retro-text/60",
};

interface Props {
  goals: GoalInfo[];
  depth: number;
  onEdit?: (goal: GoalInfo) => void;
}

export function GoalTree({ goals, depth, onEdit }: Props) {
  const updateGoal = useQuestStore((s) => s.updateGoal);
  const deleteGoal = useQuestStore((s) => s.deleteGoal);

  return (
    <div className={depth > 0 ? "ml-3 border-l border-retro-border/15 pl-2" : ""}>
      {goals.map((goal) => {
        const icon = STATUS_ICONS[goal.status] ?? "[ ]";
        const color = LEVEL_COLORS[goal.level] ?? "text-retro-text/60";
        const isCompleted = goal.status === "completed";

        return (
          <div key={goal.id} className="mb-1">
            <div className="flex items-start gap-1 group">
              <button
                className={`text-[10px] font-mono shrink-0 ${
                  isCompleted ? "text-green-400/70" : "text-retro-text/50"
                } hover:text-retro-highlight`}
                onClick={() => {
                  updateGoal(goal.id, {
                    status: isCompleted ? "active" : "completed",
                  });
                }}
              >
                {icon}
              </button>
              <div className="flex-1 min-w-0">
                <span
                  className={`text-[10px] ${color} ${
                    isCompleted ? "line-through opacity-50" : ""
                  }`}
                >
                  {goal.title}
                </span>
                {goal.due_date && (
                  <span className="text-[8px] text-retro-text/30 ml-1">
                    {new Date(goal.due_date).toLocaleDateString("en-US", {
                      month: "short",
                      day: "numeric",
                    })}
                  </span>
                )}
                <span className="text-[8px] text-retro-text/20 ml-1 hidden group-hover:inline">
                  {goal.level}
                </span>
              </div>
              <div className="hidden group-hover:flex items-center gap-0.5 shrink-0">
                {onEdit && (
                  <button
                    onClick={() => onEdit(goal)}
                    className="text-[8px] text-retro-text/30 hover:text-retro-highlight px-0.5"
                    title="Edit quest"
                  >
                    /
                  </button>
                )}
                <button
                  onClick={() => {
                    if (window.confirm(`Delete "${goal.title}"?`)) {
                      deleteGoal(goal.id);
                    }
                  }}
                  className="text-[8px] text-retro-text/30 hover:text-red-400 px-0.5"
                  title="Delete quest"
                >
                  x
                </button>
              </div>
            </div>
            {goal.children && goal.children.length > 0 && (
              <GoalTree goals={goal.children} depth={depth + 1} onEdit={onEdit} />
            )}
          </div>
        );
      })}
    </div>
  );
}
