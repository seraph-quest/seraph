import { useEffect, useRef, useState } from "react";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import type { WorkBoardTask } from "../../types";

const recovery = "Notice delivery is uncertain. Inspect Telegram outbox recovery in Settings, or send a fresh notice to retire its old controls.";

export function TelegramTaskNotice({ task, ownerSessionId }: {
  task: WorkBoardTask; ownerSessionId?: string | null;
}) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const pending = useRef<{ key: string; outboxId?: string } | null>(null);
  const scope = `${ownerSessionId}:${task.owner_principal_id}:${task.task_id}:${task.task_revision}`;
  const current = useRef(scope);
  current.current = scope;
  useEffect(() => { pending.current = null; setBusy(false); setMessage(null); }, [scope]);
  const recovered = !ownerSessionId || task.owner_session_id !== ownerSessionId;

  async function send(fresh = false) {
    if (busy || recovered) return;
    const captured = scope;
    if (fresh || !pending.current) pending.current = { key: crypto.randomUUID() };
    const request = pending.current;
    setBusy(true); setMessage(null);
    try {
      if (!request.outboxId) {
        const queued = await apiFetch(`${API_URL}/api/telegram/tasks/${encodeURIComponent(task.task_id)}/notice`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ expected_revision: task.task_revision, idempotency_key: request.key }),
        });
        if (!queued.ok) throw new Error("Pair Telegram and grant transit consent in Settings, then refresh the current task.");
        const value = await queued.json();
        if (captured !== current.current) return;
        if (typeof value.id !== "string" || value.id.length > 128) throw new Error(recovery);
        request.outboxId = value.id;
      }
      const outboxId = request.outboxId;
      if (typeof outboxId !== "string") throw new Error(recovery);
      const delivery = await apiFetch(`${API_URL}/api/telegram/outbox/${encodeURIComponent(outboxId)}/deliver`, { method: "POST" });
      if (!delivery.ok) throw new Error(recovery);
      const value = await delivery.json();
      if (captured !== current.current) return;
      if (value.status === "delivered") {
        setMessage("Neutral notice accepted by the configured transport. Review sends status metadata only; sensitive decisions continue in the cockpit.");
        pending.current = null;
      } else if (value.status === "unknown" || value.status === "sending") {
        setMessage(recovery);
      } else if (value.status === "queued") {
        setMessage("Notice queued. Retry delivery uses the same outbox record.");
      } else {
        setMessage("Notice blocked or expired. Refresh the task and send a fresh notice.");
      }
    } catch (error) {
      if (captured === current.current) setMessage(error instanceof Error ? error.message : recovery);
    } finally { if (captured === current.current) setBusy(false); }
  }

  return <section aria-label="Paired Telegram task notice" className="cockpit-card mt-3">
    <p>Telegram notices include no task title, body, sources, or private approval details.</p>
    <button type="button" className="cockpit-feedback-button" disabled={busy || recovered}
      onClick={() => void send()}>{busy ? "Sending notice…" : pending.current ? "Retry same notice" : "Send neutral Telegram notice"}</button>
    {message && <p role="status">{message}</p>}
    {pending.current && !busy && <button type="button" className="cockpit-feedback-button"
      disabled={recovered} onClick={() => void send(true)}>Send fresh notice and retire old controls</button>}
  </section>;
}
