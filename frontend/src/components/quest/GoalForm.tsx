import { useState } from "react";
import { useQuestStore } from "../../stores/questStore";
import type { GoalInfo } from "../../types";

const LEVELS = ["daily", "weekly", "monthly", "quarterly", "annual", "vision"] as const;
const DOMAINS = ["productivity", "performance", "health", "influence", "growth"] as const;

interface Props {
  goal?: GoalInfo;
  onClose: () => void;
}

export function GoalForm({ goal, onClose }: Props) {
  const createGoal = useQuestStore((s) => s.createGoal);
  const updateGoal = useQuestStore((s) => s.updateGoal);

  const [title, setTitle] = useState(goal?.title ?? "");
  const [level, setLevel] = useState(goal?.level ?? "weekly");
  const [domain, setDomain] = useState(goal?.domain ?? "productivity");
  const [description, setDescription] = useState(goal?.description ?? "");
  const [dueDate, setDueDate] = useState(goal?.due_date?.split("T")[0] ?? "");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  const isEdit = !!goal;

  const handleSubmit = async () => {
    if (!title.trim()) {
      setError("Title is required");
      return;
    }
    setSaving(true);
    try {
      if (isEdit) {
        await updateGoal(goal.id, {
          title: title.trim(),
          level,
          domain,
          description: description.trim() || undefined,
          due_date: dueDate || null,
        });
      } else {
        await createGoal({
          title: title.trim(),
          level,
          domain,
          description: description.trim() || undefined,
          due_date: dueDate || undefined,
        });
      }
      onClose();
    } catch {
      setError("Failed to save");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center">
      <div className="absolute inset-0 bg-black/50" onClick={onClose} />
      <div className="rpg-frame p-3 w-[280px] max-w-[90vw] relative z-10 space-y-2">
        <div className="text-[10px] uppercase tracking-wider text-retro-highlight font-bold mb-2">
          {isEdit ? "Edit Quest" : "New Quest"}
        </div>

        <input
          type="text"
          placeholder="Quest title"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          className="w-full bg-transparent text-[9px] text-retro-text border-b border-retro-text/20 px-0.5 py-1 outline-none focus:border-retro-highlight"
          autoFocus
        />

        <div className="flex gap-2">
          <div className="flex-1">
            <div className="text-[8px] text-retro-text/40 mb-0.5">Level</div>
            <select
              value={level}
              onChange={(e) => setLevel(e.target.value)}
              className="w-full bg-retro-bg text-[9px] text-retro-text border border-retro-text/20 rounded-sm px-0.5 py-0.5 outline-none focus:border-retro-highlight"
            >
              {LEVELS.map((l) => (
                <option key={l} value={l}>{l}</option>
              ))}
            </select>
          </div>
          <div className="flex-1">
            <div className="text-[8px] text-retro-text/40 mb-0.5">Domain</div>
            <select
              value={domain}
              onChange={(e) => setDomain(e.target.value)}
              className="w-full bg-retro-bg text-[9px] text-retro-text border border-retro-text/20 rounded-sm px-0.5 py-0.5 outline-none focus:border-retro-highlight"
            >
              {DOMAINS.map((d) => (
                <option key={d} value={d}>{d}</option>
              ))}
            </select>
          </div>
        </div>

        <textarea
          placeholder="Description (optional)"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          rows={2}
          className="w-full bg-transparent text-[9px] text-retro-text border border-retro-text/20 rounded-sm px-1 py-1 outline-none focus:border-retro-highlight resize-none"
        />

        <div>
          <div className="text-[8px] text-retro-text/40 mb-0.5">Due date (optional)</div>
          <input
            type="date"
            value={dueDate}
            onChange={(e) => setDueDate(e.target.value)}
            className="bg-retro-bg text-[9px] text-retro-text border border-retro-text/20 rounded-sm px-1 py-0.5 outline-none focus:border-retro-highlight"
          />
        </div>

        {error && <div className="text-[8px] text-red-400">{error}</div>}

        <div className="flex gap-2 pt-1">
          <button
            onClick={handleSubmit}
            disabled={saving}
            className="text-[9px] text-retro-highlight hover:text-retro-text uppercase tracking-wider font-bold"
          >
            {saving ? "Saving..." : isEdit ? "Update" : "Create"}
          </button>
          <button
            onClick={onClose}
            className="text-[9px] text-retro-text/40 hover:text-retro-text uppercase tracking-wider"
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}
