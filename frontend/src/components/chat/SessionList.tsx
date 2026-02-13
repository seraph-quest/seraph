import { useEffect, useState, useRef } from "react";
import { useChatStore } from "../../stores/chatStore";

export function SessionList() {
  const sessions = useChatStore((s) => s.sessions);
  const sessionId = useChatStore((s) => s.sessionId);
  const loadSessions = useChatStore((s) => s.loadSessions);
  const switchSession = useChatStore((s) => s.switchSession);
  const newSession = useChatStore((s) => s.newSession);
  const deleteSession = useChatStore((s) => s.deleteSession);
  const renameSession = useChatStore((s) => s.renameSession);

  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingTitle, setEditingTitle] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    loadSessions();
  }, [loadSessions]);

  useEffect(() => {
    if (editingId && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [editingId]);

  const commitRename = async () => {
    if (editingId && editingTitle.trim()) {
      await renameSession(editingId, editingTitle.trim());
    }
    setEditingId(null);
  };

  return (
    <div className="flex flex-col gap-1 py-1">
      <button
        onClick={() => {
          newSession();
          loadSessions();
        }}
        className="text-[9px] text-retro-highlight hover:text-retro-border text-left px-2 py-1 uppercase tracking-wider"
      >
        + New Chat
      </button>
      {sessions.map((s) => (
        <div
          key={s.id}
          className={`flex items-center gap-1 px-2 py-1 cursor-pointer text-[9px] hover:bg-retro-accent/30 rounded-sm ${
            s.id === sessionId ? "bg-retro-accent/50 text-retro-highlight" : "text-retro-text/60"
          }`}
        >
          {editingId === s.id ? (
            <input
              ref={inputRef}
              value={editingTitle}
              onChange={(e) => setEditingTitle(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") commitRename();
                if (e.key === "Escape") setEditingId(null);
              }}
              onBlur={commitRename}
              className="flex-1 bg-retro-panel border border-retro-border/40 text-[9px] text-retro-text px-1 py-0 outline-none"
            />
          ) : (
            <button
              className="flex-1 text-left truncate"
              onClick={() => switchSession(s.id)}
              onDoubleClick={() => {
                setEditingId(s.id);
                setEditingTitle(s.title);
              }}
            >
              {s.title}
            </button>
          )}
          <button
            onClick={(e) => {
              e.stopPropagation();
              deleteSession(s.id);
            }}
            className="text-retro-error/60 hover:text-retro-error text-[9px] px-1"
          >
            x
          </button>
        </div>
      ))}
    </div>
  );
}
