import { useState, useEffect } from "react";
import { API_URL } from "../../config/constants";
import { useChatStore } from "../../stores/chatStore";

type McpPolicyMode = "disabled" | "approval" | "full";

const MODES: { value: McpPolicyMode; label: string; desc: string }[] = [
  { value: "disabled", label: "Disabled", desc: "Hide external MCP tools completely" },
  { value: "approval", label: "Ask First", desc: "Allow MCP tools, but approve every call" },
  { value: "full", label: "Full", desc: "Treat MCP tools like the current connected runtime" },
];

export function McpPolicyModeToggle() {
  const [mode, setMode] = useState<McpPolicyMode>("full");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    fetch(`${API_URL}/api/settings/mcp-policy-mode`)
      .then((r) => r.ok ? r.json() : null)
      .then((data) => {
        if (data?.mode) setMode(data.mode);
      })
      .catch(() => {});
  }, []);

  const handleSelect = async (m: McpPolicyMode) => {
    if (m === mode || loading) return;
    setLoading(true);
    try {
      const res = await fetch(`${API_URL}/api/settings/mcp-policy-mode`, {
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
        MCP Access
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
