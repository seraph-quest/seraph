import { useEffect, useId, useRef, useState } from "react";

import type { ShellStatusAction, ShellStatusItem, ShellStatusSummary } from "../../lib/cockpitShellStatus";

const TONE_LABEL: Record<ShellStatusItem["tone"], string> = {
  ok: "Ready",
  info: "No action needed",
  unknown: "Not confirmed",
  warning: "Needs attention",
  error: "Blocked",
};

export interface CockpitStatusMenuProps {
  items: ShellStatusItem[];
  summary: ShellStatusSummary;
  /** One-line identity shown on the trigger, e.g. the chat model. */
  headline: string;
  onAction: (action: ShellStatusAction) => void;
}

/**
 * Compact status trigger plus a detail popover (#1172). The trigger always
 * shows the chat model and an overall tone; every non-ready item in the
 * popover explains why and offers the action that resolves it.
 */
export function CockpitStatusMenu({ items, summary, headline, onAction }: CockpitStatusMenuProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLElement | null>(null);
  const panelId = useId();

  const close = (restoreFocus: boolean) => {
    setOpen(false);
    if (restoreFocus) triggerRef.current?.focus();
  };

  useEffect(() => {
    if (!open) return;
    // Move focus into the dialog so keyboard users land on its content.
    panelRef.current?.focus();
    const onPointer = (event: PointerEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) setOpen(false);
    };
    // Escape belongs to this popover only while it is open and no other
    // overlay already handled the key; it does not reach global shortcuts.
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      event.preventDefault();
      event.stopPropagation();
      setOpen(false);
      triggerRef.current?.focus();
    };
    document.addEventListener("pointerdown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div className="cockpit-status-menu" ref={rootRef}>
      <button
        ref={triggerRef}
        type="button"
        className={`cockpit-status-trigger cockpit-status-trigger--${summary.tone}`}
        aria-expanded={open}
        aria-haspopup="dialog"
        aria-controls={panelId}
        aria-label={`System status: ${summary.label}. ${headline}`}
        title="Show system status details"
        data-testid="cockpit-status-trigger"
        onClick={() => setOpen((current) => !current)}
      >
        <span className={`cockpit-status-dot cockpit-status-dot--${summary.tone}`} aria-hidden="true" />
        <span className="cockpit-status-trigger-headline">{headline}</span>
        <span className="cockpit-status-trigger-summary">{summary.label}</span>
      </button>
      {open ? (
        <section
          id={panelId}
          ref={panelRef}
          tabIndex={-1}
          className="cockpit-status-panel"
          role="dialog"
          aria-label="System status"
          data-testid="cockpit-status-panel"
        >
          <header className="cockpit-status-panel-header">
            <h2>System status</h2>
            <span className={`cockpit-status-badge cockpit-status-badge--${summary.tone}`}>{summary.label}</span>
          </header>
          <ul className="cockpit-status-list">
            {items.map((item) => (
              <li key={item.id} className={`cockpit-status-item cockpit-status-item--${item.tone}`} data-testid={`cockpit-status-item-${item.id}`}>
                <div className="cockpit-status-item-row">
                  <span className={`cockpit-status-dot cockpit-status-dot--${item.tone}`} aria-hidden="true" />
                  <span className="cockpit-status-item-label">{item.label}</span>
                  <span className="cockpit-status-item-tone">{TONE_LABEL[item.tone]}</span>
                </div>
                <div className="cockpit-status-item-value">{item.value}</div>
                {item.reason ? <p className="cockpit-status-item-reason">{item.reason}</p> : null}
                <div className="cockpit-status-item-footer">
                  <span className="cockpit-status-item-source">Source: {item.source}</span>
                  {item.action ? (
                    <button
                      type="button"
                      className="cockpit-status-item-action"
                      onClick={() => {
                        const action = item.action!;
                        onAction(action);
                        // Settings opens its own panel; other actions keep the
                        // popover open to show the refreshed state.
                        if (action.kind === "settings") close(false);
                      }}
                    >
                      {item.action.label}
                    </button>
                  ) : null}
                </div>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}
