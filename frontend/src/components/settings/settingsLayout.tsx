import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { settingsAnchorId } from "../../lib/settingsNavigation";

/**
 * Shared Settings layout pieces (#1280 PR 3): a one-line status row and a
 * closed fold. Every panel that adopts the new pattern draws from these.
 */

export type SettingsTone = "good" | "warn" | "bad" | "off";

const DOT: Record<SettingsTone, string> = {
  good: "bg-green-400",
  warn: "bg-yellow-400",
  bad: "bg-red-400",
  off: "bg-retro-text/30",
};

export const settingsActionClass =
  "border border-retro-highlight px-3 py-1 text-[10px] uppercase tracking-wider text-retro-highlight disabled:border-retro-text/20 disabled:text-retro-text/30";
export const settingsLinkClass = "text-[10px] underline text-retro-text/70 hover:text-retro-text disabled:opacity-40";

/** One row: a name, a plain status, at most one action, and optional content below. */
export function SettingsRow({
  label, status, detail, tone, action, children,
}: {
  label: string;
  status: string;
  detail?: ReactNode;
  tone: SettingsTone;
  action?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <section aria-label={label} className="flex flex-col gap-2 border border-retro-text/15 px-3 py-2">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="flex min-w-0 items-start gap-2">
          <span aria-hidden="true" className={`mt-1 h-2 w-2 flex-shrink-0 rounded-full ${DOT[tone]}`} />
          <div className="flex min-w-0 flex-col gap-0.5">
            <div className="text-[10px] uppercase tracking-wider text-retro-text/60">{label}</div>
            <div role="status" className="text-[11px] text-retro-text">{status}</div>
            {detail && (typeof detail === "string" ? <p className="text-[10px] text-retro-text/70">{detail}</p> : detail)}
          </div>
        </div>
        {action && <div className="flex-shrink-0">{action}</div>}
      </div>
      {children}
    </section>
  );
}

/**
 * A closed fold on the Diagnostics tab. Its content mounts on first open, so
 * nothing inside fetches until the operator (or a deep link) opens it. A deep
 * link whose target is this fold's anchor opens it.
 */
export function SettingsFold({
  anchor, title, openAnchor, children,
}: {
  anchor: string;
  title: string;
  /** DOM id a deep link is bringing into view, if any. */
  openAnchor?: string | null;
  children: ReactNode;
}) {
  const elementId = settingsAnchorId(anchor);
  const [open, setOpen] = useState(() => openAnchor === elementId);
  const [opened, setOpened] = useState(open);
  useEffect(() => {
    if (openAnchor === elementId) { setOpen(true); setOpened(true); }
  }, [openAnchor, elementId]);
  return (
    <details id={elementId} data-settings-anchor={anchor} open={open} className="border border-retro-text/10 px-2 py-1">
      <summary
        className="cursor-pointer text-[10px] uppercase tracking-wider text-retro-text/70"
        onClick={(event) => {
          event.preventDefault();
          setOpen((current) => !current);
          setOpened(true);
        }}
      >
        {title}
      </summary>
      {opened && <div className="mt-2 flex flex-col gap-2">{children}</div>}
    </details>
  );
}
