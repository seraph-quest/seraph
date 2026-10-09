import { useEffect, useRef, useState } from "react";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import { decodeChannelTaskReview, type ChannelTaskReview as Review } from "../../lib/channelCapture";

export function ChannelTaskReview({ handle, ownerPrincipalId, ownerSessionId, goalScope, onVerified, onDiscard }: {
  handle: string | null; ownerPrincipalId: string | null; ownerSessionId: string | null;
  goalScope: string; onVerified: (receipt: Review) => void; onDiscard: () => void;
}) {
  const [notice, setNotice] = useState<string | null>(null);
  const scope = JSON.stringify([handle, ownerPrincipalId, ownerSessionId, goalScope]);
  const currentScope = useRef(scope); currentScope.current = scope;
  const callbacks = useRef({ onVerified, onDiscard }); callbacks.current = { onVerified, onDiscard };
  const opened = useRef(false);
  useEffect(() => {
    const controller = new AbortController();
    const originalScope = scope;
    const current = () => !controller.signal.aborted && currentScope.current === originalScope;
    if (opened.current) callbacks.current.onDiscard();
    opened.current = false; setNotice(null);
    if (!handle || !ownerPrincipalId || !ownerSessionId) return () => controller.abort();
    if (handle.length > 64 || !/^stc1:[A-Za-z0-9_-]+$/.test(handle)) {
      setNotice("Review link is invalid. Inspect current Work.");
      return () => controller.abort();
    }
    setNotice("Checking the original Task and current source for review.");
    void apiFetch(`${API_URL}/api/telegram/task-review?handle=${encodeURIComponent(handle)}`, { signal: controller.signal })
      .then(async response => {
        if (!current()) return;
        if (!response.ok) throw new Error("Review access changed.");
        const receipt = decodeChannelTaskReview(await response.json(), ownerSessionId);
        if (!current()) return;
        callbacks.current.onVerified(receipt); opened.current = true;
        setNotice("Current Task is open in Work for review. Approval and execution require their own controls.");
      }).catch(() => {
        if (!current()) return;
        callbacks.current.onDiscard();
        setNotice("Review access changed or expired. Inspect current Work.");
      });
    return () => controller.abort();
  }, [scope, handle, ownerPrincipalId, ownerSessionId]);
  return handle && notice ? <p role="status" className="cockpit-sublist-item">{notice}</p> : null;
}
