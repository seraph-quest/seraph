import { useCallback, useEffect, useState } from "react";

import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";

type SourceWatch = {
  id: string;
  goal_id: string;
  goal_revision: number;
  plan_revision: number;
  state: string;
  write_mode: string;
  sources: Array<{ source_key: string; kind: string; target: string; priority: number }>;
  last_status?: string | null;
  last_error_code?: string | null;
  latest_packet?: { status?: string; approval_id?: string | null } | null;
};

export function SourceWatchPanel() {
  const [watches, setWatches] = useState<SourceWatch[]>([]);
  const [goalId, setGoalId] = useState("");
  const [goalRevision, setGoalRevision] = useState("1");
  const [source, setSource] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const loadWatches = useCallback(async () => {
    try {
      const response = await apiFetch(`${API_URL}/api/capabilities/source-watches`);
      if (!response.ok) {
        setStatus("Unable to read guardian watches");
        return;
      }
      const payload = await response.json() as SourceWatch[] | { watches?: SourceWatch[] };
      setWatches(Array.isArray(payload) ? payload : Array.isArray(payload.watches) ? payload.watches : []);
      setStatus(null);
    } catch {
      setStatus("Guardian watch status unavailable");
    }
  }, []);

  useEffect(() => {
    void loadWatches();
  }, [loadWatches]);

  const createWatch = async () => {
    if (!goalId.trim() || !source.trim()) {
      setStatus("Goal and HTTPS source are required");
      return;
    }
    setSaving(true);
    try {
      const response = await apiFetch(`${API_URL}/api/capabilities/source-watches`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          goal_id: goalId.trim(),
          expected_goal_revision: Number(goalRevision) || 1,
          sources: [{ source_key: "primary", kind: "public_https_text", target: source.trim(), priority: 3 }],
          criteria: { min_changed_lines: 1, min_changed_chars: 1, max_material_sources: 3 },
          schedule: { cron: "*/15 * * * *", timezone: "UTC" },
          write_mode: "approval_each_run",
        }),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({})) as { detail?: { code?: string } | string };
        const detail = typeof payload.detail === "string" ? payload.detail : payload.detail?.code;
        setStatus(detail ? `Watch blocked: ${detail}` : "Watch creation blocked");
        return;
      }
      setGoalId("");
      setSource("");
      setStatus("Guardian watch created; approval is required for local writes");
      await loadWatches();
    } catch {
      setStatus("Guardian watch creation failed");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="px-1">
      <div className="text-[10px] uppercase tracking-wider text-retro-border font-bold mb-1">
        Guardian source watches
      </div>
      <div className="text-[9px] text-retro-text/40 mb-2">
        Public HTTPS observations run no more often than every 15 minutes. Writes remain approval-bound.
      </div>
      <div className="space-y-1 border border-retro-text/10 rounded p-1 mb-2">
        <input
          aria-label="Guardian goal id"
          value={goalId}
          onChange={(event) => setGoalId(event.target.value)}
          placeholder="Goal id"
          className="w-full bg-transparent text-[9px] text-retro-text border-b border-retro-text/20 px-0.5 py-0.5 outline-none focus:border-retro-highlight"
        />
        <input
          aria-label="Guardian goal revision"
          value={goalRevision}
          onChange={(event) => setGoalRevision(event.target.value)}
          inputMode="numeric"
          placeholder="Goal revision"
          className="w-full bg-transparent text-[9px] text-retro-text border-b border-retro-text/20 px-0.5 py-0.5 outline-none focus:border-retro-highlight"
        />
        <input
          aria-label="Guardian HTTPS source"
          value={source}
          onChange={(event) => setSource(event.target.value)}
          placeholder="https://example.org/updates.txt"
          className="w-full bg-transparent text-[9px] text-retro-text border-b border-retro-text/20 px-0.5 py-0.5 outline-none focus:border-retro-highlight"
        />
        <button
          type="button"
          onClick={() => void createWatch()}
          disabled={saving}
          className="text-[9px] text-retro-highlight hover:text-retro-text uppercase tracking-wider disabled:text-retro-text/20"
        >
          {saving ? "Saving..." : "Add watch"}
        </button>
      </div>
      {watches.length > 0 ? (
        <div className="border border-retro-text/10 rounded">
          {watches.map((watch) => (
            <div key={watch.id} className="px-1 py-1 border-b border-retro-text/10 last:border-b-0">
              <div className="flex items-center gap-1 text-[10px] text-retro-text">
                <span className={`w-1.5 h-1.5 rounded-full ${watch.state === "active" ? "bg-green-400" : "bg-yellow-400"}`} />
                <span className="truncate">{watch.goal_id}</span>
                <span className="ml-auto text-retro-text/40">{watch.last_status ?? watch.state}</span>
              </div>
              <div className="text-[9px] text-retro-text/40">
                rev {watch.goal_revision}/{watch.plan_revision} · {watch.sources.length} source · {watch.write_mode}
              </div>
              {watch.last_error_code && <div className="text-[9px] text-amber-300/70">blocked · {watch.last_error_code}</div>}
              {watch.latest_packet?.status === "awaiting_approval" && <div className="text-[9px] text-retro-highlight">approval required for proposed intervention</div>}
            </div>
          ))}
        </div>
      ) : (
        <div className="text-[9px] text-retro-text/30">No guardian watches configured</div>
      )}
      {status && <div className="text-[9px] text-retro-highlight mt-1">{status}</div>}
    </div>
  );
}
