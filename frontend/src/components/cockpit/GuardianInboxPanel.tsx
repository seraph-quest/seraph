import { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from "react";

import {
  applyGuardianInboxAction,
  createGuardianInboxIdempotencyKey,
  fetchGuardianInbox,
  fetchGuardianInboxItem,
  GuardianInboxApiError,
  cancelGuardianOpportunity,
  createGuardianUuid,
  generateGuardianOpportunityPlan, opportunityPlanStorageKey, readOpportunityPlanRequest, retainOpportunityPlanRequest,
} from "../../lib/guardianInbox";
import type {
  GuardianInboxAction,
  GuardianInboxActionRequest,
  GuardianInboxEvidenceRef,
  GuardianInboxEvidencePreview,
  GuardianInboxItem,
  GuardianInboxPage,
  OpportunityPlanRequest,
} from "../../types";

export interface GuardianInboxPanelProps {
  currentOwnerPrincipalId?: string | null;
  currentRootId?: string | null;
  autoLoad?: boolean;
  active?: boolean;
  pageSize?: number;
  pollIntervalMs?: number;
  autoFocusAcceptedTask?: boolean;
  onOpenTask?: (taskId: string, origin?: GuardianInboxItem) => void;
  focusItemId?: string | null;
  onOpenGoals?: () => void;
  onOpenWork?: () => void;
  onSelectItem?: (item: GuardianInboxItem) => void;
  selectedItemId?: string | null;
  onRefreshSelectedItem?: (itemId: string, item: GuardianInboxItem | null) => void;
  onInspectArtifact?: (reference: GuardianInboxEvidenceRef, preview?: GuardianInboxEvidencePreview) => void;
}

/**
 * Composition seam for the M2 inspector. The panel remains the sole owner of
 * action payloads, idempotency keys, retries, and revision checks; the
 * inspector can request an already-rendered row's server-advertised action
 * without implementing a second action client.
 */
export interface GuardianInboxPanelHandle {
  runAction: (itemId: string, action: GuardianInboxAction) => void;
}

function formatTime(value: string | null | undefined): string {
  if (!value) return "unknown";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
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
    || listItem.opportunity_id !== detail.opportunity_id
    || listItem.opportunity_revision !== detail.opportunity_revision
    || listItem.opportunity_status !== detail.opportunity_status
    || listItem.state !== detail.state
    || listItem.degraded !== detail.degraded
    || listItem.evidence_status !== detail.evidence_status
    || listItem.source_status !== detail.source_status
    || listItem.source_freshness !== detail.source_freshness
    || listItem.verification_status !== detail.verification_status
    || listItem.reason_code !== detail.reason_code
    || listItem.policy_reason !== detail.policy_reason
    || listItem.recovery_action !== detail.recovery_action
    || JSON.stringify(listItem.allowed_actions) !== JSON.stringify(detail.allowed_actions)
  ) {
    return false;
  }
  // The list projection intentionally omits private Mail selection metadata;
  // detail may enrich it. If a list ever advertises an origin, require the
  // detail projection to preserve that exact opaque binding and revision.
  if (listItem.mail && (!detail.mail || detail.mail.private !== true
    || listItem.mail.message_binding_id !== detail.mail.message_binding_id
    || listItem.mail.message_revision !== detail.mail.message_revision)) return false;
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
    mail: detail.mail ?? listItem.mail,
    task_id: listItem.task_id ?? detail.task_id,
    task_url: listItem.task_url ?? detail.task_url,
    recovery_action: listItem.recovery_action ?? detail.recovery_action,
    action_history: detail.action_history ?? listItem.action_history,
    action_history_truncated: detail.action_history_truncated ?? listItem.action_history_truncated,
    assessment: detail.assessment ?? listItem.assessment,
    reason_code: listItem.reason_code ?? detail.reason_code,
    cancel_allowed: listItem.cancel_allowed === true && detail.cancel_allowed === true,
    cancel_requested: listItem.cancel_requested === true || detail.cancel_requested === true,
    quiescent: listItem.quiescent === true || detail.quiescent === true,
    allowed_actions: listItem.allowed_actions.filter((action) => detail.allowed_actions.includes(action)),
  };
}

function actionLabel(action: GuardianInboxAction): string {
  if (action === "accept_followup") return "Accept follow-up";
  if (action === "snooze") return "Snooze";
  return "Dismiss";
}

function snoozeReason(item: GuardianInboxItem): string {
  const history = item.action_history?.find((entry) => entry.action === "snooze");
  if (history?.reason_state === "provided" && history.safe_reason) return history.safe_reason;
  if (history?.reason_state === "not_provided") return "reason not provided";
  return "reason unavailable";
}

function stateLabel(item: GuardianInboxItem): string {
  if (item.source_kind === "guardian_opportunity" && item.opportunity_status) {
    if (item.cancel_requested && !item.quiescent) return `${item.opportunity_status} · cancellation requested; waiting for native quiescence`;
    if (item.state === "pending") return item.opportunity_status;
  }
  return item.state === "snoozed"
    ? `Snoozed until ${formatTime(item.snoozed_until)} · ${snoozeReason(item)}`
    : item.state;
}

const GESTURE_STORAGE_PREFIX = "seraph.guardian.gesture.v1:";
const MAX_PERSISTED_GESTURES = 32;

interface PersistedGesture {
  version: 1;
  item_id: string;
  action: GuardianInboxAction;
  expected_revision: number;
  idempotency_key: string;
  until?: string;
  reason_digest?: string;
  created_at: number;
}

