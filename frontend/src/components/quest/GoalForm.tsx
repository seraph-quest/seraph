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
    <div className="cockpit-modal-shell cockpit-modal-shell--raised">
      <button
        type="button"
        className="cockpit-modal-backdrop"
        onClick={onClose}
        aria-label="Close priority editor"
      />
      <section className="cockpit-modal-card cockpit-modal-card--goal-editor">
        <div className="cockpit-modal-header">
          <div>
            <div className="cockpit-card-title">{isEdit ? "Edit Priority" : "New Priority"}</div>
            <div className="cockpit-card-meta">stored in the structured goal system</div>
          </div>
          <button
            type="button"
            className="cockpit-modal-close"
            aria-label="Close priority editor"
            title="Close priority editor"
            onClick={onClose}
          >
            x
          </button>
        </div>
        <div className="cockpit-modal-body cockpit-modal-form cockpit-tone-scope cockpit-goals-scope">
          <input
            type="text"
            placeholder="Priority title"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            className="w-full bg-transparent text-[11px] text-slate-100 border-b border-white/10 px-0.5 py-1 outline-none focus:border-cyan-300 placeholder:text-slate-500"
            autoFocus
          />

          <div className="flex gap-2">
            <div className="flex-1">
              <div className="text-[10px] text-slate-400 mb-1 uppercase tracking-[0.16em]">Level</div>
              <select
                value={level}
                onChange={(e) => setLevel(e.target.value)}
                className="w-full bg-slate-950/70 text-[11px] text-slate-100 border border-white/10 rounded-md px-2 py-1.5 outline-none focus:border-cyan-300"
              >
                {LEVELS.map((l) => (
                  <option key={l} value={l}>{l}</option>
                ))}
              </select>
            </div>
            <div className="flex-1">
              <div className="text-[10px] text-slate-400 mb-1 uppercase tracking-[0.16em]">Domain</div>
              <select
                value={domain}
                onChange={(e) => setDomain(e.target.value)}
                className="w-full bg-slate-950/70 text-[11px] text-slate-100 border border-white/10 rounded-md px-2 py-1.5 outline-none focus:border-cyan-300"
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
            rows={3}
            className="w-full bg-slate-950/70 text-[11px] text-slate-100 border border-white/10 rounded-md px-2 py-2 outline-none focus:border-cyan-300 resize-none placeholder:text-slate-500"
          />

          <div>
            <div className="text-[10px] text-slate-400 mb-1 uppercase tracking-[0.16em]">
              Due date (optional)
            </div>
            <input
              type="date"
              value={dueDate}
              onChange={(e) => setDueDate(e.target.value)}
              className="bg-slate-950/70 text-[11px] text-slate-100 border border-white/10 rounded-md px-2 py-1.5 outline-none focus:border-cyan-300"
            />
          </div>

          {error && <div className="text-[10px] text-rose-400">{error}</div>}

          <div className="flex gap-2 pt-1">
            <button
              onClick={handleSubmit}
              disabled={saving}
              className="cockpit-action cockpit-action--primary disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {saving ? "Saving..." : isEdit ? "Update" : "Create"}
            </button>
            <button
              onClick={onClose}
              className="cockpit-action cockpit-action--ghost"
            >
              Cancel
            </button>
          </div>
        </div>
      </section>
    </div>
  );
}
