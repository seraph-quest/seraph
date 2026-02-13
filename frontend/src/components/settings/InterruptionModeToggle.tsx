import { useState, useEffect } from "react";
import { API_URL } from "../../config/constants";

type Mode = "focus" | "balanced" | "active";

const MODES: { value: Mode; label: string; desc: string }[] = [
  { value: "focus", label: "Focus", desc: "Only scheduled briefings" },
  { value: "balanced", label: "Balanced", desc: "Smart timing (default)" },
  { value: "active", label: "Active", desc: "Real-time coaching" },
];

export function InterruptionModeToggle() {
  const [mode, setMode] = useState<Mode>("balanced");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    fetch(`${API_URL}/api/settings/interruption-mode`)
      .then((r) => r.ok ? r.json() : null)
      .then((data) => {
        if (data?.mode) setMode(data.mode);
      })
      .catch(() => {});
  }, []);

  const handleSelect = async (m: Mode) => {
    if (m === mode || loading) return;
    setLoading(true);
    try {
      const res = await fetch(`${API_URL}/api/settings/interruption-mode`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: m }),
      });
      if (res.ok) setMode(m);
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="px-1">
      <div className="text-[9px] uppercase tracking-wider text-retro-border font-bold mb-2">
        Interruption Mode
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
              <div className="text-[9px] font-bold uppercase tracking-wider">{m.label}</div>
              <div className="text-[7px] mt-0.5 opacity-70">{m.desc}</div>
            </button>
          );
        })}
      </div>
    </div>
  );
}
