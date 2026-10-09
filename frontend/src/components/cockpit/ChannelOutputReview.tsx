import { useEffect, useRef, useState } from "react";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import { decodeChannelOutputReview, type ChannelOutputReview as OutputReceipt } from "../../lib/channelCapture";

export function ChannelOutputReview({ handle, ownerPrincipalId, ownerSessionId, goalScope, onVerified, onDiscard }: {
  handle: string | null;
  ownerPrincipalId: string | null;
  ownerSessionId: string | null;
  goalScope: string;
  onVerified: (receipt: OutputReceipt) => void;
  onDiscard: () => void;
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
    opened.current = false;
    setNotice(null);
    if (!handle || !ownerPrincipalId || !ownerSessionId) return () => controller.abort();
    if (handle.length > 4096 || !/^sco1\.[A-Za-z0-9_-]+\.[a-f0-9]{64}$/.test(handle)) {
      setNotice("Output link is invalid. Inspect current Work.");
      return () => controller.abort();
    }
    setNotice("Checking current source and exact physical output.");
    void apiFetch(`${API_URL}/api/telegram/output-review?handle=${encodeURIComponent(handle)}`, { signal: controller.signal })
      .then(async response => {
        if (!current()) return;
        if (!response.ok) throw new Error("Output access changed or expired. Inspect current Work.");
        const receipt = decodeChannelOutputReview(await response.json(), ownerSessionId);
        if (!current()) return;
        callbacks.current.onVerified(receipt);
        opened.current = true;
        setNotice("Verified output is open for private review. No action or learning was authorized.");
      }).catch(() => {
        if (!current()) return;
        callbacks.current.onDiscard();
        setNotice("Output access changed or expired. Inspect current Work.");
      });
    return () => controller.abort();
  }, [scope, handle, ownerPrincipalId, ownerSessionId]);
  if (!handle || !notice) return null;
  return <p role="status" className="cockpit-sublist-item">{notice}</p>;
}
