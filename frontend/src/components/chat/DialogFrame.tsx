import type { PointerEventHandler, ReactNode } from "react";

interface DialogFrameProps {
  children: ReactNode;
  title?: string;
  className?: string;
  onMaximize?: () => void;
  maximized?: boolean;
  onClose?: () => void;
  dragHandleProps?: { onPointerDown: PointerEventHandler };
}

export function DialogFrame({ children, title, className = "", onMaximize, maximized, onClose, dragHandleProps }: DialogFrameProps) {
  const hasButtons = onMaximize || onClose;

  return (
    <div className={`rpg-frame p-3 ${className}`}>
      <div
        className="absolute -top-3 left-0 right-0 h-6 flex items-center justify-between px-4"
        style={dragHandleProps ? { cursor: "move", userSelect: "none", zIndex: 10 } : { zIndex: 10 }}
        {...dragHandleProps}
      >
        {title ? (
          <span className="bg-retro-panel px-2 text-retro-border text-[11px] uppercase tracking-wider">
            {title}
          </span>
        ) : <span />}
        {hasButtons && (
          <div className="flex gap-1">
            {onMaximize && (
              <button
                onClick={onMaximize}
                onPointerDown={(e) => e.stopPropagation()}
                className="bg-retro-panel px-2 text-retro-border hover:text-retro-highlight text-[11px] uppercase tracking-wider transition-colors"
                title={maximized ? "Minimize" : "Maximize"}
              >
                {maximized ? "▼" : "▲"}
              </button>
            )}
            {onClose && (
              <button
                onClick={onClose}
                onPointerDown={(e) => e.stopPropagation()}
                className="bg-retro-panel px-2 text-retro-border hover:text-retro-highlight text-[11px] uppercase tracking-wider transition-colors"
                title="Close"
              >
                ✕
              </button>
            )}
          </div>
        )}
      </div>
      {children}
    </div>
  );
}
