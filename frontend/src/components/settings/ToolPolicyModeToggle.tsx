import { useState, useEffect } from "react";
import { API_URL } from "../../config/constants";
import { useChatStore } from "../../stores/chatStore";

type ToolPolicyMode = "safe" | "balanced" | "full";

const MODES: { value: ToolPolicyMode; label: string; desc: string }[] = [
  { value: "safe", label: "Safe", desc: "Read-focused tools only" },
  { value: "balanced", label: "Balanced", desc: "Allow edits, block high-risk tools" },
  { value: "full", label: "Full", desc: "Current behavior, including shell and vault access" },
];

export function ToolPolicyModeToggle() {
  const [mode, setMode] = useState<ToolPolicyMode>("full");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    fetch(`${API_URL}/api/settings/tool-policy-mode`)
      .then((r) => r.ok ? r.json() : null)
      .then((data) => {
        if (data?.mode) setMode(data.mode);
      })
      .catch(() => {});
  }, []);

  const handleSelect = async (m: ToolPolicyMode) => {
    if (m === mode || loading) return;
    setLoading(true);
    try {
      const res = await fetch(`${API_URL}/api/settings/tool-policy-mode`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: m }),
      });
      if (res.ok) {
        setMode(m);
        await useChatStore.getState().fetchToolRegistry();
      }
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="px-1">
      <div className="text-[10px] uppercase tracking-wider text-retro-border font-bold mb-2">
        Tool Policy
      </div>
      <div className="flex gap-1">
        {MODES.map((m) => {
          const selected = mode === m.value;
          return (
            <button
              key={m.value}
              onClick={() => handleSelect(m.value)}
              disabled={loading}
              className={`flex-1 px-1 py-1 border rounded-sm text-center transition-colors ${
                selected
                  ? "text-retro-highlight border-retro-highlight"
                  : "text-retro-text/40 border-retro-text/20 hover:text-retro-text/60 hover:border-retro-text/40"
              }`}
            >
              <div className="text-[10px] font-bold uppercase tracking-wider">{m.label}</div>
              <div className="text-[9px] mt-0.5 opacity-70">{m.desc}</div>
            </button>
          );
        })}
      </div>
    </div>
  );
}
