import { useCallback, useEffect, useRef, useState } from "react";

import {
  emptyCockpitHomeSnapshot,
  fetchCockpitHomeSnapshot,
  type CockpitHomeLoadResult,
  type CockpitHomeSnapshot,
  type HomeResourceState,
} from "../../lib/cockpitHome";

export interface CockpitHomeProps {
  onOpenSection: (section: "inbox" | "work" | "goals" | "library" | "connections") => void;
  onOpenApprovals?: () => void;
  onOpenTask?: (taskId: string) => void;
  goalSummary?: {
    title?: string | null;
    status?: string | null;
    criterion?: string | null;
  } | null;
}

function resourceLabel(state: HomeResourceState | undefined): string {
  if (!state || state === "ready") return "confirmed";
  if (state === "loading") return "loading";
  if (state === "stale") return "last confirmed";
  if (state === "degraded" || state === "forbidden" || state === "offline") return "unavailable";
  return state;
}

function runtimeLabel(runtime: Record<string, unknown> | null): string {
  if (!runtime) return "unavailable";
  const effective = runtime.effective_runtime;
  if (effective && typeof effective === "object") {
    const value = effective as Record<string, unknown>;
    return String(value.summary_label ?? value.route_label ?? value.provider_label ?? "effective route available");
  }
  return String(runtime.summary_label ?? runtime.provider ?? "effective route unavailable");
}

function taskId(task: Record<string, unknown>): string | null {
  return typeof task.id === "string" ? task.id : typeof task.task_id === "string" ? task.task_id : null;
}