function gestureStorage(): Storage | null {
  if (typeof window === "undefined") return null;
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

function gestureStorageKey(itemId: string, action: GuardianInboxAction): string {
  return `${GESTURE_STORAGE_PREFIX}${encodeURIComponent(itemId)}:${action}`;
}

function normalizedReason(value: string | undefined): string {
  return value?.trim().slice(0, 500) ?? "";
}

function sha256Fallback(value: string): string {
  const bytes = new TextEncoder().encode(value);
  const paddedLength = Math.ceil((bytes.length + 9) / 64) * 64;
  const padded = new Uint8Array(paddedLength);
  padded.set(bytes);
  padded[bytes.length] = 0x80;
  const bitLength = bytes.length * 8;
  const lengthOffset = padded.length - 8;
  for (let index = 0; index < 8; index += 1) {
    padded[lengthOffset + index] = Math.floor(bitLength / 2 ** (56 - index)) & 0xff;
  }
  const constants = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ];
  const hash = [
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
  ];
  const rotate = (word: number, bits: number): number => (word >>> bits) | (word << (32 - bits));
  for (let offset = 0; offset < padded.length; offset += 64) {
    const words = new Uint32Array(64);
    for (let index = 0; index < 16; index += 1) {
      const position = offset + index * 4;
      words[index] = ((padded[position] << 24) | (padded[position + 1] << 16) | (padded[position + 2] << 8) | padded[position + 3]) >>> 0;
    }
    for (let index = 16; index < 64; index += 1) {
      const word = words[index - 15];
      const smallSigma0 = (rotate(word, 7) ^ rotate(word, 18) ^ (word >>> 3)) >>> 0;
      const previous = words[index - 2];
      const smallSigma1 = (rotate(previous, 17) ^ rotate(previous, 19) ^ (previous >>> 10)) >>> 0;
      words[index] = (words[index - 16] + smallSigma0 + words[index - 7] + smallSigma1) >>> 0;
    }
    let [a, b, c, d, e, f, g, h] = hash;
    for (let index = 0; index < 64; index += 1) {
      const bigSigma1 = (rotate(e, 6) ^ rotate(e, 11) ^ rotate(e, 25)) >>> 0;
      const choice = ((e & f) ^ (~e & g)) >>> 0;
      const first = (h + bigSigma1 + choice + constants[index] + words[index]) >>> 0;
      const bigSigma0 = (rotate(a, 2) ^ rotate(a, 13) ^ rotate(a, 22)) >>> 0;
      const majority = ((a & b) ^ (a & c) ^ (b & c)) >>> 0;
      const second = (bigSigma0 + majority) >>> 0;
      h = g;
      g = f;
      f = e;
      e = (d + first) >>> 0;
      d = c;
      c = b;
      b = a;
      a = (first + second) >>> 0;
    }
    hash[0] = (hash[0] + a) >>> 0;
    hash[1] = (hash[1] + b) >>> 0;
    hash[2] = (hash[2] + c) >>> 0;
    hash[3] = (hash[3] + d) >>> 0;
    hash[4] = (hash[4] + e) >>> 0;
    hash[5] = (hash[5] + f) >>> 0;
    hash[6] = (hash[6] + g) >>> 0;
    hash[7] = (hash[7] + h) >>> 0;
  }
  return hash.map((word) => word.toString(16).padStart(8, "0")).join("");
}

async function reasonDigest(value: string): Promise<string> {
  const normalized = normalizedReason(value);
  try {
    if (globalThis.crypto?.subtle) {
      const bytes = new TextEncoder().encode(normalized);
      const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
      return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
    }
  } catch {
    // Fall back to the local implementation in non-secure or restricted contexts.
  }
  return sha256Fallback(normalized);
}

function readPersistedGesture(itemId: string, action: GuardianInboxAction): PersistedGesture | null {
  const storage = gestureStorage();
  if (!storage) return null;
  try {
    const raw = storage.getItem(gestureStorageKey(itemId, action));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<PersistedGesture>;
    if (
      parsed.version !== 1
      || parsed.item_id !== itemId
      || parsed.action !== action
      || typeof parsed.expected_revision !== "number"
      || !Number.isInteger(parsed.expected_revision)
      || typeof parsed.idempotency_key !== "string"
      || typeof parsed.created_at !== "number"
    ) {
      storage.removeItem(gestureStorageKey(itemId, action));
      return null;
    }
    return parsed as PersistedGesture;
  } catch {
    return null;
  }
}

function persistGesture(record: PersistedGesture): void {
  const storage = gestureStorage();
  if (!storage) return;
  try {
    storage.setItem(gestureStorageKey(record.item_id, record.action), JSON.stringify(record));
    const keys = Array.from({ length: storage.length }, (_, index) => storage.key(index))
      .filter((key): key is string => Boolean(key && key.startsWith(GESTURE_STORAGE_PREFIX)));
    if (keys.length <= MAX_PERSISTED_GESTURES) return;
    const records = keys.flatMap((key) => {
      try {
        const value = JSON.parse(storage.getItem(key) ?? "null") as Partial<PersistedGesture>;
        return typeof value.created_at === "number" ? [{ key, created_at: value.created_at }] : [];
      } catch {
        return [];
      }
    }).sort((left, right) => left.created_at - right.created_at);
    records.slice(0, Math.max(0, records.length - MAX_PERSISTED_GESTURES)).forEach(({ key }) => storage.removeItem(key));
  } catch {
    // The in-memory request remains authoritative when session storage is unavailable.
  }
}

function clearPersistedGesture(itemId: string, action: GuardianInboxAction): void {
  const storage = gestureStorage();
  try {
    storage?.removeItem(gestureStorageKey(itemId, action));
  } catch {
    // Storage may be unavailable in a private or restricted browsing context.
  }
}

