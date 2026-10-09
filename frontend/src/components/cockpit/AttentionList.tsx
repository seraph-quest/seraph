import { useEffect, useRef, type KeyboardEvent } from "react";
import type { AttentionItem } from "../../lib/cockpitAttention";
import "./AttentionList.css";

export interface AttentionListProps {
  items: AttentionItem[];
  confirmedAt: string | null;
  available: boolean;
  focusItemId?: string | null;
  onOpen: (item: AttentionItem) => void;
}

/** Shared keyboard traversal for current Home and Inbox attention metadata. */
export function moveAttentionFocus(event: KeyboardEvent<HTMLElement>) {
  if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key) || !(event.target instanceof HTMLButtonElement) || !event.target.hasAttribute("data-attention-id")) return;
  const buttons = [...event.currentTarget.querySelectorAll<HTMLButtonElement>("button[data-attention-id]")];
  const index = buttons.indexOf(event.target);
  if (index < 0 || buttons.length === 0) return;
  event.preventDefault();
  const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : Math.max(0, Math.min(buttons.length - 1, index + (event.key === "ArrowDown" ? 1 : -1)));
  buttons[next].focus();
}

function age(value: string | null): string {
  if (!value) return "age unavailable";
  const elapsed = Date.now() - Date.parse(value);
  if (!Number.isFinite(elapsed)) return "age unavailable";
  const minutes = Math.max(0, Math.floor(elapsed / 60_000));
  return minutes < 60 ? `${minutes}m since update` : minutes < 1440 ? `${Math.floor(minutes / 60)}h since update` : `${Math.floor(minutes / 1440)}d since update`;
}

export function AttentionList({ items, confirmedAt, available, focusItemId, onOpen }: AttentionListProps) {
  const listRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!focusItemId) return;
    const button = [...(listRef.current?.querySelectorAll<HTMLButtonElement>("[data-attention-id]") ?? [])].find((entry) => entry.dataset.attentionId === focusItemId);
    (button ?? listRef.current)?.focus();
  }, [focusItemId, items]);
  return (
    <article className="cockpit-home-card cockpit-attention" aria-label="Needs attention">
      <h3>Needs attention</h3>
      <p className="cockpit-home-muted">Snapshot · {confirmedAt ?? "confirmation time unavailable"}. Refresh Home to check current state.</p>
      <div ref={listRef} className="cockpit-attention-list" tabIndex={-1} aria-label="Attention snapshot" onKeyDown={moveAttentionFocus}>
        {items.map((item) => (
          <button key={item.id} type="button" data-attention-id={item.id} className="cockpit-attention-item" onClick={() => onOpen(item)}>
            <strong>{item.title}</strong>
            <span>{item.reason}</span>
            <span className="cockpit-home-muted">{item.readOnly ? "Recovered owner · read only" : "Current operator"} · {age(item.updatedAt)}</span>
            <span className="cockpit-home-muted">Goal {item.goalId ?? "unavailable"}{item.threadId ? ` · thread ${item.threadId}` : ""}</span>
            <span>{item.metadataConfirmed ? item.readOnly ? "Inspect history" : item.recoveryAction ? `Inspect · server recovery: ${item.recoveryAction.replace(/_/g, " ")}` : "Inspect decision" : "Inspect last confirmed metadata · refresh required before acting"}</span>
          </button>
        ))}
      </div>
      {!available ? <p>Inbox data unavailable. Attention projections may be incomplete.</p> : items.length === 0 ? <p>No pending decisions on this page.</p> : null}
    </article>
  );
}