export function CockpitHome({ onOpenSection, onOpenApprovals, onOpenTask, goalSummary }: CockpitHomeProps) {
  type HomeResourceKey = keyof CockpitHomeLoadResult["resources"];
  const resourceKeys: HomeResourceKey[] = ["goals", "work", "approvals", "inbox", "continuity", "runtime"];
  const [snapshot, setSnapshot] = useState<CockpitHomeSnapshot>(emptyCockpitHomeSnapshot);
  const [resources, setResources] = useState<CockpitHomeLoadResult["resources"]>({
    goals: "loading", work: "loading", approvals: "loading", inbox: "loading", continuity: "loading", runtime: "loading",
  });
  const [confirmedResources, setConfirmedResources] = useState<Record<HomeResourceKey, boolean>>({
    goals: false, work: false, approvals: false, inbox: false, continuity: false, runtime: false,
  });
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const retryDelay = useRef(30_000);
  const snapshotRef = useRef(snapshot);
  const controllerRef = useRef<AbortController | null>(null);
  const retryTimerRef = useRef<number | null>(null);
  const refreshLoopRef = useRef<(() => void) | null>(null);
  snapshotRef.current = snapshot;

  const load = useCallback(async () => {
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setLoading(true);
    try {
      const result = await fetchCockpitHomeSnapshot(controller.signal, snapshotRef.current);
      if (controller.signal.aborted || controllerRef.current !== controller) return false;
      setSnapshot(result.snapshot);
      setResources(result.resources);
      setConfirmedResources((current) => {
        const next = { ...current };
        resourceKeys.forEach((key) => {
          if (result.resources[key] === "ready") next[key] = true;
        });
        return next;
      });
      setError(result.error);
      retryDelay.current = result.error ? Math.min(retryDelay.current === 30_000 ? 60_000 : 120_000, 120_000) : 30_000;
      return result.error === null;
    } catch (cause) {
      if (cause instanceof DOMException && cause.name === "AbortError") return false;
      if (controller.signal.aborted || controllerRef.current !== controller) return false;
      setError(cause instanceof Error ? cause.message : "Home refresh failed.");
      setResources((current) => Object.fromEntries(Object.keys(current).map((key) => [key, "offline"])) as typeof current);
      retryDelay.current = Math.min(retryDelay.current === 30_000 ? 60_000 : 120_000, 120_000);
      return false;
    } finally {
      if (!controller.signal.aborted && controllerRef.current === controller) setLoading(false);
    }
  }, []);

  const refreshManually = useCallback(() => {
    retryDelay.current = 30_000;
    if (retryTimerRef.current !== null) {
      window.clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }
    if (refreshLoopRef.current) refreshLoopRef.current();
    else void load();
  }, [load]);

  useEffect(() => {
    let cancelled = false;
    const schedule = () => {
      if (cancelled) return;
      if (retryTimerRef.current !== null) window.clearTimeout(retryTimerRef.current);
      retryTimerRef.current = window.setTimeout(() => {
        retryTimerRef.current = null;
        void refresh();
      }, retryDelay.current);
    };
    const refresh = async () => {
      if (cancelled) return;
      await load();
      if (!cancelled) schedule();
    };
    refreshLoopRef.current = () => void refresh();
    void refresh();
    return () => {
      cancelled = true;
      refreshLoopRef.current = null;
      controllerRef.current?.abort();
      if (retryTimerRef.current !== null) {
        window.clearTimeout(retryTimerRef.current);
        retryTimerRef.current = null;
      }
    };
  }, [load]);

  const resourceConfirmed = (key: HomeResourceKey): boolean => confirmedResources[key];
  const resourceSuffix = (key: HomeResourceKey): string => {
    if (resources[key] === "ready") return "";
    return resourceConfirmed(key) ? " · last confirmed" : " · unavailable";
  };
  const metricValue = (key: HomeResourceKey, value: number): number | string => (
    resourceConfirmed(key) ? value : "—"
  );
  const attentionItems = resourceConfirmed("inbox")
    ? snapshot.inbox?.items.filter((item) => ["pending", "snoozed"].includes(item.state)) ?? []
    : [];
  const inboxCount = attentionItems.length;
  const approvalsCount = snapshot.approvals.length;
  const runningCount = snapshot.work?.tasks.filter((task) => String(task.status) === "running").length ?? 0;
  const queuedCount = snapshot.work?.tasks.filter((task) => ["triage", "todo", "ready"].includes(String(task.status))).length ?? 0;
  const runtimeStatus = snapshot.runtime && typeof snapshot.runtime.status === "string" ? snapshot.runtime.status : resourceLabel(resources.runtime);
  const hasPriorConfirmedHome = Boolean(snapshot.last_confirmed_at);

  return (
    <section className="cockpit-section-surface cockpit-home" data-testid="cockpit-home" aria-busy={loading}>
      <div className="cockpit-section-header">
        <div>
          <div className="cockpit-eyebrow">HOME</div>
          <h2>Operator cockpit</h2>
          <p>One confirmed view of goals, work, decisions, and runtime recovery.</p>
        </div>
        <button type="button" onClick={refreshManually} disabled={loading}>
          {loading ? "Refreshing…" : "Refresh Home"}
        </button>
      </div>
      {error ? <div className="cockpit-section-notice" role="status">{hasPriorConfirmedHome ? "Showing last confirmed Home data" : "Some Home sections are unavailable"} · {error}</div> : null}
      <div className="cockpit-home-metrics">
        <button type="button" onClick={() => onOpenSection("goals")} data-testid="cockpit-home-goals-count">
          <strong>{snapshot.goals ? metricValue("goals", snapshot.goals.active_count) : "—"}</strong><span>active goals{resourceSuffix("goals")}</span>
        </button>
        <button type="button" onClick={() => onOpenSection("work")}>
          <strong>{resourceConfirmed("work") ? `${runningCount} / ${queuedCount}` : "—"}</strong><span>running / queued work on this page{resourceSuffix("work")}</span>
        </button>
        <button type="button" onClick={() => onOpenSection("inbox")}>
          <strong>{metricValue("inbox", inboxCount)}</strong><span>pending inbox items on this page{resourceSuffix("inbox")}</span>
        </button>
        <button type="button" onClick={() => {
          if (onOpenApprovals) onOpenApprovals();
          else onOpenSection("connections");
        }}>
          <strong>{metricValue("approvals", approvalsCount)}</strong><span>pending approvals on this page{resourceSuffix("approvals")}</span>
        </button>
      </div>
      <div className="cockpit-home-grid">
        <article className="cockpit-home-card">
          <h3>Ongoing goals</h3>
          <p>{snapshot.goals && resourceConfirmed("goals") ? `${snapshot.goals.active_count} active · ${snapshot.goals.total_count} total${resourceSuffix("goals")}` : "Goal summary unavailable."}</p>
          {goalSummary?.title ? <p><strong>{goalSummary.title}</strong>{goalSummary.status ? ` · ${goalSummary.status}` : ""}</p> : null}
          <p className="cockpit-home-muted">criterion · {goalSummary?.criterion ?? "not configured or unavailable"}</p>
          <p className="cockpit-home-muted">next eligible · reported in the existing Goals surface</p>
          <button type="button" onClick={() => onOpenSection("goals")}>Open Goals</button>
        </article>
        <article className="cockpit-home-card">
          <h3>Needs attention</h3>
          {attentionItems.slice(0, 3).map((item) => (
            <button key={item.id} type="button" className="cockpit-home-link" onClick={() => onOpenSection("inbox")}>
              {item.title} · {item.state}
            </button>
          ))}
          {!resourceConfirmed("inbox") ? <p>Inbox data unavailable.</p> : inboxCount === 0 ? <p>No pending decisions on this page.</p> : null}
          <button type="button" onClick={() => onOpenSection("inbox")}>Open Inbox</button>
        </article>
        <article className="cockpit-home-card">
          <h3>Runtime and recovery</h3>
          <p><strong>{runtimeLabel(snapshot.runtime)}</strong></p>
          <p>status · {runtimeStatus} · {resourceLabel(resources.runtime)}</p>
          <p className="cockpit-home-muted">Remote queue, spend, and budget fields remain unavailable when the runtime does not report them.</p>
          <button type="button" onClick={() => onOpenSection("connections")}>Open Connections</button>
        </article>
      </div>
      <div className="cockpit-home-work">
        <div className="cockpit-section-subheader"><h3>Latest confirmed work</h3><button type="button" onClick={() => onOpenSection("work")}>Open Work</button></div>
        {snapshot.work?.tasks.slice(0, 5).map((task) => {
          const id = taskId(task);
          return (
            <div key={id ?? JSON.stringify(task)} className="cockpit-home-task">
              <span>{String(task.title ?? task.label ?? id ?? "Untitled task")}</span>
              <span>{String(task.status ?? "unknown")}</span>
              {id && onOpenTask ? <button type="button" onClick={() => onOpenTask(id)}>Focus task</button> : null}
            </div>
          );
        })}
        {!resourceConfirmed("work") ? <p>Work data unavailable.</p> : !snapshot.work?.tasks.length ? <p>No work is confirmed on this page.</p> : null}
      </div>
      <div className="cockpit-home-footer">last confirmed · {snapshot.last_confirmed_at ?? "unavailable"}</div>
    </section>
  );
}
