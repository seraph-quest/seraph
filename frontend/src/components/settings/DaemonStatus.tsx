import { useState, useEffect } from "react";
import { API_URL } from "../../config/constants";

interface DaemonStatusData {
  connected: boolean;
  last_post: number | null;
  active_window: string | null;
  has_screen_context: boolean;
  capture_mode: "on_switch" | "balanced" | "detailed";
  pending_notification_count: number;
  last_native_notification_at: string | null;
  last_native_notification_title: string | null;
  last_native_notification_outcome: string | null;
}

interface PendingNotification {
  id: string;
  intervention_id: string | null;
  title: string;
  body: string;
  intervention_type: string | null;
  urgency: number | null;
  created_at: string;
}

interface PendingNotificationsData {
  notifications: PendingNotification[];
  pending_count: number;
}

export function DaemonStatus() {
  const [status, setStatus] = useState<DaemonStatusData | null>(null);
  const [notifications, setNotifications] = useState<PendingNotification[]>([]);
  const [testState, setTestState] = useState<"idle" | "sending" | "sent" | "failed">("idle");
  const [dismissState, setDismissState] = useState<"idle" | "dismissing" | "dismissed" | "failed">("idle");

  async function fetchStatus() {
    try {
      const [statusResponse, notificationsResponse] = await Promise.all([
        fetch(`${API_URL}/api/observer/daemon-status`),
        fetch(`${API_URL}/api/observer/notifications`),
      ]);
      if (statusResponse.ok) {
        const data = await statusResponse.json();
        setStatus(data);
      }
      if (notificationsResponse.ok) {
        const data: PendingNotificationsData = await notificationsResponse.json();
        setNotifications(data.notifications ?? []);
      }
    } catch {
      // ignore
    }
  }

  useEffect(() => {
    fetchStatus();
    const interval = setInterval(fetchStatus, 10_000);
    return () => clearInterval(interval);
  }, []);

  async function handleSendTestNotification() {
    if (testState === "sending") return;
    setTestState("sending");
    try {
      const response = await fetch(`${API_URL}/api/observer/notifications/test`, {
        method: "POST",
      });
      if (!response.ok) {
        setTestState("failed");
        return;
      }
      await fetchStatus();
      setTestState("sent");
    } catch {
      setTestState("failed");
    }
  }

  async function handleDismissNotification(notificationId: string) {
    if (dismissState === "dismissing") return;
    setDismissState("dismissing");
    try {
      const response = await fetch(`${API_URL}/api/observer/notifications/${notificationId}/dismiss`, {
        method: "POST",
      });
      if (!response.ok) {
        setDismissState("failed");
        return;
      }
      await fetchStatus();
      setDismissState("dismissed");
    } catch {
      setDismissState("failed");
    }
  }

  async function handleDismissAllNotifications() {
    if (dismissState === "dismissing") return;
    setDismissState("dismissing");
    try {
      const response = await fetch(`${API_URL}/api/observer/notifications/dismiss-all`, {
        method: "POST",
      });
      if (!response.ok) {
        setDismissState("failed");
        return;
      }
      await fetchStatus();
      setDismissState("dismissed");
    } catch {
      setDismissState("failed");
    }
  }

  const connected = status?.connected ?? false;
  const dotColor = connected ? "bg-green-400" : "bg-retro-text/30";
  const activeWindow = status?.active_window;
  const pendingCount = status?.pending_notification_count ?? 0;
  const captureMode = status?.capture_mode ?? "on_switch";
  const lastNativeOutcome = status?.last_native_notification_outcome;
  const lastNativeTitle = status?.last_native_notification_title;

  const displayWindow =
    activeWindow && activeWindow.length > 40
      ? activeWindow.slice(0, 37) + "..."
      : activeWindow;
  const captureLabel =
    captureMode === "on_switch"
      ? "On Switch"
      : captureMode === "balanced"
        ? "Balanced"
        : "Detailed";
  const lastNativeLabel =
    lastNativeOutcome === "queued"
      ? "Queued for desktop delivery"
      : lastNativeOutcome === "queued_test"
        ? "Queued test notification"
        : lastNativeOutcome === "acked"
          ? "Desktop notification acknowledged"
          : lastNativeOutcome === "dismissed"
            ? "Desktop notification dismissed in browser"
          : "No desktop notification sent yet";

  return (
    <div className="px-1">
      <div className="text-[10px] uppercase tracking-wider text-retro-border font-bold mb-2">
        Native Presence
      </div>
      <div className="border border-retro-text/10 rounded px-2 py-2 flex flex-col gap-2">
        <div className="flex items-center gap-1.5">
          <div className={`w-2 h-2 rounded-full flex-shrink-0 ${dotColor}`} />
          <div className="flex-1 min-w-0">
            <div className="text-[10px] text-retro-text">
              {connected ? "Desktop link live" : "Daemon offline"}
            </div>
            <div className="text-[9px] text-retro-text/40 truncate">
              {displayWindow ?? "No active window yet"}
            </div>
          </div>
          {connected && status?.has_screen_context && (
            <div className="text-[9px] text-retro-text/30">OCR</div>
          )}
        </div>

        <div className="grid grid-cols-2 gap-2 text-[9px] text-retro-text/50 uppercase tracking-wider">
          <div>
            <div className="text-retro-text/30">Capture</div>
            <div className="text-retro-text">{captureLabel}</div>
          </div>
          <div>
            <div className="text-retro-text/30">Pending</div>
            <div className="text-retro-text">{pendingCount}</div>
          </div>
        </div>

        <div className="text-[9px] text-retro-text/50">
          <div className="text-retro-text/30 uppercase tracking-wider mb-0.5">Last native event</div>
          <div className="text-retro-text">{lastNativeLabel}</div>
          {lastNativeTitle && (
            <div className="text-retro-text/40 truncate">{lastNativeTitle}</div>
          )}
        </div>

        <div className="flex items-center gap-2">
          <button
            onClick={() => void handleSendTestNotification()}
            disabled={testState === "sending"}
            className="text-[9px] uppercase tracking-wider px-2 py-1 border border-retro-text/20 rounded text-retro-text/70 hover:text-retro-highlight hover:border-retro-highlight disabled:opacity-40"
          >
            {testState === "sending" ? "Sending..." : "Send test notification"}
          </button>
          {testState !== "idle" && (
            <span className="text-[9px] text-retro-text/40">
              {testState === "sent" ? "Queued" : testState === "failed" ? "Failed" : "Sending"}
            </span>
          )}
        </div>

        <div className="text-[9px] text-retro-text/50">
          <div className="flex items-center justify-between gap-2 mb-1">
            <div className="text-retro-text/30 uppercase tracking-wider">Pending desktop notifications</div>
            {notifications.length > 1 && (
              <button
                onClick={() => void handleDismissAllNotifications()}
                disabled={dismissState === "dismissing"}
                className="text-[9px] uppercase tracking-wider text-retro-text/50 hover:text-retro-highlight disabled:opacity-40"
              >
                Dismiss all
              </button>
            )}
          </div>
          {notifications.length === 0 ? (
            <div className="text-retro-text/40">No pending desktop notifications.</div>
          ) : (
            <div className="flex flex-col gap-1">
              {notifications.map((notification) => (
                <div
                  key={notification.id}
                  className="border border-retro-text/10 rounded px-2 py-1 flex items-start justify-between gap-2"
                >
                  <div className="min-w-0">
                    <div className="text-retro-text truncate">{notification.title}</div>
                    <div className="text-retro-text/40 truncate">{notification.body}</div>
                  </div>
                  <button
                    onClick={() => void handleDismissNotification(notification.id)}
                    disabled={dismissState === "dismissing"}
                    className="text-[9px] uppercase tracking-wider text-retro-text/50 hover:text-retro-highlight disabled:opacity-40"
                  >
                    Dismiss
                  </button>
                </div>
              ))}
            </div>
          )}
          {dismissState !== "idle" && (
            <div className="mt-1 text-retro-text/40">
              {dismissState === "dismissed"
                ? "Desktop queue updated"
                : dismissState === "failed"
                  ? "Dismiss failed"
                  : "Updating"}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
