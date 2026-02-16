import { useState, useEffect } from "react";
import { API_URL } from "../../config/constants";

interface DaemonStatusData {
  connected: boolean;
  last_post: number | null;
  active_window: string | null;
  has_screen_context: boolean;
}

export function DaemonStatus() {
  const [status, setStatus] = useState<DaemonStatusData | null>(null);

  useEffect(() => {
    const fetchStatus = () => {
      fetch(`${API_URL}/api/observer/daemon-status`)
        .then((r) => (r.ok ? r.json() : null))
        .then((data) => {
          if (data) setStatus(data);
        })
        .catch(() => {});
    };

    fetchStatus();
    const interval = setInterval(fetchStatus, 10_000);
    return () => clearInterval(interval);
  }, []);

  const connected = status?.connected ?? false;
  const dotColor = connected ? "bg-green-400" : "bg-retro-text/30";
  const activeWindow = status?.active_window;

  // Truncate long window titles
  const displayWindow =
    activeWindow && activeWindow.length > 40
      ? activeWindow.slice(0, 37) + "..."
      : activeWindow;

  return (
    <div className="px-1">
      <div className="text-[10px] uppercase tracking-wider text-retro-border font-bold mb-2">
        Screen Observer
      </div>
      <div className="flex items-center gap-1.5 px-1">
        <div className={`w-2 h-2 rounded-full flex-shrink-0 ${dotColor}`} />
        <div className="flex-1 min-w-0">
          {connected ? (
            <>
              <div className="text-[10px] text-retro-text">Connected</div>
              {displayWindow && (
                <div className="text-[9px] text-retro-text/40 truncate">
                  {displayWindow}
                </div>
              )}
            </>
          ) : (
            <div className="text-[10px] text-retro-text/40">Not running</div>
          )}
        </div>
        {connected && status?.has_screen_context && (
          <div className="text-[9px] text-retro-text/30">OCR</div>
        )}
      </div>
    </div>
  );
}
