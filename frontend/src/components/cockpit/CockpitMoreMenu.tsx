import { useEffect, useId, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";

export interface CockpitMenuItem {
  id: string;
  label: string;
  /** Short explanation shown under the label. */
  description?: string;
  /** Keyboard shortcut hint, replacing the old permanent hint chips. */
  shortcut?: string;
  group: string;
  onSelect: () => void;
}

export interface CockpitMoreMenuProps {
  items: CockpitMenuItem[];
}

/**
 * Overflow menu for rarely used shell controls (#1172): thread reset,
 * onboarding, workspace windows/layouts, extension studio and the priorities
 * overlay. Keyboard: Enter/Space opens, arrows move, Escape closes.
 */
export function CockpitMoreMenu({ items }: CockpitMoreMenuProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const menuId = useId();

  useEffect(() => {
    if (!open) return;
    const onPointer = (event: PointerEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", onPointer);
    menuRef.current?.querySelector<HTMLButtonElement>("[role=menuitem]")?.focus();
    return () => document.removeEventListener("pointerdown", onPointer);
  }, [open]);

  const onMenuKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    const entries = Array.from(menuRef.current?.querySelectorAll<HTMLButtonElement>("[role=menuitem]") ?? []);
    const index = entries.indexOf(document.activeElement as HTMLButtonElement);
    if (event.key === "Escape") {
      event.preventDefault();
      setOpen(false);
      triggerRef.current?.focus();
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      entries[(index + 1) % entries.length]?.focus();
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      entries[(index - 1 + entries.length) % entries.length]?.focus();
    } else if (event.key === "Home") {
      event.preventDefault();
      entries[0]?.focus();
    } else if (event.key === "End") {
      event.preventDefault();
      entries[entries.length - 1]?.focus();
    }
  };

  const groups = items.reduce<Array<[string, CockpitMenuItem[]]>>((acc, item) => {
    const existing = acc.find(([group]) => group === item.group);
    if (existing) existing[1].push(item);
    else acc.push([item.group, [item]]);
    return acc;
  }, []);

  return (
    <div className="cockpit-more-menu" ref={rootRef}>
      <button
        ref={triggerRef}
        type="button"
        className="cockpit-shell-button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={menuId}
        aria-label="More actions"
        title="More actions"
        onClick={() => setOpen((current) => !current)}
      >
        <span aria-hidden="true">⋯</span>
        <span className="cockpit-shell-button-label">More</span>
      </button>
      {open ? (
        <div id={menuId} ref={menuRef} role="menu" aria-label="More actions" className="cockpit-more-menu-panel" onKeyDown={onMenuKeyDown}>
          {groups.map(([group, entries]) => (
            <div key={group} role="group" aria-label={group} className="cockpit-more-menu-group">
              <div className="cockpit-more-menu-group-title" aria-hidden="true">{group}</div>
              {entries.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  role="menuitem"
                  className="cockpit-more-menu-item"
                  aria-label={item.label}
                  onClick={() => {
                    setOpen(false);
                    item.onSelect();
                  }}
                >
                  <span className="cockpit-more-menu-item-main">
                    <span className="cockpit-more-menu-item-label">{item.label}</span>
                    {item.description ? <span className="cockpit-more-menu-item-description">{item.description}</span> : null}
                  </span>
                  {item.shortcut ? <kbd className="cockpit-kbd">{item.shortcut}</kbd> : null}
                </button>
              ))}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
