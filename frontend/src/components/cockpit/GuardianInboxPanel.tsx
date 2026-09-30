import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  applyGuardianInboxAction,
  createGuardianInboxIdempotencyKey,
  fetchGuardianInbox,
  fetchGuardianInboxItem,
  GuardianInboxApiError,
} from "../../lib/guardianInbox";
import type {
  GuardianInboxAction,
  GuardianInboxActionRequest,
  GuardianInboxEvidenceRef,
  GuardianInboxEvidencePreview,
  GuardianInboxItem,
  GuardianInboxPage,
} from "../../types";

export interface GuardianInboxPanelProps {
  autoLoad?: boolean;
  pollIntervalMs?: number;
  onOpenTask?: (taskId: string) => void;
  onInspectArtifact?: (reference: GuardianInboxEvidenceRef, preview?: GuardianInboxEvidencePreview) => void;
}

function formatTime(value: string | null | undefined): string {
  if (!value) return "unknown";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function defaultSnoozeValue(): string {
  const value = new Date(Date.now() + 60 * 60 * 1000);
  const local = new Date(value.getTime() - value.getTimezoneOffset() * 60 * 1000);
  return local.toISOString().slice(0, 16);
}

function safeHref(value: string | null | undefined): string | null {
  if (!value) return null;
  if (value.startsWith("//")) return null;
  try {
    const parsed = new URL(value, window.location.origin);
    if (parsed.origin !== window.location.origin) return null;
    const knownPath = [
      "/api/artifacts/",
      "/api/capabilities/source-watches/",
      "/api/work-board/tasks/",
      "/cockpit",
    ].some((prefix) => parsed.pathname.startsWith(prefix));
    if (!knownPath) return null;
    return `${parsed.pathname}${parsed.search}${parsed.hash}`;
  } catch {
    return null;
  }
}

function mergePage(current: GuardianInboxItem[], page: GuardianInboxPage): GuardianInboxItem[] {
  const byId = new Map(current.map((item) => [item.id, item]));
  page.items.forEach((item) => byId.set(item.id, item));
  return Array.from(byId.values()).sort((a, b) => a.id.localeCompare(b.id));
}

function evidenceBinding(item: GuardianInboxItem): string {
  return item.evidence_refs
    .map((reference) => [
      reference.artifact_id ?? "",
      reference.file_path ?? "",
      reference.content_sha256 ?? reference.sha256 ?? "",
      reference.owner_session_id ?? "",
    ].join("\u0000"))
    .sort()
    .join("\u0001");
}

function ownerBinding(item: GuardianInboxItem): string[] {
  return [
    ...item.evidence_refs.map((reference) => reference.owner_session_id),
    ...(item.evidence_previews ?? []).map((preview) => preview.owner_session_id),
  ]
    .filter((owner): owner is string => Boolean(owner))
    .sort();
}

function detailMatchesListItem(listItem: GuardianInboxItem, detail: GuardianInboxItem): boolean {
  if (
    listItem.id !== detail.id
    || listItem.revision !== detail.revision
    || listItem.goal_id !== detail.goal_id
    || listItem.goal_revision !== detail.goal_revision
    || listItem.watch_id !== detail.watch_id
    || listItem.plan_revision !== detail.plan_revision
    || listItem.source_id !== detail.source_id
  ) {
    return false;
  }
  const listEvidence = evidenceBinding(listItem);
  if (listEvidence && evidenceBinding(detail) !== listEvidence) return false;
  const listOwners = ownerBinding(listItem);
  if (listOwners.length > 0) {
    const detailOwners = ownerBinding(detail);
    if (detailOwners.length === 0 || listOwners.some((owner, index) => detailOwners[index] !== owner)) return false;
  }
  return true;
}

function mergeCachedDetail(listItem: GuardianInboxItem, detail: GuardianInboxItem): GuardianInboxItem {
  return {
    ...listItem,
    evidence_refs: detail.evidence_refs.length > 0 ? detail.evidence_refs : listItem.evidence_refs,
    evidence_previews: detail.evidence_previews,
    job: detail.job,
    task_id: listItem.task_id ?? detail.task_id,
    task_url: listItem.task_url ?? detail.task_url,
    recovery_action: listItem.recovery_action ?? detail.recovery_action,
  };
}

function actionLabel(action: GuardianInboxAction): string {
  if (action === "accept_followup") return "Accept follow-up";
  if (action === "snooze") return "Snooze";
  return "Dismiss";
}

export function GuardianInboxPanel({
  autoLoad = true,
  pollIntervalMs = 30_000,
  onOpenTask,
  onInspectArtifact,
}: GuardianInboxPanelProps) {
  const [items, setItems] = useState<GuardianInboxItem[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [lastConfirmedAt, setLastConfirmedAt] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [actionError, setActionError] = useState<Record<string, string>>({});
  const [actionBusy, setActionBusy] = useState<string | null>(null);
  const [receipts, setReceipts] = useState<Record<string, string>>({});
  const [snoozeValues, setSnoozeValues] = useState<Record<string, string>>({});
  const [dismissReasons, setDismissReasons] = useState<Record<string, string>>({});
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [detailLoading, setDetailLoading] = useState<Record<string, boolean>>({});
  const gestureKeys = useRef(new Map<string, string>());
  const gestureRequests = useRef(new Map<string, GuardianInboxActionRequest>());
  const detailCacheRef = useRef(new Map<string, GuardianInboxItem>());
  const expandedRef = useRef<Record<string, boolean>>({});
  const itemsRef = useRef<GuardianInboxItem[]>([]);
  const mountedRef = useRef(true);
  const controllersRef = useRef(new Set<AbortController>());

  const rememberDetail = useCallback((item: GuardianInboxItem) => {
    detailCacheRef.current.set(item.id, item);
    for (const id of detailCacheRef.current.keys()) {
      if (id !== item.id && !expandedRef.current[id]) detailCacheRef.current.delete(id);
    }
    while (detailCacheRef.current.size > 8) {
      const first = detailCacheRef.current.keys().next().value as string | undefined;
      if (!first) break;
      if (expandedRef.current[first]) {
        const movable = Array.from(detailCacheRef.current.keys()).find((candidate) => !expandedRef.current[candidate]);
        if (!movable) break;
        detailCacheRef.current.delete(movable);
      } else {
        detailCacheRef.current.delete(first);
      }
    }
  }, []);

  const loadDetail = useCallback(async (item: GuardianInboxItem) => {
    setDetailLoading((current) => ({ ...current, [item.id]: true }));
    try {
      const next = await fetchGuardianInboxItem(item.id);
      if (!mountedRef.current) return;
      const current = itemsRef.current.find((candidate) => candidate.id === item.id);
      if (!current) {
        detailCacheRef.current.delete(item.id);
        return;
      }
      if (!detailMatchesListItem(current, next)) {
        detailCacheRef.current.delete(item.id);
        setActionError((state) => ({
          ...state,
          [item.id]: "Evidence changed while its detail was loading. Refresh the inbox before inspecting it again.",
        }));
        return;
      }
      rememberDetail(next);
      setItems((currentItems) => {
        const updated = currentItems.map((candidate) => (
          candidate.id === next.id ? mergeCachedDetail(candidate, next) : candidate
        ));
        itemsRef.current = updated;
        return updated;
      });
    } catch (err) {
      if (!mountedRef.current) return;
      setActionError((current) => ({
        ...current,
        [item.id]: err instanceof GuardianInboxApiError
          ? `${err.message}${err.recoveryAction ? ` · recovery: ${err.recoveryAction}` : ""}`
          : "Evidence details unavailable.",
      }));
    } finally {
      if (mountedRef.current) {
        setDetailLoading((current) => ({ ...current, [item.id]: false }));
      }
    }
  }, [rememberDetail]);

  const load = useCallback(async (cursor?: string | null, append = false, reconcile = false) => {
    const controller = new AbortController();
    controllersRef.current.add(controller);
    setLoading(true);
    try {
      const page = await fetchGuardianInbox({ limit: 50, cursor, signal: controller.signal });
      if (!mountedRef.current) return;
      const reloadDetails: GuardianInboxItem[] = [];
      const pageItems = page.items.map((item) => {
        const cached = detailCacheRef.current.get(item.id);
        if (!cached) {
          if (expandedRef.current[item.id]) reloadDetails.push(item);
          return item;
        }
        if (!detailMatchesListItem(item, cached)) {
          detailCacheRef.current.delete(item.id);
          if (expandedRef.current[item.id]) reloadDetails.push(item);
          return item;
        }
        return mergeCachedDetail(item, cached);
      });
      const next = append
        ? mergePage(itemsRef.current, { ...page, items: pageItems })
        : pageItems;
      itemsRef.current = next;
      if (reconcile) {
        page.items.forEach((item) => {
          ["accept_followup", "snooze", "dismiss"].forEach((action) => {
            gestureKeys.current.delete(`${item.id}:${action}`);
            gestureRequests.current.delete(`${item.id}:${action}`);
          });
        });
      }
      setItems(next);
      reloadDetails.forEach((item) => void loadDetail(item));
      setNextCursor(page.next_cursor ?? null);
      setLastConfirmedAt(page.last_confirmed_at ?? new Date().toISOString());
      setStatus(null);
    } catch (err) {
      if (!mountedRef.current || (err instanceof DOMException && err.name === "AbortError")) return;
      const message = err instanceof GuardianInboxApiError
        ? err.message
        : "Guardian inbox refresh failed.";
      setStatus(itemsRef.current.length > 0
        ? `Inbox refresh degraded; showing last-known items. ${message}`
        : message);
    } finally {
      controllersRef.current.delete(controller);
      if (mountedRef.current) setLoading(false);
    }
  }, [loadDetail]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      controllersRef.current.forEach((controller) => controller.abort());
      controllersRef.current.clear();
    };
  }, []);

  useEffect(() => {
    expandedRef.current = expanded;
  }, [expanded]);

  useEffect(() => {
    if (!autoLoad) return;
    void load();
    if (pollIntervalMs <= 0) return;
    const timer = window.setInterval(() => void load(), pollIntervalMs);
    return () => window.clearInterval(timer);
  }, [autoLoad, load, pollIntervalMs]);

  const orderedItems = useMemo(() => items.slice().sort((a, b) => {
    const aPending = a.state === "pending" || a.state === "snoozed";
    const bPending = b.state === "pending" || b.state === "snoozed";
    if (aPending !== bPending) return aPending ? -1 : 1;
    return a.title.localeCompare(b.title);
  }), [items]);

  const updateItem = (next: GuardianInboxItem) => {
    setItems((current) => {
      const updated = current.map((item) => item.id === next.id ? next : item);
      itemsRef.current = updated;
      return updated;
    });
  };

  const showDetails = async (item: GuardianInboxItem) => {
    if (expanded[item.id]) {
      expandedRef.current[item.id] = false;
      detailCacheRef.current.delete(item.id);
      setExpanded((current) => ({ ...current, [item.id]: false }));
      return;
    }
    expandedRef.current[item.id] = true;
    setExpanded((current) => ({ ...current, [item.id]: true }));
    const cached = detailCacheRef.current.get(item.id);
    if (cached && detailMatchesListItem(item, cached)) {
      updateItem(mergeCachedDetail(item, cached));
      return;
    }
    void loadDetail(item);
  };

  const normalizedSnoozeUntil = (value: string | undefined): string | null => {
    if (!value) return null;
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date.toISOString();
  };

  const gestureInputChanged = (
    item: GuardianInboxItem,
    action: GuardianInboxAction,
    request: GuardianInboxActionRequest,
  ): boolean => {
    if (action === "snooze") {
      const currentValue = snoozeValues[item.id] || defaultSnoozeValue();
      return normalizedSnoozeUntil(currentValue) !== normalizedSnoozeUntil(request.until);
    }
    if (action === "dismiss") {
      const currentReason = dismissReasons[item.id]?.trim().slice(0, 500) || undefined;
      return currentReason !== request.reason;
    }
    return false;
  };

  const runAction = async (item: GuardianInboxItem, action: GuardianInboxAction) => {
    const actionKey = `${item.id}:${action}`;
    const priorRequest = gestureRequests.current.get(actionKey);
    if (priorRequest && gestureInputChanged(item, action, priorRequest)) {
      setActionError((current) => ({
        ...current,
        [item.id]: "This gesture changed after the failed attempt. Refresh the inbox before sending a new request.",
      }));
      return;
    }
    const request = priorRequest ?? (() => {
      const key = createGuardianInboxIdempotencyKey(item.id, action);
      const nextRequest: GuardianInboxActionRequest = {
        action,
        expected_revision: item.revision,
        idempotency_key: key,
        ...(action === "snooze"
          ? (() => {
            const until = snoozeValues[item.id] || defaultSnoozeValue();
            if (!snoozeValues[item.id]) {
              setSnoozeValues((current) => ({ ...current, [item.id]: until }));
            }
            return { until: new Date(until).toISOString() };
          })()
          : {}),
        ...(action === "dismiss" && dismissReasons[item.id]?.trim()
          ? { reason: dismissReasons[item.id].trim().slice(0, 500) }
          : {}),
      };
      gestureKeys.current.set(actionKey, key);
      gestureRequests.current.set(actionKey, nextRequest);
      return nextRequest;
    })();
    setActionBusy(actionKey);
    setActionError((current) => ({ ...current, [item.id]: "" }));
    try {
      const result = await applyGuardianInboxAction(item.id, request);
      const terminal = result.state === "accepted" || result.state === "dismissed" || result.state === "expired";
      updateItem({
        ...item,
        revision: result.revision,
        state: result.state,
        task_id: result.task_id ?? item.task_id,
        allowed_actions: terminal ? [] : item.allowed_actions,
        recovery_action: result.recovery_action ?? item.recovery_action,
      });
      setReceipts((current) => ({ ...current, [item.id]: result.receipt_id }));
      setStatus(`${actionLabel(action)} recorded.`);
      gestureKeys.current.delete(actionKey);
      gestureRequests.current.delete(actionKey);
    } catch (err) {
      const message = err instanceof GuardianInboxApiError
        ? `${err.message}${err.code ? ` (${err.code})` : ""}${err.recoveryAction ? ` · recovery: ${err.recoveryAction}` : ""}`
        : `${actionLabel(action)} failed.`;
      setActionError((current) => ({ ...current, [item.id]: message }));
    } finally {
      setActionBusy(null);
    }
  };

  return (
    <section className="cockpit-outcome-card" data-testid="guardian-inbox-panel">
      <div className="cockpit-outcome-card-header">
        <div>
          <div className="cockpit-outcome-card-label">Guardian intervention inbox</div>
          <div className="cockpit-outcome-copy">Durable, owner-scoped source changes awaiting your decision.</div>
        </div>
        <button type="button" onClick={() => void load(undefined, false, true)} disabled={loading}>
          {loading ? "Refreshing…" : "Refresh"}
        </button>
      </div>
      <div className="cockpit-outcome-card-body">
        {status ? <div className="cockpit-outcome-note" role="status">{status}</div> : null}
        {lastConfirmedAt ? <div className="cockpit-outcome-note">last confirmed · {formatTime(lastConfirmedAt)}</div> : null}
        {orderedItems.length === 0 ? (
          <div className="cockpit-outcome-copy">No actionable guardian items are waiting.</div>
        ) : orderedItems.map((item) => {
          const supported = new Set(item.allowed_actions);
          const pendingAction = actionBusy?.startsWith(`${item.id}:`) ?? false;
          const refreshingDetail = Boolean(detailLoading[item.id]);
          return (
            <article key={item.id} className="source-watch-record" data-testid={`guardian-inbox-item-${item.id}`} data-state={item.state}>
              <div className="cockpit-outcome-primary">{item.title} · {item.state}</div>
              <div className="cockpit-outcome-copy">{item.summary}</div>
              <div className="cockpit-outcome-copy">Why now: {item.why_now}</div>
              <div className="cockpit-outcome-note">
                goal {item.goal_id} rev {item.goal_revision} · watch {item.watch_id} plan {item.plan_revision} · expires {formatTime(item.expires_at)}
              </div>
              <div className="cockpit-outcome-note">
                source {item.source_status ?? "unknown"} · freshness {item.source_freshness ?? "unknown"} · evidence {item.evidence_status ?? item.verification_status ?? "unknown"} · memory {item.memory_status ?? "unknown"}
              </div>
              {item.policy_reason ? <div className="cockpit-outcome-note">policy · {item.policy_reason}</div> : null}
              {item.recovery_action ? <div className="cockpit-outcome-note">recovery · {item.recovery_action}</div> : null}
              {item.degraded ? <div className="cockpit-outcome-note">degraded · server state is not recognized; actions are unavailable</div> : null}
              <div className="source-watch-actions">
                {supported.has("accept_followup") ? (
                  <button type="button" onClick={() => void runAction(item, "accept_followup")} disabled={pendingAction}>
                    {actionBusy === `${item.id}:accept_followup` ? "Accepting…" : "Accept follow-up"}
                  </button>
                ) : null}
                {supported.has("snooze") ? (
                  <>
                    <input
                      aria-label={`Snooze ${item.title}`}
                      type="datetime-local"
                      value={snoozeValues[item.id] ?? ""}
                      onChange={(event) => setSnoozeValues((current) => ({ ...current, [item.id]: event.target.value }))}
                      disabled={pendingAction}
                    />
                    <button type="button" onClick={() => void runAction(item, "snooze")} disabled={pendingAction}>
                      {actionBusy === `${item.id}:snooze` ? "Snoozing…" : "Snooze"}
                    </button>
                  </>
                ) : null}
                {supported.has("dismiss") ? (
                  <>
                    <input
                      aria-label={`Dismiss reason for ${item.title}`}
                      value={dismissReasons[item.id] ?? ""}
                      onChange={(event) => setDismissReasons((current) => ({ ...current, [item.id]: event.target.value }))}
                      placeholder="Dismiss reason (optional)"
                      maxLength={500}
                      disabled={pendingAction}
                    />
                    <button type="button" onClick={() => void runAction(item, "dismiss")} disabled={pendingAction}>
                      {actionBusy === `${item.id}:dismiss` ? "Dismissing…" : "Dismiss"}
                    </button>
                  </>
                ) : null}
                <button
                  type="button"
                  onClick={() => void showDetails(item)}
                  disabled={pendingAction || (refreshingDetail && !expanded[item.id])}
                >
                  {expanded[item.id] ? "Hide evidence" : "View evidence and task"}
                </button>
              </div>
              {expanded[item.id] ? (
                <div className="cockpit-outcome-note">
                  {refreshingDetail ? <div role="status">Refreshing verified evidence details…</div> : null}
                  <div>evidence refs:</div>
                  {item.evidence_refs.length > 0 ? item.evidence_refs.map((ref, index) => {
                    const href = safeHref(ref.artifact_url);
                    const label = ref.label ?? ref.artifact_id ?? ref.content_sha256 ?? ref.sha256 ?? `evidence ${index + 1}`;
                    const preview = item.evidence_previews?.find((candidate) => (
                      candidate.artifact_id === ref.artifact_id
                      && (!candidate.sha256 || candidate.sha256 === (ref.content_sha256 ?? ref.sha256))
                    ));
                    const inspectable = Boolean(
                      ref.artifact_id
                      || (ref.file_path && (ref.content_sha256 || ref.sha256)),
                    );
                    return (
                      <div key={`${item.id}:evidence:${index}`}>
                        {inspectable && onInspectArtifact ? (
                          <button
                            type="button"
                            className="underline"
                            aria-label={`Inspect guardian evidence ${label}`}
                            onClick={() => onInspectArtifact(ref, preview)}
                            disabled={refreshingDetail}
                          >
                            Inspect evidence {label}
                          </button>
                        ) : href ? (
                          <a href={href} target="_blank" rel="noreferrer">{label}</a>
                        ) : (
                          label
                        )}
                      </div>
                    );
                  }) : <div>No evidence metadata was returned.</div>}
                  {item.job ? (
                    <div className="mt-2">
                      <div>durable job {item.job.id ?? "unknown"} · {item.job.status ?? "unknown"}</div>
                      <div>
                        attempts {item.job.attempt_count ?? "unknown"}/{item.job.max_attempts ?? "unknown"}
                        {item.job.readback_status && !item.job.readbacks?.length ? ` · readback ${item.job.readback_status}` : ""}
                      </div>
                      {item.job.readbacks?.length ? (
                        <div>
                          <div>readbacks</div>
                          {item.job.readbacks.map((readback, index) => (
                            <div key={`${item.id}:readback:${readback.readback_id ?? index}`}>
                              {readback.target_path ?? `readback ${index + 1}`}
                              {readback.readback_id ? ` · ${readback.readback_id}` : ""}
                              {readback.status ? ` · ${readback.status}` : ""}
                              {readback.verified_at ? ` · verified ${formatTime(readback.verified_at)}` : ""}
                              {readback.digest ? ` · digest ${readback.digest}` : ""}
                            </div>
                          ))}
                        </div>
                      ) : (
                        <>
                          {item.job.readback_id ? <div>readback · {item.job.readback_id}</div> : null}
                          {item.job.verified_at ? <div>verified · {formatTime(item.job.verified_at)}</div> : null}
                          {item.job.digest ? <div>digest · {item.job.digest}</div> : null}
                        </>
                      )}
                    </div>
                  ) : refreshingDetail ? null : <div className="mt-2">Durable source job receipt unavailable.</div>}
                  {item.task_id ? (
                    <a
                      href={safeHref(item.task_url) ?? `/cockpit?task_id=${encodeURIComponent(item.task_id)}`}
                      onClick={(event) => {
                        if (!onOpenTask) return;
                        event.preventDefault();
                        onOpenTask(item.task_id as string);
                      }}
                    >
                      Open accepted task {item.task_id}
                    </a>
                  ) : <div>No accepted task yet.</div>}
                  {safeHref(item.watch_url) ? <a href={safeHref(item.watch_url) as string}>Open source watch</a> : null}
                  {receipts[item.id] ? <div>receipt · {receipts[item.id]}</div> : null}
                </div>
              ) : null}
              {actionError[item.id] ? <div className="cockpit-outcome-note" role="alert">{actionError[item.id]}</div> : null}
            </article>
          );
        })}
        {nextCursor ? (
          <button type="button" onClick={() => void load(nextCursor, true)} disabled={loading}>
            Load more
          </button>
        ) : null}
      </div>
    </section>
  );
}
