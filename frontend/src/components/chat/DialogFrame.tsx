import type { ReactNode } from "react";

interface DialogFrameProps {
  children: ReactNode;
  title?: string;
  className?: string;
  onMaximize?: () => void;
  maximized?: boolean;
  onClose?: () => void;
}

export function DialogFrame({ children, title, className = "", onMaximize, maximized, onClose }: DialogFrameProps) {
  return (
    <div className={`rpg-frame p-3 ${className}`}>
      {title && (
        <div className="absolute -top-3 left-4 bg-retro-panel px-2 text-retro-border text-[11px] uppercase tracking-wider">
          {title}
        </div>
      )}
      <div className="absolute -top-3 right-4 flex gap-1">
        {onMaximize && (
          <button
            onClick={onMaximize}
            className="bg-retro-panel px-2 text-retro-border hover:text-retro-highlight text-[11px] uppercase tracking-wider transition-colors"
            title={maximized ? "Minimize" : "Maximize"}
          >
            {maximized ? "▼" : "▲"}
          </button>
        )}
        {onClose && (
          <button
            onClick={onClose}
            className="bg-retro-panel px-2 text-retro-border hover:text-retro-highlight text-[11px] uppercase tracking-wider transition-colors"
            title="Close"
          >
            ✕
          </button>
        )}
      </div>
      {children}
    </div>
  );
}
