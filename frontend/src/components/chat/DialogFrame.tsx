import type { ReactNode } from "react";

interface DialogFrameProps {
  children: ReactNode;
  title?: string;
  className?: string;
}

export function DialogFrame({ children, title, className = "" }: DialogFrameProps) {
  return (
    <div className={`rpg-frame p-3 ${className}`}>
      {title && (
        <div className="absolute -top-3 left-4 bg-retro-panel px-2 text-retro-border text-[10px] uppercase tracking-wider">
          {title}
        </div>
      )}
      {children}
    </div>
  );
}
