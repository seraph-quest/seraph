import { useEffect, useState, useRef } from "react";
import { useChatStore } from "../../stores/chatStore";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import { continueTask, readTaskContext, type TaskContextPacket } from "../../lib/taskContinuity";

export function SessionList() {
  const sessions = useChatStore((s) => s.sessions);
  const sessionId = useChatStore((s) => s.sessionId);
  const sessionContinuity = useChatStore((s) => s.sessionContinuity);
  const loadSessions = useChatStore((s) => s.loadSessions);
  const switchSession = useChatStore((s) => s.switchSession);
  const clearSessionContinuity = useChatStore((s) => s.clearSessionContinuity);
  const newSession = useChatStore((s) => s.newSession);
  const deleteSession = useChatStore((s) => s.deleteSession);
  const renameSession = useChatStore((s) => s.renameSession);

  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingTitle, setEditingTitle] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const [tasks, setTasks] = useState<{ task_id: string; title: string }[]>([]);
  const [nextAfter, setNextAfter] = useState<number | null>(null);
  const [loadingTasks, setLoadingTasks] = useState(false);
  const taskListAbort = useRef<AbortController | null>(null);
  const [taskId, setTaskId] = useState("");
  const [packet, setPacket] = useState<TaskContextPacket | null>(null);
  const [contextError, setContextError] = useState("");
  const [continuing, setContinuing] = useState(false);
  const [reload, setReload] = useState(0);
  const continuationRef = useRef<{ taskId: string; conversationId: string } | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    taskListAbort.current = controller;
    setLoadingTasks(true);
    apiFetch(`${API_URL}/api/work-board/tasks?limit=100`, { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error("task_list_unavailable");
        const result = await response.json();
        if (!controller.signal.aborted) {
          setTasks(result.tasks);
          setNextAfter(result.next_after ?? null);
        }
      }).catch((error) => {
        if (!controller.signal.aborted) setContextError(error instanceof Error ? error.message : "task_list_unavailable");
      }).finally(() => {
        if (!controller.signal.aborted) setLoadingTasks(false);
      });
    return () => taskListAbort.current?.abort();
  }, [reload]);

  const loadMoreTasks = async () => {
    if (nextAfter === null || loadingTasks) return;
    const controller = new AbortController();
    taskListAbort.current = controller;
    setLoadingTasks(true);
    try {
      const response = await apiFetch(`${API_URL}/api/work-board/tasks?limit=100&after=${nextAfter}`, { signal: controller.signal });
      if (!response.ok) throw new Error("task_list_unavailable");
      const result = await response.json();
      if (!controller.signal.aborted) {
        setTasks((current) => {
          const known = new Set(current.map((task) => task.task_id));
          return [...current, ...result.tasks.filter((task: { task_id: string }) => !known.has(task.task_id))];
        });
        setNextAfter(result.next_after ?? null);
      }
    } catch (error) {
      if (!controller.signal.aborted) setContextError(error instanceof Error ? error.message : "task_list_unavailable");
    } finally {
      if (!controller.signal.aborted) setLoadingTasks(false);
    }
  };

  useEffect(() => {
    const linked = sessions.find((session) => session.id === sessionId)?.continuity_task_id;
    if (linked) setTaskId(linked);
  }, [sessions, sessionId]);

  useEffect(() => {
    let cancelled = false;
    setPacket(null);
    if (!taskId) return;
    setContextError("");
    readTaskContext(taskId).then((value) => {
      if (!cancelled) setPacket(value);
    }).catch((error) => {
      if (!cancelled) setContextError(error instanceof Error ? error.message : "task_context_unavailable");
    });
    return () => { cancelled = true; };
  }, [taskId, reload]);

  const resumeTask = async () => {
    if (!packet || continuing) return;
    if (continuationRef.current?.taskId !== packet.task_id) {
      continuationRef.current = { taskId: packet.task_id, conversationId: crypto.randomUUID() };
    }
    setContinuing(true);
    setContextError("");
    try {
      const id = await continueTask(packet, continuationRef.current.conversationId);
      await loadSessions();
      await switchSession(id, "restored");
      setPacket(await readTaskContext(packet.task_id));
      continuationRef.current = null;
    } catch (error) {
      setContextError(error instanceof Error ? error.message : "task_context_unavailable");
    } finally {
      setContinuing(false);
    }
  };

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
      <label className="text-[9px] px-2">
        Continue a task
        <select aria-label="Task to continue" value={taskId} disabled={continuing}
          onChange={(event) => setTaskId(event.target.value)} className="w-full bg-retro-panel text-retro-text">
          <option value="">Choose task</option>
          {taskId && !tasks.some((task) => task.task_id === taskId) && <option value={taskId}>{packet?.task_title || "Current linked task"}</option>}
          {tasks.map((task) => <option key={task.task_id} value={task.task_id}>{task.title}</option>)}
        </select>
      </label>
      {nextAfter !== null && <button disabled={loadingTasks || continuing} onClick={loadMoreTasks} className="text-[9px] px-2 text-left text-retro-highlight">
        {loadingTasks ? "Loading tasks…" : "Load more tasks"}
      </button>}
      {packet && <div className="text-[9px] px-2" aria-label="Task continuity context">
        <p>{packet.task_title}</p>
        <p>{packet.summary}</p>
        <p>Remaining work: {packet.remaining_work?.join(" ") || "None recorded"}</p>
        <p>Current blocker: {packet.blocker_text || "None recorded"}</p>
        <p>Only authenticated operator comments appear as corrections. Worker, review and older unclassified notes remain in Work.</p>
        {packet.corrections?.filter((correction) => correction.classification === "local_only_operator_correction").map((correction) => <div key={correction.ref}>
          <p>Operator correction: {correction.body}</p>
          <p>{correction.ref} · {correction.at} · Local review required; body and integrity hash excluded from assistant context.</p>
        </div>)}
        {packet.ownership_access === "recovered_read_only" && <p>Recovered history is read-only. Current scope review is required for execution.</p>}
        <p>Verified outputs: {packet.verified_artifact_refs.length}</p>
        <p>Selected private references: {packet.private_source_refs.length}</p>
        <p>Evidence: {packet.evidence_state.replace(/_/g, " ")}</p>
        <p>Assistant context: {packet.assistant_context_state.replace(/_/g, " ")}</p>
        <p>Source egress: {packet.source_egress.filter((source) => source.model_context_allowed).length} allowed / {packet.source_egress.filter((source) => !source.model_context_allowed).length} blocked</p>
        <p>Unanswered questions: {packet.open_questions.length ? packet.open_questions.join(" ") : "None recorded"}</p>
        <p>Next permitted action: {packet.next_actions.join(" ")}</p>
        <p>Unresolved effect: {packet.unresolved_effect ? `Unknown — ${packet.unresolved_effect.replace(/_/g, " ")}` : "None recorded"}</p>
        {packet.truncated && <p>Context is bounded. Open Work for the full task history.</p>}
        <button disabled={continuing} onClick={resumeTask} className="text-retro-highlight">
          {continuing ? "Continuing…" : "Continue in new chat"}
        </button>
      </div>}
      {contextError && <div role="alert" className="text-[9px] px-2 text-retro-error">
        Task context unavailable: {contextError.replace(/_/g, " ")}. No previous action was replayed.
        <button disabled={continuing} onClick={() => setReload((value) => value + 1)}>Reload task context</button>
      </div>}
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
              onClick={() => {
                clearSessionContinuity(s.id);
                switchSession(s.id, "live");
              }}
              onDoubleClick={() => {
                setEditingId(s.id);
                setEditingTitle(s.title);
              }}
            >
              {s.title}
              {sessionContinuity[s.id] && (
                <span className="ml-1 text-retro-highlight/70">
                  · {sessionContinuity[s.id] === "new_activity" ? "new" : sessionContinuity[s.id]}
                </span>
              )}
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