export const GuardianInboxPanel = forwardRef<GuardianInboxPanelHandle, GuardianInboxPanelProps>(function GuardianInboxPanel({
  currentOwnerPrincipalId = null,
  currentRootId = null,
  autoLoad = true,
  active = true,
  pageSize = 50,
  pollIntervalMs = 30_000,
  autoFocusAcceptedTask = false,
  onOpenTask,
  focusItemId,
  onOpenGoals,
  onOpenWork,
  onSelectItem,
  selectedItemId,
  onRefreshSelectedItem,
  onInspectArtifact,
}: GuardianInboxPanelProps, ref) {
  const [items, setItems] = useState<GuardianInboxItem[]>([]);
  const planOwner = currentOwnerPrincipalId && currentRootId ? `${currentOwnerPrincipalId}:${currentRootId}` : null;
  const [confirmedPlanOwner, setConfirmedPlanOwner] = useState<string | null>(null);
  const planScope = useRef(planOwner);
  const planController = useRef<AbortController | null>(null);
  if (planScope.current !== planOwner) { planController.current?.abort(); planScope.current = planOwner; }
  const planLock = useRef(false);
  const [planBusy, setPlanBusy] = useState<string | null>(null);
  const [planError, setPlanError] = useState<string | null>(null);
  const [planRequests, setPlanRequests] = useState<Record<string, OpportunityPlanRequest | null>>({});
  const [planStorageErrors, setPlanStorageErrors] = useState<Record<string, boolean>>({});
  useEffect(() => {
    setPlanBusy(null); planLock.current = false; setPlanError(null); setConfirmedPlanOwner(null);
  }, [currentOwnerPrincipalId, currentRootId]);
  useEffect(() => {
    const requests: Record<string, OpportunityPlanRequest | null> = {};
    const errors: Record<string, boolean> = {};
    if (currentOwnerPrincipalId && currentRootId) for (const item of items) {
      if (!item.opportunity_id) continue;
      try { requests[item.id] = readOpportunityPlanRequest(opportunityPlanStorageKey(currentOwnerPrincipalId, currentRootId, item.opportunity_id)); }
      catch { errors[item.id] = true; }
    }
    setPlanRequests(requests); setPlanStorageErrors(errors);
  }, [currentOwnerPrincipalId, currentRootId, items]);
  useEffect(() => () => { planController.current?.abort(); }, []);
  useEffect(() => {
    if (!focusItemId) return;
    const row = [...document.querySelectorAll<HTMLElement>("[data-testid^=\"guardian-inbox-row-\"]")].find((entry) => entry.dataset.testid === `guardian-inbox-row-${focusItemId}`);
    row?.focus();
  }, [focusItemId, items]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [lastConfirmedAt, setLastConfirmedAt] = useState<string | null>(null);
  const [loading, setLoading] = useState(autoLoad && active);
  const [status, setStatus] = useState<string | null>(null);
  const [actionError, setActionError] = useState<Record<string, string>>({});
  const [unknownActions, setUnknownActions] = useState<Record<string, boolean>>({});
  const [actionBusy, setActionBusy] = useState<string | null>(null);
  const [receipts, setReceipts] = useState<Record<string, string>>({});
  const [snoozeValues, setSnoozeValues] = useState<Record<string, string>>({});
  const [snoozeReasons, setSnoozeReasons] = useState<Record<string, string>>({});
  const [dismissReasons, setDismissReasons] = useState<Record<string, string>>({});
  const [filterText, setFilterText] = useState("");
  const [filterGoal, setFilterGoal] = useState("");
  const [filterState, setFilterState] = useState("");
  const [filterSource, setFilterSource] = useState("");
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [detailLoading, setDetailLoading] = useState<Record<string, boolean>>({});
  const [listConfirmed, setListConfirmed] = useState(false);
  const gestureKeys = useRef(new Map<string, string>());
  const gestureRequests = useRef(new Map<string, GuardianInboxActionRequest>());
  const cancellationRequests = useRef(new Map<string, { revision: number; key: string }>());
  const detailCacheRef = useRef(new Map<string, GuardianInboxItem>());
  const detailConfirmedRef = useRef(new Set<string>());
  const expandedRef = useRef<Record<string, boolean>>({});
  const itemsRef = useRef<GuardianInboxItem[]>([]);
  const selectionRef = useRef({ selectedItemId, onRefreshSelectedItem });
  selectionRef.current = { selectedItemId, onRefreshSelectedItem };
  const mountedRef = useRef(true);
  const listControllerRef = useRef<AbortController | null>(null);
  const listGenerationRef = useRef(0);
  const detailControllersRef = useRef(new Map<string, AbortController>());
  const retryDelayRef = useRef(30_000);
  const refreshTimerRef = useRef<number | null>(null);
  const refreshLoopRef = useRef<((reconcile?: boolean) => void) | null>(null);

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
    detailControllersRef.current.get(item.id)?.abort();
    const controller = new AbortController();
    detailControllersRef.current.set(item.id, controller);
    setDetailLoading((current) => ({ ...current, [item.id]: true }));
    try {
      const next = await fetchGuardianInboxItem(item.id, controller.signal);
      if (
        !mountedRef.current
        || controller.signal.aborted
        || detailControllersRef.current.get(item.id) !== controller
        || !expandedRef.current[item.id]
      ) return;
      const current = itemsRef.current.find((candidate) => candidate.id === item.id);
      if (!current) {
        detailCacheRef.current.delete(item.id);
        return;
      }
      if (!detailMatchesListItem(current, next)) {
        detailCacheRef.current.delete(item.id);
        detailConfirmedRef.current.delete(item.id);
        setActionError((state) => ({
          ...state,
          [item.id]: "Evidence changed while its detail was loading. Refresh the inbox before inspecting it again.",
        }));
        return;
      }
      detailConfirmedRef.current.add(item.id);
      rememberDetail(next);
      const mergedDetail = mergeCachedDetail(current, next);
      setItems((currentItems) => {
        const updated = currentItems.map((candidate) => (
          candidate.id === next.id ? mergedDetail : candidate
        ));
        itemsRef.current = updated;
        return updated;
      });
      if (selectionRef.current.onRefreshSelectedItem) {
        selectionRef.current.onRefreshSelectedItem(item.id, mergedDetail);
      } else onSelectItem?.(mergedDetail);
    } catch (err) {
      if (
        !mountedRef.current
        || detailControllersRef.current.get(item.id) !== controller
      ) return;
      detailConfirmedRef.current.delete(item.id);
      if (controller.signal.aborted) return;
      setActionError((current) => ({
        ...current,
        [item.id]: err instanceof GuardianInboxApiError
          ? `${err.message}${err.recoveryAction ? ` · recovery: ${err.recoveryAction}` : ""}`
          : "Evidence details unavailable.",
      }));
    } finally {
      const ownsDetailRequest = detailControllersRef.current.get(item.id) === controller;
      if (ownsDetailRequest) {
        detailControllersRef.current.delete(item.id);
      }
      if (mountedRef.current && ownsDetailRequest) {
        setDetailLoading((current) => ({ ...current, [item.id]: false }));
      }
    }
  }, [onSelectItem, rememberDetail]);

  const load = useCallback(async (cursor?: string | null, append = false, reconcile = false): Promise<boolean> => {
    listControllerRef.current?.abort();
    const controller = new AbortController();
    const generation = listGenerationRef.current + 1;
    listGenerationRef.current = generation;
    listControllerRef.current = controller;
    setListConfirmed(false);
    setLoading(true);
    try {
      const page = await fetchGuardianInbox({ limit: pageSize, cursor, signal: controller.signal });
      if (
        !mountedRef.current
        || controller.signal.aborted
        || listControllerRef.current !== controller
        || listGenerationRef.current !== generation
      ) return false;
      const reloadDetails: GuardianInboxItem[] = [];
      const pageItems = page.items.map((item) => {
        const cached = detailCacheRef.current.get(item.id);
        if (!cached) {
          detailConfirmedRef.current.delete(item.id);
          if (expandedRef.current[item.id] && item.evidence_status !== "unavailable") reloadDetails.push(item);
          return item;
        }
        if (!detailMatchesListItem(item, cached)) {
          detailControllersRef.current.get(item.id)?.abort();
          detailControllersRef.current.delete(item.id);
          detailCacheRef.current.delete(item.id);
          detailConfirmedRef.current.delete(item.id);
          if (expandedRef.current[item.id] && item.evidence_status !== "unavailable") reloadDetails.push(item);
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
            const typedAction = action as GuardianInboxAction;
            gestureKeys.current.delete(`${item.id}:${typedAction}`);
            gestureRequests.current.delete(`${item.id}:${typedAction}`);
            clearPersistedGesture(item.id, typedAction);
          });
        });
      }
      setItems(next);
      const selectedId = selectionRef.current.selectedItemId;
      if (selectedId) selectionRef.current.onRefreshSelectedItem?.(
        selectedId, next.find((item) => item.id === selectedId) ?? null,
      );
      setListConfirmed(true);
      setConfirmedPlanOwner(planOwner);
      reloadDetails.forEach((item) => void loadDetail(item));
      setNextCursor(page.next_cursor ?? null);
      setLastConfirmedAt(page.last_confirmed_at ?? new Date().toISOString());
      setStatus(null);
      return true;
    } catch (err) {
      if (
        !mountedRef.current
        || controller.signal.aborted
        || listControllerRef.current !== controller
        || listGenerationRef.current !== generation
        || (err instanceof DOMException && err.name === "AbortError")
      ) return false;
      const message = err instanceof GuardianInboxApiError
        ? err.message
        : "Guardian inbox refresh failed.";
      setListConfirmed(false);
      setStatus(itemsRef.current.length > 0
        ? `Inbox refresh degraded; showing last-known items. ${message}`
        : message);
      return false;
    } finally {
      if (listControllerRef.current === controller && listGenerationRef.current === generation) {
        listControllerRef.current = null;
        if (mountedRef.current) setLoading(false);
      }
    }
    return false;
  }, [loadDetail, pageSize, planOwner]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      listGenerationRef.current += 1;
      listControllerRef.current?.abort();
      listControllerRef.current = null;
      detailControllersRef.current.forEach((controller) => controller.abort());
      detailControllersRef.current.clear();
      detailConfirmedRef.current.clear();
    };
  }, []);

  useEffect(() => {
    expandedRef.current = expanded;
  }, [expanded]);

  useEffect(() => {
    if (!autoLoad || !active) {
      listGenerationRef.current += 1;
      listControllerRef.current?.abort();
      listControllerRef.current = null;
      detailControllersRef.current.forEach((controller) => controller.abort());
      detailControllersRef.current.clear();
      detailConfirmedRef.current.clear();
      setListConfirmed(false);
      setLoading(false);
      refreshLoopRef.current = null;
      if (refreshTimerRef.current !== null) {
        window.clearTimeout(refreshTimerRef.current);
        refreshTimerRef.current = null;
      }
      return;
    }
    let cancelled = false;
    const schedule = () => {
      if (cancelled || pollIntervalMs <= 0) return;
      if (refreshTimerRef.current !== null) window.clearTimeout(refreshTimerRef.current);
      refreshTimerRef.current = window.setTimeout(() => {
        refreshTimerRef.current = null;
        void refresh();
      }, pollIntervalMs === 30_000 ? Math.max(pollIntervalMs, retryDelayRef.current) : pollIntervalMs);
    };
    const refresh = async (reconcile = false) => {
      if (cancelled) return;
      const confirmed = await load(undefined, false, reconcile);
      if (cancelled) return;
      retryDelayRef.current = confirmed
        ? 30_000
        : Math.min(retryDelayRef.current === 30_000 ? 60_000 : 120_000, 120_000);
      schedule();
    };
    refreshLoopRef.current = (reconcile = false) => {
      retryDelayRef.current = reconcile ? 30_000 : retryDelayRef.current;
      void refresh(reconcile);
    };
    void refresh();
    return () => {
      cancelled = true;
      refreshLoopRef.current = null;
      if (refreshTimerRef.current !== null) {
        window.clearTimeout(refreshTimerRef.current);
        refreshTimerRef.current = null;
      }
    };
  }, [active, autoLoad, load, pollIntervalMs]);

  const orderedItems = useMemo(() => items.slice().sort((a, b) => {
    const aPending = a.state === "pending" || a.state === "snoozed";
    const bPending = b.state === "pending" || b.state === "snoozed";
    if (aPending !== bPending) return aPending ? -1 : 1;
    return a.title.localeCompare(b.title);
  }), [items]);

  const filteredItems = useMemo(() => {
    const normalizedText = filterText.trim().toLowerCase();
    return orderedItems.filter((item) => {
      if (filterGoal && item.goal_id !== filterGoal) return false;
      if (filterState && item.state !== filterState) return false;
      if (filterSource && item.source_kind !== filterSource) return false;
      if (!normalizedText) return true;
      return [item.title, item.summary, item.why_now, item.goal_id, item.watch_id, item.source_id, item.source_kind]
        .some((value) => value.toLowerCase().includes(normalizedText));
    });
  }, [filterGoal, filterSource, filterState, filterText, orderedItems]);

  const filterOptions = useMemo(() => ({
    goals: Array.from(new Set(orderedItems.map((item) => item.goal_id).filter(Boolean))).sort(),
    states: Array.from(new Set(orderedItems.map((item) => item.state))).sort(),
    sources: Array.from(new Set(orderedItems.map((item) => item.source_kind).filter(Boolean))).sort(),
  }), [orderedItems]);

  const updateItem = (next: GuardianInboxItem) => {
    setItems((current) => {
      const updated = current.map((item) => item.id === next.id ? next : item);
      itemsRef.current = updated;
      return updated;
    });
  };

  const showDetails = async (item: GuardianInboxItem) => {
    if (expanded[item.id]) {
      detailControllersRef.current.get(item.id)?.abort();
      detailControllersRef.current.delete(item.id);
      detailConfirmedRef.current.delete(item.id);
      setDetailLoading((current) => ({ ...current, [item.id]: false }));
      expandedRef.current[item.id] = false;
      detailCacheRef.current.delete(item.id);
      setExpanded((current) => ({ ...current, [item.id]: false }));
      return;
    }
    expandedRef.current[item.id] = true;
    setExpanded((current) => ({ ...current, [item.id]: true }));
    onSelectItem?.(item);
    const cached = detailCacheRef.current.get(item.id);
    if (cached && detailMatchesListItem(item, cached)) {
      const merged = mergeCachedDetail(item, cached);
      updateItem(merged);
      onSelectItem?.(merged);
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
      const currentValue = snoozeValues[item.id];
      const currentReason = normalizedReason(snoozeReasons[item.id]) || undefined;
      return normalizedSnoozeUntil(currentValue) !== normalizedSnoozeUntil(request.until)
        || currentReason !== request.reason;
    }
    if (action === "dismiss") {
      const currentReason = dismissReasons[item.id]?.trim().slice(0, 500) || undefined;
      return currentReason !== request.reason;
    }
    return false;
  };

  const runAction = useCallback(async (item: GuardianInboxItem, action: GuardianInboxAction) => {
    const actionKey = `${item.id}:${action}`;
    let priorRequest = gestureRequests.current.get(actionKey) ?? null;
    const persisted = priorRequest ? null : readPersistedGesture(item.id, action);
    if (persisted) {
      if (action === "snooze" && normalizedSnoozeUntil(snoozeValues[item.id]) !== normalizedSnoozeUntil(persisted.until)) {
        setActionError((current) => ({
          ...current,
          [item.id]: "Re-enter the original snooze time before retrying this unknown outcome; refresh to start a new gesture.",
        }));
        return;
      }
      if (action === "snooze") {
        const reason = normalizedReason(snoozeReasons[item.id]);
        if ((persisted.reason_digest && (!reason || await reasonDigest(reason) !== persisted.reason_digest))
          || (!persisted.reason_digest && reason)) {
          setActionError((current) => ({
            ...current,
            [item.id]: "Re-enter the same snooze reason before retrying this unknown outcome; refresh to start a new gesture.",
          }));
          return;
        }
      }
      if (action === "dismiss") {
        const reason = normalizedReason(dismissReasons[item.id]);
        if (!reason || !persisted.reason_digest || await reasonDigest(reason) !== persisted.reason_digest) {
          setActionError((current) => ({
            ...current,
            [item.id]: "Re-enter the same dismiss reason before retrying this unknown outcome; refresh to start a new gesture.",
          }));
          return;
        }
      }
      priorRequest = {
        action,
        expected_revision: persisted.expected_revision,
        idempotency_key: persisted.idempotency_key,
        ...(action === "snooze" && persisted.until ? { until: persisted.until } : {}),
        ...(action === "snooze" && normalizedReason(snoozeReasons[item.id]) ? { reason: normalizedReason(snoozeReasons[item.id]) } : {}),
        ...(action === "dismiss" ? { reason: normalizedReason(dismissReasons[item.id]) } : {}),
      };
      gestureKeys.current.set(actionKey, persisted.idempotency_key);
      gestureRequests.current.set(actionKey, priorRequest);
    }
    if (priorRequest && gestureInputChanged(item, action, priorRequest)) {
      setActionError((current) => ({
        ...current,
        [item.id]: "This gesture changed after the failed attempt. Refresh the inbox before sending a new request.",
      }));
      return;
    }
    let request = priorRequest;
    if (!request) {
      if (action === "snooze" && !snoozeValues[item.id]) {
        setActionError((current) => ({ ...current, [item.id]: "Choose a snooze time before submitting." }));
        return;
      }
      if (action === "dismiss" && !normalizedReason(dismissReasons[item.id])) {
        setActionError((current) => ({ ...current, [item.id]: "Enter a reason before dismissing this candidate." }));
        return;
      }
      const key = createGuardianInboxIdempotencyKey(item.id, action);
      const until = action === "snooze" ? normalizedSnoozeUntil(snoozeValues[item.id]) : null;
      if (action === "snooze" && !until) {
        setActionError((current) => ({ ...current, [item.id]: "Choose a valid snooze time before submitting." }));
        return;
      }
      const reason = action === "dismiss"
        ? normalizedReason(dismissReasons[item.id])
        : action === "snooze" ? normalizedReason(snoozeReasons[item.id]) : "";
      request = {
        action,
        expected_revision: item.revision,
        idempotency_key: key,
        ...(until ? { until } : {}),
        ...(reason ? { reason } : {}),
      };
      gestureKeys.current.set(actionKey, key);
      gestureRequests.current.set(actionKey, request);
      const digest = reason ? await reasonDigest(reason) : undefined;
      persistGesture({
        version: 1,
        item_id: item.id,
        action,
        expected_revision: item.revision,
        idempotency_key: key,
        created_at: Date.now(),
        ...(until ? { until } : {}),
        ...(digest ? { reason_digest: digest } : {}),
      });
    }
    if (!request) return;
    setActionBusy(actionKey);
    setActionError((current) => ({ ...current, [item.id]: "" }));
    try {
      const result = await applyGuardianInboxAction(item.id, request);
      const terminal = result.state === "accepted" || result.state === "dismissed" || result.state === "expired";
      const updated = {
        ...item,
        revision: result.revision,
        state: result.state,
        task_id: result.task_id ?? item.task_id,
        snoozed_until: action === "snooze" ? request.until ?? item.snoozed_until : item.snoozed_until,
        // A successful snooze is not eligible for another decision until a
        // fresh server projection says so. Keep the local receipt truthful
        // while the next list/detail read establishes the new boundary.
        allowed_actions: action === "snooze" || terminal ? [] : item.allowed_actions,
        recovery_action: result.recovery_action ?? item.recovery_action,
      };
      updateItem(updated);
      onSelectItem?.(updated);
      setReceipts((current) => ({ ...current, [item.id]: result.receipt_id }));
      setStatus(`${actionLabel(action)} recorded.`);
      setUnknownActions((current) => ({ ...current, [item.id]: false }));
      gestureKeys.current.delete(actionKey);
      gestureRequests.current.delete(actionKey);
      clearPersistedGesture(item.id, action);
      if (autoFocusAcceptedTask && action === "accept_followup" && result.state === "accepted" && result.task_id) {
        onOpenTask?.(result.task_id, updated);
      }
    } catch (err) {
      const message = err instanceof GuardianInboxApiError
        ? `${err.message}${err.code ? ` (${err.code})` : ""}${err.recoveryAction ? ` · recovery: ${err.recoveryAction}` : ""}`
        : `${actionLabel(action)} failed.`;
      if (err instanceof GuardianInboxApiError && err.status === 409) {
        setActionError((current) => ({
          ...current,
          [item.id]: `${message} Another decision won this revision. The current candidate is reloading; the original gesture will be retained.`,
        }));
        void load(undefined, false, false);
      } else if (!(err instanceof GuardianInboxApiError) || err.status >= 500) {
        setUnknownActions((current) => ({ ...current, [item.id]: true }));
        setActionError((current) => ({
          ...current,
          [item.id]: `${message} Outcome unknown; refresh/read evidence and retry the same gesture.`,
        }));
        void (async () => {
          const confirmed = await load(undefined, false, false);
          if (!confirmed) return;
          const current = itemsRef.current.find((candidate) => candidate.id === item.id);
          if (current && ["accepted", "dismissed", "expired"].includes(current.state)) {
            gestureKeys.current.delete(actionKey);
            gestureRequests.current.delete(actionKey);
            clearPersistedGesture(item.id, action);
            setUnknownActions((state) => ({ ...state, [item.id]: false }));
            setActionError((state) => ({ ...state, [item.id]: "The current inbox projection confirms a terminal disposition; the unknown gesture was reconciled." }));
          } else {
            setActionError((state) => ({ ...state, [item.id]: "Outcome unknown; refresh/read evidence and retry the same gesture." }));
          }
        })();
      } else if (err instanceof GuardianInboxApiError && err.status >= 400 && err.status < 500) {
        // A definitive client/request rejection is safe to correct in place.
        // Drop the failed request metadata so the next valid input receives a
        // fresh idempotency key; 409 remains above because its original
        // revision-bound gesture must be reconciled and retried unchanged.
        gestureKeys.current.delete(actionKey);
        gestureRequests.current.delete(actionKey);
        clearPersistedGesture(item.id, action);
        setUnknownActions((current) => ({ ...current, [item.id]: false }));
        setActionError((current) => ({ ...current, [item.id]: message }));
      } else {
        setActionError((current) => ({ ...current, [item.id]: message }));
      }
    } finally {
      setActionBusy(null);
    }
  }, [autoFocusAcceptedTask, dismissReasons, load, onOpenTask, onSelectItem, snoozeReasons, snoozeValues]);

  useImperativeHandle(ref, () => ({
    runAction: (itemId, action) => {
      const item = itemsRef.current.find((candidate) => candidate.id === itemId);
      if (item) void runAction(item, action);
    },
  }), [runAction]);

  const generatePlan = async (item: GuardianInboxItem, retry = false) => {
    if (!currentOwnerPrincipalId || !currentRootId || !planOwner || confirmedPlanOwner !== planOwner || planLock.current || item.degraded || !item.opportunity_id
      || !item.opportunity_revision || !item.plan_offer || planStorageErrors[item.id]) return;
    const retained = planRequests[item.id];
    const reference = item.plan_offer.proposal_ref;
    const retryAllowed = retained && retained.expected_goal_revision === item.goal_revision
      && reference?.generation_retry_allowed === true && reference.provider_contact_state === "not_started";
    if (retry ? !retryAllowed : retained || !item.plan_offer.can_generate) return;
    const request = retained ?? { expected_opportunity_revision: item.opportunity_revision,
      expected_goal_revision: item.goal_revision, idempotency_key: createGuardianUuid() };
    const scope = planOwner;
    const controller = new AbortController(); planController.current = controller;
    planLock.current = true; setPlanBusy(item.id); setPlanError(null);
    try {
      retainOpportunityPlanRequest(opportunityPlanStorageKey(currentOwnerPrincipalId, currentRootId, item.opportunity_id), request);
      setPlanRequests((current) => ({ ...current, [item.id]: request }));
      await generateGuardianOpportunityPlan(item.opportunity_id, request, controller.signal);
      if (controller.signal.aborted || planScope.current !== scope) return;
      await load(undefined, false, false);
    } catch (error) {
      if (!controller.signal.aborted && planScope.current === scope) setPlanError(error instanceof Error ? error.message : "Plan outcome uncertain. Refresh; no automatic retry.");
    } finally {
      if (planScope.current === scope) { planLock.current = false; setPlanBusy(null); }
    }
  };

  const cancelOpportunity = async (item: GuardianInboxItem) => {
    if (!item.cancel_allowed || !item.opportunity_id || !item.opportunity_revision || actionBusy) return;
    const id = item.opportunity_id;
    const request = cancellationRequests.current.get(id) ?? { revision: item.opportunity_revision, key: createGuardianUuid() };
    cancellationRequests.current.set(id, request);
    setActionBusy(`${item.id}:cancel`); setActionError((current) => ({ ...current, [item.id]: "" }));
    try {
      const result = await cancelGuardianOpportunity(id, request.revision, request.key);
      if (!mountedRef.current) return;
      cancellationRequests.current.delete(id);
      setItems((current) => {
        const next = current.map((row) => row.id === item.id ? { ...row, opportunity_revision: result.revision,
          opportunity_status: result.status, state: result.status, cancel_requested: result.cancel_requested,
          quiescent: result.quiescent, cancel_allowed: false, allowed_actions: [], reason_code: result.reason_code } : row);
        itemsRef.current = next;
        return next;
      });
      setReceipts((current) => ({ ...current, [item.id]: result.quiescent
        ? result.status === "cancelled" ? "Cancellation confirmed · native work is quiescent." : "Native work is quiescent; Unknown outcome and cost liability remain retained."
        : "Cancellation requested · waiting for native quiescence. Refresh to read back; no replay or authority renewal." }));
      detailCacheRef.current.delete(item.id); detailConfirmedRef.current.delete(item.id);
    } catch (error) {
      if (!mountedRef.current) return;
      const stale = error instanceof GuardianInboxApiError && error.status === 409;
      setActionError((current) => ({ ...current, [item.id]: stale
        ? "Opportunity changed. Refresh and review before cancelling again; no automatic resubmission."
        : "Cancellation outcome Unknown. Refresh to inspect native quiescence and retained cost liability." }));
      if (stale) cancellationRequests.current.delete(id);
      setItems((current) => {
        const next = current.map((row) => row.id === item.id ? { ...row, cancel_allowed: false } : row);
        itemsRef.current = next; return next;
      });
    } finally { if (mountedRef.current) setActionBusy(null); }
  };

  return (
    <section className="cockpit-outcome-card" data-testid="guardian-inbox-panel" aria-busy={loading ? "true" : "false"}>
      <div className="cockpit-outcome-card-header">
        <div>
          <div className="cockpit-outcome-card-label">Guardian intervention inbox</div>
          <div className="cockpit-outcome-copy">Durable, owner-scoped source changes awaiting your decision.</div>
        </div>
        <button
          type="button"
          onClick={() => {
            retryDelayRef.current = 30_000;
            if (refreshTimerRef.current !== null) {
              window.clearTimeout(refreshTimerRef.current);
              refreshTimerRef.current = null;
            }
            if (refreshLoopRef.current) refreshLoopRef.current(true);
            else void load(undefined, false, true);
          }}
          disabled={loading}
        >
          {loading ? "Refreshing…" : "Refresh"}
        </button>
      </div>
      <div className="cockpit-outcome-card-body" data-testid="guardian-inbox-list">
        {status ? <div className="cockpit-outcome-note" role="status">{status}</div> : null}
        {lastConfirmedAt ? <div className="cockpit-outcome-note">last confirmed · {formatTime(lastConfirmedAt)}</div> : null}
        {!(loading && items.length === 0) ? (
          <div className="guardian-inbox-filters" aria-label="Filter loaded inbox page">
            <input
              aria-label="Filter inbox page"
              value={filterText}
              onChange={(event) => setFilterText(event.target.value)}
              placeholder="Filter this loaded page"
            />
            <select aria-label="Filter inbox by goal" value={filterGoal} onChange={(event) => setFilterGoal(event.target.value)}>
              <option value="">All goals</option>
              {filterOptions.goals.map((value) => <option key={value} value={value}>{value}</option>)}
            </select>
            <select aria-label="Filter inbox by state" value={filterState} onChange={(event) => setFilterState(event.target.value)}>
              <option value="">All states</option>
              {filterOptions.states.map((value) => <option key={value} value={value}>{value}</option>)}
            </select>
            <select aria-label="Filter inbox by source" value={filterSource} onChange={(event) => setFilterSource(event.target.value)}>
              <option value="">All sources</option>
              {filterOptions.sources.map((value) => <option key={value} value={value}>{value}</option>)}
            </select>
            <span className="cockpit-outcome-note" aria-live="polite">{filteredItems.length} of {orderedItems.length} loaded page items</span>
          </div>
        ) : null}
        {loading && items.length === 0 ? (
          <div className="guardian-inbox-skeleton" data-testid="guardian-inbox-loading" role="status" aria-label="Loading guardian decisions">
            <div className="guardian-inbox-skeleton-row" aria-hidden="true" />
            <div className="guardian-inbox-skeleton-row" aria-hidden="true" />
            <div className="guardian-inbox-skeleton-row" aria-hidden="true" />
          </div>
        ) : filteredItems.length === 0 ? (
          <div className="cockpit-outcome-copy">
            {orderedItems.length > 0 ? (
              <>
                <p>No matching decisions on this loaded page.</p>
                <button type="button" onClick={() => { setFilterText(""); setFilterGoal(""); setFilterState(""); setFilterSource(""); }}>Clear filters</button>
              </>
            ) : (
              <>
                <p>No pending decisions.</p>
                <div className="source-watch-actions">
                  {onOpenGoals ? <button type="button" onClick={onOpenGoals}>Open Goals</button> : null}
                  {onOpenWork ? <button type="button" onClick={onOpenWork}>Open Work</button> : null}
                </div>
              </>
            )}
          </div>
        ) : filteredItems.map((item) => {
          const supported = new Set(item.allowed_actions);
          const pendingAction = actionBusy?.startsWith(`${item.id}:`) ?? false;
          const refreshingDetail = Boolean(detailLoading[item.id]);
          const actionReady = listConfirmed || detailConfirmedRef.current.has(item.id);
          const actionDisabled = pendingAction || !actionReady;
          return (
            <article key={item.id} className="source-watch-record" data-testid={`guardian-inbox-row-${item.id}`} data-state={item.state} tabIndex={-1}>
              <div className="cockpit-outcome-primary">{item.title} · {stateLabel(item)}</div>
              <div className="cockpit-outcome-copy">{item.summary}</div>
              <div className="cockpit-outcome-copy">Why now: {item.why_now}</div>
              {item.source_kind === "guardian_opportunity" ? <div className="cockpit-outcome-note">
                <div>Model judgment · relevance {item.assessment?.relevance ?? "unknown"}/4 · confidence {item.assessment?.confidence ?? "unknown"}. Scores are not calibrated probabilities.</div>
                <div>Assessment {item.opportunity_status ?? "unknown"} · reason {item.reason_code ?? "none"} · delivery {item.delivery_status ?? "unknown"} · no learning</div>
                {item.opportunity_status === "unknown" ? <div>Outcome Unknown; retained inference liability. No automatic replay.</div> : null}
                {item.opportunity_status === "silent" ? <div>Silent assessment history; no proposed action.</div> : null}
                {item.reason_code === "goal_review_required" ? <div>Goal review required. Review current Goal and watches before future assessment.</div> : null}
                {item.assessment?.abstain_reason ? <div>Abstention: {item.assessment.abstain_reason}</div> : null}
                {item.assessment ? <div>Advisory blueprint: {item.assessment.suggested_blueprint}. Source and model text do not authorize execution.</div> : null}
              </div> : null}
              <div className="cockpit-outcome-note">
                goal {item.goal_id} rev {item.goal_revision} · watch {item.watch_id} plan {item.plan_revision} · expires {formatTime(item.expires_at)}
              </div>
              <div className="cockpit-outcome-note">
                source {item.source_status ?? "unknown"} · freshness {item.source_freshness ?? "unknown"} · evidence {item.evidence_status ?? item.verification_status ?? "unknown"} · memory {item.memory_status ?? "unknown"}
              </div>
              <div className="cockpit-outcome-note">authority / budget boundary · {item.policy_reason ?? (item.source_kind === "guardian_opportunity" ? "No current boundary reason" : "unavailable in this inbox projection")}</div>
              {item.recovery_action ? <div className="cockpit-outcome-note">recovery · {item.recovery_action}</div> : null}
              {item.degraded ? <div className="cockpit-outcome-note">degraded · server state is not recognized; actions are unavailable</div> : null}
              {item.plan_offer ? <div className="cockpit-outcome-note">
                <div>Available read-only blueprints: {item.plan_offer.available_blueprint_ids.join(", ") || "none"}</div>
                {item.plan_offer.unavailable_reason ? <div>Unmet need: {item.plan_offer.unavailable_reason}</div> : null}
                {item.plan_offer.generation_block_reason ? <div>Plan generation blocked: {item.plan_offer.generation_block_reason}</div> : null}
                {planStorageErrors[item.id] ? <div role="alert">Exact plan request storage is corrupt or unavailable. Generation is blocked.</div> : null}
                {item.plan_offer.proposal_ref ? <div>
                  <div>Plan {item.plan_offer.proposal_ref.status} · non-executable staging until explicit acceptance · no_learning</div>
                  <button type="button" onClick={() => onOpenTask?.(item.plan_offer!.proposal_ref!.parent_task_id, item)}>Review staged read-only plan in Work</button>
                </div> : null}
                {planRequests[item.id] ? <button type="button" disabled={!planOwner || confirmedPlanOwner !== planOwner || Boolean(planBusy) || item.degraded || planStorageErrors[item.id]
                  || planRequests[item.id]?.expected_goal_revision !== item.goal_revision
                  || item.plan_offer.proposal_ref?.generation_retry_allowed !== true || item.plan_offer.proposal_ref?.provider_contact_state !== "not_started"}
                  onClick={() => void generatePlan(item, true)}>Retry exact never-contacted plan request</button>
                  : <button type="button" disabled={!planOwner || confirmedPlanOwner !== planOwner || Boolean(planBusy) || item.degraded || planStorageErrors[item.id] || !item.plan_offer.can_generate}
                    onClick={() => void generatePlan(item)}>Generate plan</button>}
                {planError ? <div role="alert">{planError}</div> : null}
              </div> : null}
              <div className="source-watch-actions">
                {item.cancel_allowed && item.opportunity_id && item.opportunity_revision ? <button type="button" disabled={actionDisabled} onClick={() => void cancelOpportunity(item)}>
                  {actionBusy === `${item.id}:cancel` ? "Requesting cancellation…" : "Cancel assessment"}
                </button> : null}
                {supported.has("accept_followup") ? (
                  <button type="button" onClick={() => void runAction(item, "accept_followup")} disabled={actionDisabled}>
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
                      disabled={actionDisabled}
                    />
                    <input
                      aria-label={`Snooze reason for ${item.title}`}
                      value={snoozeReasons[item.id] ?? ""}
                      onChange={(event) => setSnoozeReasons((current) => ({ ...current, [item.id]: event.target.value }))}
                      placeholder="Snooze reason (optional)"
                      maxLength={500}
                      disabled={actionDisabled}
                    />
                    <button type="button" onClick={() => void runAction(item, "snooze")} disabled={actionDisabled || !snoozeValues[item.id]}>
                      {actionBusy === `${item.id}:snooze` ? "Snoozing…" : "Snooze"}
                    </button>
                    {!snoozeValues[item.id] ? <span className="cockpit-outcome-note">Choose a time before snoozing.</span> : null}
                  </>
                ) : null}
                {supported.has("dismiss") ? (
                  <>
                    <input
                      aria-label={`Dismiss reason for ${item.title}`}
                      value={dismissReasons[item.id] ?? ""}
                      onChange={(event) => setDismissReasons((current) => ({ ...current, [item.id]: event.target.value }))}
                      placeholder="Dismiss reason (required)"
                      maxLength={500}
                      disabled={actionDisabled}
                    />
                    <button type="button" onClick={() => void runAction(item, "dismiss")} disabled={actionDisabled || !dismissReasons[item.id]?.trim()}>
                      {actionBusy === `${item.id}:dismiss` ? "Dismissing…" : "Dismiss"}
                    </button>
                    {!dismissReasons[item.id]?.trim() ? <span className="cockpit-outcome-note">A reason is required before dismissing.</span> : null}
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
              {!actionReady && !item.degraded ? (
                <div className="cockpit-outcome-note" role="status">Refresh before acting because this candidate is not currently confirmed.</div>
              ) : null}
              {expanded[item.id] ? (
                <div className="cockpit-outcome-note">
                  {refreshingDetail ? <div role="status">Refreshing verified evidence details…</div> : null}
                  {item.source_kind === "guardian_opportunity" && item.assessment ? <div aria-label="Exact assessment citations">
                    <div>Literal model judgment: {item.assessment.reason}</div>
                    {item.assessment.citations.map((citation, index) => {
                      const preview = item.evidence_previews?.find((entry) => entry.source_id === citation.source_id);
                      const lines = preview?.text?.split("\n");
                      const exact = lines && citation.end_line <= lines.length ? lines.slice(citation.start_line - 1, citation.end_line).join("\n") : null;
                      return <div key={`${citation.source_id}:${index}`}>
                        <div>Source {citation.source_id} · normalized redacted lines {citation.start_line}–{citation.end_line} · span SHA256 {citation.span_sha256}</div>
                        {exact && !refreshingDetail && detailConfirmedRef.current.has(item.id) && item.evidence_status === "verified"
                          ? <pre className="whitespace-pre-wrap">{exact}</pre> : <div>Exact cited excerpt unavailable; refresh authorized evidence.</div>}
                      </div>;
                    })}
                    <div>Citations establish provenance; they do not establish semantic truth or grant authority.</div>
                  </div> : null}
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
                  {item.action_history?.length || item.action_history_truncated ? (
                    <div className="mt-2" aria-label="Decision history">
                      <div>decision history</div>
                      {(item.action_history ?? []).map((entry) => (
                        <div key={`${item.id}:history:${entry.receipt_id}`} className="cockpit-outcome-note">
                          {entry.action} · {entry.outcome} · receipt {entry.receipt_id || "unavailable"}
                          {entry.created_at ? ` · ${formatTime(entry.created_at)}` : ""}
                          {entry.reason_state === "provided" && entry.safe_reason ? ` · reason: ${entry.safe_reason}` : ` · reason ${entry.reason_state}`}
                          {entry.task_id && onOpenTask ? <> · <button type="button" onClick={() => onOpenTask(entry.task_id as string, item)}>Open task {entry.task_id}</button></> : null}
                        </div>
                      ))}
                      {item.action_history_truncated ? <div>Older decision history is not shown.</div> : null}
                    </div>
                  ) : null}
                  {item.task_id ? (
                    <a
                      href={safeHref(item.task_url) ?? `/cockpit?task_id=${encodeURIComponent(item.task_id)}`}
                      onClick={(event) => {
                        if (!onOpenTask) return;
                        event.preventDefault();
                        onOpenTask(item.task_id as string, item);
                      }}
                    >
                      Open accepted task {item.task_id}
                    </a>
                  ) : <div>No accepted task yet.</div>}
                  {safeHref(item.watch_url) ? <a href={safeHref(item.watch_url) as string}>Open source watch</a> : null}
                </div>
              ) : null}
              {receipts[item.id] ? (
                <div className="cockpit-outcome-note" role="status">
                  receipt · {receipts[item.id]}
                  {item.task_id ? <> · <button type="button" onClick={() => onOpenTask?.(item.task_id as string, item)}>Open accepted task {item.task_id}</button></> : null}
                </div>
              ) : null}
              {unknownActions[item.id] ? (
                <button
                  type="button"
                  onClick={() => {
                    if (refreshLoopRef.current) refreshLoopRef.current(false);
                    else void load(undefined, false, false);
                  }}
                >
                  Refresh and retain gesture
                </button>
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
});
