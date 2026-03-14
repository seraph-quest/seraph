import { useEffect, useState } from "react";
import { API_URL } from "../../config/constants";

interface AuditEvent {
  id: string;
  event_type: string;
  tool_name: string | null;
  risk_level: string;
  policy_mode: string;
  summary: string;
  created_at: string;
}

const RISK_COLORS: Record<string, string> = {
  low: "text-green-400",
  medium: "text-yellow-400",
  high: "text-red-400",
};

function formatAge(iso: string): string {
  const then = new Date(iso).getTime();
  const deltaSeconds = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (deltaSeconds < 60) return `${deltaSeconds}s ago`;
  const deltaMinutes = Math.floor(deltaSeconds / 60);
  if (deltaMinutes < 60) return `${deltaMinutes}m ago`;
  const deltaHours = Math.floor(deltaMinutes / 60);
  return `${deltaHours}h ago`;
}

export function AuditLogPanel() {
  const [events, setEvents] = useState<AuditEvent[]>([]);

  useEffect(() => {
    const fetchEvents = () => {
      fetch(`${API_URL}/api/audit/events?limit=8`)
        .then((r) => (r.ok ? r.json() : []))
        .then((data) => setEvents(Array.isArray(data) ? data : []))
        .catch(() => {});
    };

    fetchEvents();
    const interval = setInterval(fetchEvents, 10_000);
    return () => clearInterval(interval);
  }, []);

  return (
    <div className="px-1">
      <div className="text-[10px] uppercase tracking-wider text-retro-border font-bold mb-1">
        Audit Log
      </div>
      {events.length > 0 ? (
        <div className="border border-retro-text/10 rounded">
          {events.map((event) => (
            <div
              key={event.id}
              className="px-1 py-0.5 border-b border-retro-text/10 last:border-b-0"
            >
              <div className="flex items-center gap-1">
                <div className={`text-[9px] uppercase ${RISK_COLORS[event.risk_level] ?? "text-retro-text/40"}`}>
                  {event.risk_level}
                </div>
                <div className="text-[10px] font-bold text-retro-text truncate">
                  {event.tool_name ?? event.event_type}
                </div>
                <div className="text-[9px] text-retro-text/30 ml-auto">
                  {formatAge(event.created_at)}
                </div>
              </div>
              <div className="text-[9px] text-retro-text/40 truncate">
                {event.summary}
              </div>
              <div className="text-[9px] text-retro-text/20 uppercase tracking-wider">
                {event.event_type} · {event.policy_mode}
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div className="text-[9px] text-retro-text/30 px-1">No audit events yet</div>
      )}
    </div>
  );
}
