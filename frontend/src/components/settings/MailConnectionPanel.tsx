import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import {
  MailApiError,
  MailBodyField,
  MailConnectionMetadata,
  MailConsentMetadata,
  MailLabelMetadata,
  MailMessageMetadata,
  MailMessageReadResponse,
  MailWatchMetadata,
  controlMailWatch,
  createMailConnection,
  createMailConsent,
  createMailWatch,
  getMailConnectionRecovery,
  getMailWatchRecovery,
  getMailWatch,
  listMailWatches,
  listMailConnections,
  listMailConsents,
  listMailLabels,
  makeMailIdempotencyKey,
  readMailMessage,
  refreshMailLabels,
  revokeMailWatch,
  setMailModelConsent,
  scanMailMessages,
  withMailDeadline as bounded,
  verifyMailConnection,
} from "../../lib/mailApi";
import type { GoalInfo } from "../../types";

const GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly" as const;
const BODY_FIELDS: MailBodyField[] = ["subject", "plainbody", "replyintent"];

export interface MailConnectionPanelProps {
  ownerPrincipalId?: string | null;
  ownerSessionId?: string | null;
}

interface PendingSetup {
  version: 1;
  idempotencyKey: string;
  label: string;
}

interface PendingWatch {
  version: 1;
  idempotencyKey: string;
  action: "create" | "pause" | "resume" | "revoke";
  watchId: string | null;
  expectedBindingRevision: number | null;
}

interface MailSelectionSnapshot {
  ownerScope: string | null;
  connectionId: string | null;
  connectionRevision: number | null;
  goalId: string | null;
  goalRevision: number | null;
  consentId: string | null;
  consentRevision: number | null;
  sourceRevision: number | null;
  labelIds: string[];
}

const PENDING_SETUP_STORAGE_PREFIX = "seraph:mail-setup-recovery:v1:";
const PENDING_WATCH_STORAGE_PREFIX = "seraph:mail-watch-recovery:v1:";
const SAFE_IDEMPOTENCY_KEY = /^[A-Za-z0-9_.:@+,\-]{1,256}$/;

function pendingSetupStorageKey(ownerScope: string): string {
  return `${PENDING_SETUP_STORAGE_PREFIX}${encodeURIComponent(ownerScope)}`;
}

function readPendingSetup(ownerScope: string | null): PendingSetup | null {
  if (!ownerScope || typeof window === "undefined") return null;
  try {
    const raw = window.sessionStorage.getItem(pendingSetupStorageKey(ownerScope));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { version?: unknown; idempotencyKey?: unknown; label?: unknown };
    return parsed.version === 1 && typeof parsed.idempotencyKey === "string" && SAFE_IDEMPOTENCY_KEY.test(parsed.idempotencyKey)
      && typeof parsed.label === "string" && parsed.label.trim().length > 0 && parsed.label.length <= 200
      ? { version: 1, idempotencyKey: parsed.idempotencyKey, label: parsed.label }
      : null;
  } catch {
    return null;
  }
}

function writePendingSetup(ownerScope: string | null, pending: PendingSetup): boolean {
  if (!ownerScope || typeof window === "undefined") return false;
  try {
    const key = pendingSetupStorageKey(ownerScope);
    const serialized = JSON.stringify(pending);
    window.sessionStorage.setItem(key, serialized);
    return window.sessionStorage.getItem(key) === serialized;
  } catch {
    // Credentials are never persisted; the canonical server key remains the authority.
    return false;
  }
}

function clearPendingSetup(ownerScope: string | null): void {
  if (!ownerScope || typeof window === "undefined") return;
  try {
    window.sessionStorage.removeItem(pendingSetupStorageKey(ownerScope));
  } catch {
    // A storage failure must not authorize a new credential import.
  }
}

function pendingWatchStorageKey(ownerScope: string): string {
  return `${PENDING_WATCH_STORAGE_PREFIX}${encodeURIComponent(ownerScope)}`;
}

function readPendingWatch(ownerScope: string | null): PendingWatch | null {
  if (!ownerScope || typeof window === "undefined") return null;
  try {
    const raw = window.sessionStorage.getItem(pendingWatchStorageKey(ownerScope));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { version?: unknown; idempotencyKey?: unknown; action?: unknown; watchId?: unknown; expectedBindingRevision?: unknown };
    const action = parsed.action === "create" || parsed.action === "pause" || parsed.action === "resume" || parsed.action === "revoke" ? parsed.action : "create";
    const watchId = parsed.watchId === null || parsed.watchId === undefined ? null : typeof parsed.watchId === "string" && parsed.watchId.trim() ? parsed.watchId : null;
    const expectedBindingRevision = parsed.expectedBindingRevision === null || parsed.expectedBindingRevision === undefined
      ? null
      : typeof parsed.expectedBindingRevision === "number" && Number.isSafeInteger(parsed.expectedBindingRevision) && parsed.expectedBindingRevision > 0
        ? parsed.expectedBindingRevision
        : null;
    return typeof parsed.idempotencyKey === "string" && SAFE_IDEMPOTENCY_KEY.test(parsed.idempotencyKey)
      && (action === "create" || (watchId !== null && expectedBindingRevision !== null))
      ? { version: 1, idempotencyKey: parsed.idempotencyKey, action, watchId, expectedBindingRevision }
      : null;
  } catch {
    return null;
  }
}

function writePendingWatch(ownerScope: string | null, pending: PendingWatch): boolean {
  if (!ownerScope || typeof window === "undefined") return false;
  try {
    const key = pendingWatchStorageKey(ownerScope);
    const serialized = JSON.stringify(pending);
    window.sessionStorage.setItem(key, serialized);
    return window.sessionStorage.getItem(key) === serialized;
  } catch {
    // Browser storage is only a recovery hint; the server remains authoritative.
    return false;
  }
}

function clearPendingWatch(ownerScope: string | null): void {
  if (!ownerScope || typeof window === "undefined") return;
  try {
    window.sessionStorage.removeItem(pendingWatchStorageKey(ownerScope));
  } catch {
    // A storage failure must not turn a confirmed server result into a retry.
  }
}

interface GoalOption {
  id: string;
  title: string;
  revision: number;
  proactive_enabled?: boolean;
}

function flattenGoals(tree: GoalInfo[]): GoalOption[] {
  const result: GoalOption[] = [];
  const visit = (goal: GoalInfo) => {
    if (typeof goal.revision === "number" && Number.isSafeInteger(goal.revision) && goal.revision > 0 && goal.status === "active") {
      result.push({ id: goal.id, title: goal.title, revision: goal.revision, proactive_enabled: goal.proactive_enabled });
    }
    goal.children?.forEach(visit);
  };
  tree.forEach(visit);
  return result;
}

function idempotencyKey(prefix: string): string {
  return makeMailIdempotencyKey(prefix);
}

function toIso(value: string): string | null {
  const parsed = new Date(value);
  return Number.isFinite(parsed.getTime()) ? parsed.toISOString() : null;
}

function localInput(value: Date): string {
  const offset = value.getTimezoneOffset() * 60_000;
  return new Date(value.getTime() - offset).toISOString().slice(0, 16);
}

function safeError(error: unknown): string {
  if (error instanceof MailApiError) {
    if (error.status === 401) return "The operator session is unavailable. Sign in again before continuing.";
    if (error.status === 409) return `${error.message} Refresh the current Mail metadata before retrying.`;
    if (error.status >= 400 && error.status < 500) return error.message;
  }
  return "The Mail operation has no confirmed outcome. Keep the exact request context and reconcile it explicitly.";
}

function isUnknown(error: unknown): boolean {
  return error instanceof MailApiError && (error.status === 0 || error.status >= 500 || error.status === 200);
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function activeConsent(consent: MailConsentMetadata | null): boolean {
  return Boolean(consent && consent.state === "active" && consent.source_read_allowed && Date.parse(consent.expires_at) > Date.now());
}

export function MailConnectionPanel({ ownerPrincipalId, ownerSessionId }: MailConnectionPanelProps) {
  const ownerScope = ownerPrincipalId && ownerSessionId ? `${ownerPrincipalId}\u0000${ownerSessionId}` : null;
  const mountedRef = useRef(true);
  const generationRef = useRef(0);
  const ownerScopeRef = useRef<string | null>(ownerScope);
  ownerScopeRef.current = ownerScope;
  const requestControllerRef = useRef<AbortController | null>(null);
  const [connections, setConnections] = useState<MailConnectionMetadata[]>([]);
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [metadataStale, setMetadataStale] = useState(false);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [label, setLabel] = useState("");
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [refreshToken, setRefreshToken] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [pendingSetup, setPendingSetup] = useState<PendingSetup | null>(null);
  const [selectedConnectionId, setSelectedConnectionId] = useState("");
  const [labels, setLabels] = useState<Record<string, MailLabelMetadata[]>>({});
  const [labelStatus, setLabelStatus] = useState<Record<string, string>>({});
  const [connectionBusy, setConnectionBusy] = useState<string | null>(null);
  const [goals, setGoals] = useState<GoalOption[]>([]);
  const [goalError, setGoalError] = useState<string | null>(null);
  const [selectedGoalId, setSelectedGoalId] = useState("");
  const [selectedLabelIds, setSelectedLabelIds] = useState<string[]>([]);
  const [consents, setConsents] = useState<MailConsentMetadata[]>([]);
  const [consentError, setConsentError] = useState<string | null>(null);
  const [consentBusy, setConsentBusy] = useState(false);
  const [consentExpiry, setConsentExpiry] = useState(() => localInput(new Date(Date.now() + 24 * 60 * 60 * 1000)));
  const [maxMessages, setMaxMessages] = useState("10");
  const [sourceAcknowledged, setSourceAcknowledged] = useState(false);
  const [scanBusy, setScanBusy] = useState(false);
  const [scanError, setScanError] = useState<string | null>(null);
  const [receivedAfter, setReceivedAfter] = useState(() => localInput(new Date(Date.now() - 24 * 60 * 60 * 1000)));
  const [scan, setScan] = useState<{ messages: MailMessageMetadata[]; coverage: { more_available: boolean; list_page_complete: boolean; returned: number } } | null>(null);
  const [selectedMessageId, setSelectedMessageId] = useState("");
  const [bodyReadAcknowledged, setBodyReadAcknowledged] = useState(false);
  const [privateMessage, setPrivateMessage] = useState<MailMessageReadResponse | null>(null);
  const [readBusy, setReadBusy] = useState(false);
  const [readError, setReadError] = useState<string | null>(null);
  const [modelBusy, setModelBusy] = useState(false);
  const [modelError, setModelError] = useState<string | null>(null);
  const [modelAcknowledged, setModelAcknowledged] = useState(false);
  const [modelAllowed, setModelAllowed] = useState(false);
  const [watchBusy, setWatchBusy] = useState(false);
  const [watchError, setWatchError] = useState<string | null>(null);
  const [watch, setWatch] = useState<MailWatchMetadata | null>(null);
  const [watches, setWatches] = useState<MailWatchMetadata[]>([]);
  const [watchListError, setWatchListError] = useState<string | null>(null);
  const [watchListStale, setWatchListStale] = useState(false);
  const [pendingWatch, setPendingWatch] = useState<PendingWatch | null>(null);
  const [watchCadence, setWatchCadence] = useState<"hourly" | "6h">("hourly");
  const [watchTimezone, setWatchTimezone] = useState(() => Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC");
  const [watchExpiry, setWatchExpiry] = useState(() => localInput(new Date(Date.now() + 24 * 60 * 60 * 1000)));
  const watchControllerRef = useRef<AbortController | null>(null);

  const selectedConnection = useMemo(() => connections.find((item) => item.connection_id === selectedConnectionId) ?? null, [connections, selectedConnectionId]);
  const selectedGoal = useMemo(() => goals.find((goal) => goal.id === selectedGoalId) ?? null, [goals, selectedGoalId]);
  const selectedLabels = useMemo(() => labels[selectedConnectionId] ?? [], [labels, selectedConnectionId]);
  const selectedConsent = useMemo(() => consents.find((item) => (
    item.connection_id === selectedConnectionId
    && item.connection_revision === selectedConnection?.revision
    && item.goal_id === selectedGoalId
    && item.goal_revision === selectedGoal?.revision
    && item.state === "active"
    && item.source_read_allowed
    && Date.parse(item.expires_at) > Date.now()
  )) ?? null, [consents, selectedConnectionId, selectedConnection?.revision, selectedGoalId, selectedGoal?.revision]);
  const selectedMessage = useMemo(() => scan?.messages.find((item) => item.source_binding_id === selectedMessageId) ?? null, [scan, selectedMessageId]);
  const selectionRef = useRef<MailSelectionSnapshot>({
    ownerScope,
    connectionId: selectedConnection?.connection_id ?? null,
    connectionRevision: selectedConnection?.revision ?? null,
    goalId: selectedGoal?.id ?? null,
    goalRevision: selectedGoal?.revision ?? null,
    consentId: selectedConsent?.consent_id ?? null,
    consentRevision: selectedConsent?.revision ?? null,
    sourceRevision: selectedConsent?.source_revision ?? null,
    labelIds: [...selectedLabelIds],
  });
  selectionRef.current = {
    ownerScope,
    connectionId: selectedConnection?.connection_id ?? null,
    connectionRevision: selectedConnection?.revision ?? null,
    goalId: selectedGoal?.id ?? null,
    goalRevision: selectedGoal?.revision ?? null,
    consentId: selectedConsent?.consent_id ?? null,
    consentRevision: selectedConsent?.revision ?? null,
    sourceRevision: selectedConsent?.source_revision ?? null,
    labelIds: [...selectedLabelIds],
  };

  const isCurrentRequest = (generation: number, requestOwnerScope: string | null): boolean => (
    mountedRef.current
    && generation === generationRef.current
    && ownerScopeRef.current === requestOwnerScope
  );
  const sameSelection = (snapshot: MailSelectionSnapshot): boolean => {
    const current = selectionRef.current;
    return current.ownerScope === snapshot.ownerScope
      && current.connectionId === snapshot.connectionId
      && current.connectionRevision === snapshot.connectionRevision
      && current.goalId === snapshot.goalId
      && current.goalRevision === snapshot.goalRevision
      && current.consentId === snapshot.consentId
      && current.consentRevision === snapshot.consentRevision
      && current.sourceRevision === snapshot.sourceRevision
      && current.labelIds.length === snapshot.labelIds.length
      && current.labelIds.every((value, index) => value === snapshot.labelIds[index]);
  };

  const clearPrivateState = useCallback(() => {
    setScan(null);
    setSelectedMessageId("");
    setBodyReadAcknowledged(false);
    setPrivateMessage(null);
    setReadError(null);
    setModelAcknowledged(false);
    setModelAllowed(false);
    setWatch(null);
  }, []);

  const loadWatches = useCallback(async () => {
    if (!ownerScope) {
      watchControllerRef.current?.abort();
      setWatches([]);
      setWatch(null);
      setWatchListError(null);
      setWatchListStale(false);
      return;
    }
    const generation = generationRef.current;
    watchControllerRef.current?.abort();
    const controller = new AbortController();
    watchControllerRef.current = controller;
    try {
      const result = await bounded((signal) => listMailWatches(signal), controller.signal);
      if (!mountedRef.current || generation !== generationRef.current || controller.signal.aborted) return;
      setWatches(result);
      setWatch((current) => current ? result.find((item) => item.watch_id === current.watch_id) ?? null : null);
      setWatchListError(null);
      setWatchListStale(false);
    } catch (error) {
      if (mountedRef.current && generation === generationRef.current && !isAbort(error)) {
        setWatchListStale(true);
        setWatchListError("Saved Mail watches could not be reconciled; last confirmed watch state remains visible. Retry explicitly.");
      }
    }
  }, [ownerScope]);

  const loadGoals = useCallback(async (generation: number, signal: AbortSignal) => {
    try {
      const { response, payload } = await bounded(async (innerSignal) => {
        const nextResponse = await apiFetch(`${API_URL}/api/goals/tree`, { method: "GET", signal: innerSignal });
        return { response: nextResponse, payload: await nextResponse.json().catch(() => null) };
      }, signal);
      if (!response.ok) throw new Error("goals_unavailable");
      const tree = Array.isArray(payload) ? payload : payload && typeof payload === "object" && Array.isArray((payload as { goals?: unknown }).goals) ? (payload as { goals: GoalInfo[] }).goals : [];
      if (!mountedRef.current || generation !== generationRef.current) return;
      const next = flattenGoals(tree as GoalInfo[]);
      setGoals(next);
      setSelectedGoalId((current) => current && next.some((goal) => goal.id === current) ? current : next[0]?.id ?? "");
      setGoalError(null);
    } catch (error) {
      if (mountedRef.current && generation === generationRef.current && !isAbort(error)) setGoalError("Owned active goals are unavailable. Refresh before creating Mail consent.");
    }
  }, []);

  const loadMetadata = useCallback(async () => {
    if (!ownerScope) {
      generationRef.current += 1;
      requestControllerRef.current?.abort();
      setConnections([]);
      setSelectedConnectionId("");
      setLabels({});
      setConsents([]);
      clearPrivateState();
      return;
    }
    const generation = generationRef.current + 1;
    generationRef.current = generation;
    requestControllerRef.current?.abort();
    const controller = new AbortController();
    requestControllerRef.current = controller;
    setLoading(true);
    setConnectionError(null);
    try {
      const [connectionResult] = await Promise.all([
        bounded((signal) => listMailConnections(signal), controller.signal),
        loadGoals(generation, controller.signal),
      ]);
      if (!mountedRef.current || generation !== generationRef.current || controller.signal.aborted) return;
      setConnections(connectionResult);
      setSelectedConnectionId((current) => current && connectionResult.some((item) => item.connection_id === current) ? current : connectionResult.find((item) => item.state === "active")?.connection_id ?? "");
      setMetadataStale(false);
      setConnectionError(null);
      const active = connectionResult.find((item) => item.state === "active");
      if (active) {
        const [labelResult, consentResult] = await Promise.all([
          bounded((signal) => listMailLabels(active.connection_id, signal), controller.signal),
          bounded((signal) => listMailConsents(active.connection_id, signal), controller.signal),
        ]);
        if (!mountedRef.current || generation !== generationRef.current || controller.signal.aborted) return;
        setLabels({ [active.connection_id]: labelResult.labels });
        setConsents(consentResult.consents);
      }
    } catch (error) {
      if (mountedRef.current && generation === generationRef.current && !isAbort(error)) {
        setMetadataStale(true);
        setConnectionError("Mail metadata is unavailable; the last confirmed metadata remains visible. Retry explicitly to reconcile.");
      }
    } finally {
      if (mountedRef.current && generation === generationRef.current) setLoading(false);
    }
  }, [clearPrivateState, loadGoals, ownerScope]);

  useEffect(() => {
    mountedRef.current = true;
    void loadMetadata();
    return () => {
      mountedRef.current = false;
      requestControllerRef.current?.abort();
      generationRef.current += 1;
    };
  }, [loadMetadata]);

  useEffect(() => {
    setPendingSetup(readPendingSetup(ownerScope));
    setPendingWatch(readPendingWatch(ownerScope));
    setWatchError(null);
    if (!ownerScope) {
      clearPrivateState();
      setWatches([]);
      setWatchListError(null);
      setWatchListStale(false);
      return;
    }
    void loadWatches();
    return () => watchControllerRef.current?.abort();
  }, [clearPrivateState, loadWatches, ownerScope]);

  useEffect(() => {
    if (!selectedConnectionId || !ownerScope) return;
    const generation = generationRef.current;
    const controller = new AbortController();
    void Promise.all([
      bounded((signal) => listMailLabels(selectedConnectionId, signal), controller.signal),
      bounded((signal) => listMailConsents(selectedConnectionId, signal), controller.signal),
    ]).then(([labelResult, consentResult]) => {
      if (!mountedRef.current || generation !== generationRef.current || controller.signal.aborted) return;
      setLabels((current) => ({ ...current, [selectedConnectionId]: labelResult.labels }));
      setConsents(consentResult.consents);
      setLabelStatus((current) => ({ ...current, [selectedConnectionId]: "Cached labels loaded; no provider contact." }));
    }).catch((error) => {
      if (mountedRef.current && generation === generationRef.current && !isAbort(error)) setLabelStatus((current) => ({ ...current, [selectedConnectionId]: "Cached labels are unavailable; refresh metadata before creating consent." }));
    });
    return () => controller.abort();
  }, [ownerScope, selectedConnectionId]);

  const reconcileSetup = async () => {
    if (!ownerScope || !pendingSetup) {
      setFormError("There is no owner-scoped Gmail setup request to reconcile.");
      return;
    }
    const generation = generationRef.current;
    const requestOwnerScope = ownerScope;
    setFormError(null);
    setSubmitting(true);
    try {
      const result = await bounded((signal) => getMailConnectionRecovery(pendingSetup.idempotencyKey, signal));
      if (!isCurrentRequest(generation, requestOwnerScope)) return;
      if (result.idempotency_key !== pendingSetup.idempotencyKey) throw new MailApiError(200, "setup_recovery_mismatch", "The setup recovery did not match the original key.", "refresh_mail_metadata");
      if (result.status === "replayed" && result.connection && result.connection.state === "active") {
        clearPendingSetup(requestOwnerScope);
        setPendingSetup(null);
        setConnections((current) => [result.connection as MailConnectionMetadata, ...current.filter((item) => item.connection_id !== result.connection?.connection_id)]);
        setSelectedConnectionId(result.connection.connection_id);
        setLabel("");
        setFormError(null);
      } else if (result.status === "blocked") {
        setFormError("The original Gmail setup is canonically blocked. Refresh metadata and resolve that setup before importing credentials again.");
      } else {
        setFormError("The original Gmail setup has no confirmed terminal outcome. Its exact key is retained; refresh and reconcile it again.");
      }
    } catch {
      if (isCurrentRequest(generation, requestOwnerScope)) setFormError("The original Gmail setup could not be confirmed. Its exact key is retained; no replacement credential import was created.");
    } finally {
      if (isCurrentRequest(generation, requestOwnerScope)) setSubmitting(false);
    }
  };

  const submitConnection = async (event: FormEvent) => {
    event.preventDefault();
    setFormError(null);
    if (!ownerScope) return setFormError("Sign in through the operator session before importing Gmail credentials.");
    if (pendingSetup) {
      await reconcileSetup();
      return;
    }
    const normalizedLabel = label.trim();
    if (!normalizedLabel || !clientId.trim() || !refreshToken.trim()) return setFormError("Label, client ID, and refresh token are required.");
    const key = idempotencyKey("gmail-setup");
    const pending = { version: 1 as const, idempotencyKey: key, label: normalizedLabel };
    if (!writePendingSetup(ownerScope, pending)) {
      return setFormError("Browser recovery storage is unavailable. The Gmail credential import is blocked until this tab can retain its opaque request key.");
    }
    const generation = generationRef.current;
    const requestOwnerScope = ownerScope;
    // Keep the opaque recovery identity in component state before dispatch as
    // well as in sessionStorage. A response can be delayed or become stale
    // across an owner/session change.
    setPendingSetup(pending);
    const request = {
      schema_version: 1 as const,
      service: "gmail_readonly" as const,
      label: normalizedLabel,
      client_id: clientId,
      ...(clientSecret ? { client_secret: clientSecret } : {}),
      refresh_token: refreshToken,
      declared_scopes: [GMAIL_SCOPE] as [typeof GMAIL_SCOPE],
      idempotency_key: key,
    };
    // Clear credential fields before awaiting the network so they never leak
    // into a pending request record, persistence, or an error message.
    setClientId("");
    setClientSecret("");
    setRefreshToken("");
    setSubmitting(true);
    try {
      const result = await bounded((signal) => createMailConnection(request, signal));
      if (!isCurrentRequest(generation, requestOwnerScope)) return;
      setConnections((current) => [result, ...current.filter((item) => item.connection_id !== result.connection_id)]);
      setSelectedConnectionId(result.connection_id);
      clearPendingSetup(requestOwnerScope);
      setPendingSetup(null);
      setLabel("");
      setFormError(null);
    } catch (error) {
      if (!isCurrentRequest(generation, requestOwnerScope) || isAbort(error)) return;
      if (isUnknown(error)) {
        setPendingSetup(pending);
        setFormError("The setup outcome is unknown. Credentials were cleared; refresh Mail metadata to reconcile the exact setup key before trying another import.");
      } else {
        clearPendingSetup(requestOwnerScope);
        setPendingSetup(null);
        setFormError(safeError(error));
      }
    } finally {
      if (isCurrentRequest(generation, requestOwnerScope)) setSubmitting(false);
    }
  };

  const verify = async (connection: MailConnectionMetadata) => {
    const generation = generationRef.current;
    setConnectionBusy(connection.connection_id);
    setFormError(null);
    try {
      const result = await bounded((signal) => verifyMailConnection(connection.connection_id, { expected_revision: connection.revision, request_uuid: idempotencyKey("gmail-verify") }, signal));
      if (!mountedRef.current || generation !== generationRef.current) return;
      const nextRevision = typeof result.connection_revision === "number" ? result.connection_revision : connection.revision;
      setConnections((items) => items.map((item) => item.connection_id === connection.connection_id ? { ...item, revision: nextRevision, scope_status: typeof result.scope_status === "string" ? result.scope_status : item.scope_status, provider_scopes_verified: result.provider_scopes_verified === true, verified_setup_job_id: typeof result.control_job_id === "string" ? result.control_job_id : item.verified_setup_job_id } : item));
      setLabelStatus((current) => ({ ...current, [connection.connection_id]: `Provider verification completed with an explicit labels read (${typeof result.label_count === "number" ? result.label_count : "bounded"} labels).` }));
    } catch (error) {
      if (mountedRef.current && generation === generationRef.current && !isAbort(error)) setFormError(safeError(error));
    } finally {
      if (mountedRef.current) setConnectionBusy(null);
    }
  };

  const refreshLabelsFor = async (connection: MailConnectionMetadata) => {
    const generation = generationRef.current;
    setConnectionBusy(connection.connection_id);
    setLabelStatus((current) => ({ ...current, [connection.connection_id]: "Refreshing labels with explicit account-label acknowledgement…" }));
    try {
      await bounded((signal) => refreshMailLabels({ connection_id: connection.connection_id, expected_connection_revision: connection.revision, acknowledge_account_label_read: true, request_uuid: idempotencyKey("gmail-labels") }, signal));
      const result = await bounded((signal) => listMailLabels(connection.connection_id, signal));
      if (!mountedRef.current || generation !== generationRef.current) return;
      setLabels((current) => ({ ...current, [connection.connection_id]: result.labels }));
      setLabelStatus((current) => ({ ...current, [connection.connection_id]: `Cached ${result.labels.length} labels loaded after explicit provider contact.` }));
    } catch (error) {
      if (mountedRef.current && generation === generationRef.current && !isAbort(error)) setLabelStatus((current) => ({ ...current, [connection.connection_id]: safeError(error) }));
    } finally {
      if (mountedRef.current) setConnectionBusy(null);
    }
  };

  const createConsentForSource = async (event: FormEvent) => {
    event.preventDefault();
    setConsentError(null);
    const expiry = toIso(consentExpiry);
    const limit = Number(maxMessages);
    if (!selectedConnection || selectedConnection.state !== "active") return setConsentError("Choose an active Gmail connection.");
    if (!selectedGoal) return setConsentError("Choose a current active Goal revision.");
    if (selectedLabelIds.length < 1 || selectedLabelIds.length > 3) return setConsentError("Choose one to three current labels.");
    if (!expiry || Date.parse(expiry) <= Date.now() || Date.parse(expiry) > Date.now() + 7 * 24 * 60 * 60 * 1000) return setConsentError("Consent expiry must be in the future and within seven days.");
    if (!Number.isInteger(limit) || limit < 1 || limit > 10) return setConsentError("The source limit must be between one and ten messages.");
    if (!sourceAcknowledged) return setConsentError("Acknowledge the bounded source read before creating consent.");
    const generation = generationRef.current;
    const snapshot: MailSelectionSnapshot = {
      ...selectionRef.current,
      labelIds: [...selectedLabelIds],
    };
    const requestOwnerScope = snapshot.ownerScope;
    setConsentBusy(true);
    try {
      const result = await bounded((signal) => createMailConsent({ schema_version: 1, connection_id: selectedConnection.connection_id, expected_connection_revision: selectedConnection.revision, goal_id: selectedGoal.id, expected_goal_revision: selectedGoal.revision, label_ids: selectedLabelIds, expires_at: expiry, max_messages: limit, allowed_body_fields: BODY_FIELDS, acknowledge_source_read: true, idempotency_key: idempotencyKey("gmail-consent") }, signal));
      if (!isCurrentRequest(generation, requestOwnerScope) || !sameSelection(snapshot)) return;
      if (result.connection_id !== snapshot.connectionId || result.connection_revision !== snapshot.connectionRevision || result.goal_id !== snapshot.goalId || result.goal_revision !== snapshot.goalRevision || result.label_ids.length !== snapshot.labelIds.length || result.label_ids.some((id, index) => id !== snapshot.labelIds[index])) {
        throw new MailApiError(200, "consent_selection_mismatch", "The source consent receipt did not match the selected connection, Goal, or labels.", "refresh_mail_metadata");
      }
      setConsents((current) => [result, ...current.filter((item) => item.consent_id !== result.consent_id)]);
      setSourceAcknowledged(false);
      setConsentError(null);
    } catch (error) {
      if (isCurrentRequest(generation, requestOwnerScope) && sameSelection(snapshot) && !isAbort(error)) setConsentError(safeError(error));
    } finally {
      if (isCurrentRequest(generation, requestOwnerScope) && sameSelection(snapshot)) setConsentBusy(false);
    }
  };

  const scanSource = async () => {
    setScanError(null);
    const received = toIso(receivedAfter);
    const limit = Number(maxMessages);
    if (!selectedConnection || !selectedConsent || !activeConsent(selectedConsent)) return setScanError("Create or refresh an active source consent first.");
    if (!received || Date.parse(received) > Date.now() || Date.parse(received) < Date.now() - 7 * 24 * 60 * 60 * 1000) return setScanError("The metadata window must stay within the last seven days.");
    if (!selectedLabelIds.length || !selectedLabelIds.every((id) => selectedConsent.label_ids.includes(id))) return setScanError("Choose labels covered by the current source consent.");
    if (!Number.isInteger(limit) || limit < 1 || limit > selectedConsent.max_messages) return setScanError(`Choose between one and ${selectedConsent.max_messages} messages.`);
    const generation = generationRef.current;
    setScanBusy(true);
    try {
      const result = await bounded((signal) => scanMailMessages({ connection_id: selectedConnection.connection_id, expected_connection_revision: selectedConnection.revision, mail_consent_id: selectedConsent.consent_id, expected_source_consent_revision: selectedConsent.source_revision, label_ids: selectedLabelIds, received_after: received, max_messages: limit, request_uuid: idempotencyKey("gmail-scan") }, signal));
      if (!mountedRef.current || generation !== generationRef.current) return;
      setScan(result);
      setSelectedMessageId("");
      setPrivateMessage(null);
      setBodyReadAcknowledged(false);
    } catch (error) {
      if (mountedRef.current && !isAbort(error)) setScanError(safeError(error));
    } finally {
      if (mountedRef.current) setScanBusy(false);
    }
  };

  const readSelectedMessage = async () => {
    setReadError(null);
    if (!selectedConnection || !selectedConsent || !selectedMessage || !bodyReadAcknowledged) return setReadError("Select a current message and acknowledge its one-time bounded body read.");
    const generation = generationRef.current;
    setReadBusy(true);
    try {
      const result = await bounded((signal) => readMailMessage(selectedMessage.source_binding_id, { connection_id: selectedConnection.connection_id, expected_connection_revision: selectedConnection.revision, mail_consent_id: selectedConsent.consent_id, expected_source_consent_revision: selectedConsent.source_revision, message_binding_id: selectedMessage.source_binding_id, expected_message_revision: selectedMessage.message_revision, acknowledge_selected_body_read: true, request_uuid: idempotencyKey("gmail-body") }, signal));
      if (
        result.source_binding_id !== selectedMessage.source_binding_id
        || result.message_revision !== selectedMessage.message_revision
        || result.provenance.connection_id !== selectedConnection.connection_id
        || result.provenance.connection_revision !== selectedConnection.revision
        || result.provenance.consent_id !== selectedConsent.consent_id
        || result.provenance.source_consent_revision !== selectedConsent.source_revision
        || result.provenance.egress !== "local_only"
        || result.provenance.memory_status !== "no_learning"
      ) {
        throw new MailApiError(200, "mail_origin_mismatch", "The selected Mail readback did not match the current connection and consent.", "refresh_mail_context");
      }
      if (!mountedRef.current || generation !== generationRef.current) return;
      setPrivateMessage(result);
      setBodyReadAcknowledged(false);
    } catch (error) {
      if (mountedRef.current && !isAbort(error)) setReadError(safeError(error));
    } finally {
      if (mountedRef.current) setReadBusy(false);
    }
  };

  const changeModelConsent = async (allow: boolean) => {
    setModelError(null);
    if (!selectedConsent || !modelAcknowledged || selectedConsent.allowed_body_fields.join("|") !== BODY_FIELDS.join("|")) return setModelError("Acknowledge exactly subject, plainbody, and replyintent before changing model consent.");
    const generation = generationRef.current;
    const snapshot: MailSelectionSnapshot = { ...selectionRef.current, labelIds: [...selectionRef.current.labelIds] };
    const requestOwnerScope = snapshot.ownerScope;
    const consentSnapshot = selectedConsent;
    setModelBusy(true);
    try {
      const result = await bounded((signal) => setMailModelConsent(consentSnapshot.consent_id, { expected_revision: consentSnapshot.revision, acknowledged_payload_fields: [...consentSnapshot.allowed_body_fields], allow }, signal));
      if (!isCurrentRequest(generation, requestOwnerScope) || !sameSelection(snapshot)) return;
      if (result.consent_id !== snapshot.consentId || result.connection_id !== snapshot.connectionId || result.connection_revision !== snapshot.connectionRevision || result.goal_id !== snapshot.goalId || result.goal_revision !== snapshot.goalRevision || result.source_revision !== snapshot.sourceRevision || result.revision <= consentSnapshot.revision) {
        throw new MailApiError(200, "model_consent_selection_mismatch", "The model consent receipt did not match the selected connection, Goal, and consent revision.", "refresh_mail_metadata");
      }
      setConsents((current) => current.map((item) => item.consent_id === result.consent_id ? result : item));
      setModelAllowed(result.model_egress_allowed);
      setModelAcknowledged(false);
    } catch (error) {
      if (isCurrentRequest(generation, requestOwnerScope) && sameSelection(snapshot) && !isAbort(error)) setModelError(safeError(error));
    } finally {
      if (isCurrentRequest(generation, requestOwnerScope) && sameSelection(snapshot)) setModelBusy(false);
    }
  };

  const createWatch = async (event: FormEvent) => {
    event.preventDefault();
    setWatchError(null);
    const expiry = toIso(watchExpiry);
    if (pendingWatch) {
      await reconcilePendingWatch();
      return;
    }
    if (!selectedConnection || !selectedConsent || !selectedGoal) return setWatchError("Choose an active connection, consent, and Goal revision first.");
    if (!activeConsent(selectedConsent)) return setWatchError("The source consent is not active or has expired.");
    const limit = Number(maxMessages);
    if (!selectedLabelIds.length || !expiry || Date.parse(expiry) <= Date.now() || Date.parse(expiry) > Date.now() + 7 * 24 * 60 * 60 * 1000) return setWatchError("Choose current labels and a future expiry within seven days.");
    if (!Number.isInteger(limit) || limit < 1 || limit > selectedConsent.max_messages) return setWatchError(`Choose between one and ${selectedConsent.max_messages} messages.`);
    const key = idempotencyKey("gmail-watch");
    const pending = { version: 1 as const, idempotencyKey: key, action: "create" as const, watchId: null, expectedBindingRevision: null };
    const generation = generationRef.current;
    // Persist only the opaque replay key before dispatch.  The request body,
    // labels, Goal, message data, credentials, and intent remain transient.
    if (!writePendingWatch(ownerScope, pending)) {
      setWatchError("Browser recovery storage is unavailable. The watch mutation is blocked until its opaque request key can be retained.");
      return;
    }
    setPendingWatch(pending);
    setWatchBusy(true);
    try {
      const result = await bounded((signal) => createMailWatch({ schema_version: 1, connection_id: selectedConnection.connection_id, expected_connection_revision: selectedConnection.revision, mail_consent_id: selectedConsent.consent_id, expected_source_consent_revision: selectedConsent.source_revision, goal_id: selectedGoal.id, expected_goal_revision: selectedGoal.revision, label_ids: selectedLabelIds, cadence: { kind: watchCadence, timezone: watchTimezone.trim(), daily_hour: null, daily_minute: null }, expires_at: expiry, max_messages: limit, idempotency_key: key }, signal));
      if (mountedRef.current && generation === generationRef.current) {
        clearPendingWatch(ownerScope);
        setPendingWatch(null);
        setWatch(result);
        setWatches((current) => [result, ...current.filter((item) => item.watch_id !== result.watch_id)]);
        setWatchError(null);
      }
    } catch (error) {
      if (mountedRef.current && generation === generationRef.current && !isAbort(error)) {
        if (isUnknown(error)) {
          setWatchError("The watch outcome is unknown. The original opaque key is retained; reconcile the exact server watch before creating another.");
        } else {
          clearPendingWatch(ownerScope);
          setPendingWatch(null);
          setWatchError(safeError(error));
        }
      }
    } finally {
      if (mountedRef.current && generation === generationRef.current) setWatchBusy(false);
    }
  };

  const reconcilePendingWatch = async () => {
    if (!pendingWatch || !ownerScope) {
      setWatchError("There is no owner-scoped watch request to reconcile.");
      return;
    }
    const generation = generationRef.current;
    setWatchBusy(true);
    setWatchError(null);
    try {
      if (pendingWatch.action === "create") {
        const result = await bounded((signal) => getMailWatchRecovery(pendingWatch.idempotencyKey, signal));
        if (!mountedRef.current || generation !== generationRef.current) return;
        if (result.idempotency_key !== pendingWatch.idempotencyKey) throw new MailApiError(200, "watch_recovery_mismatch", "The watch recovery did not match the original request key.", "refresh_watch");
        if (result.status === "replayed" && result.watch) {
          clearPendingWatch(ownerScope);
          setPendingWatch(null);
          setWatch(result.watch);
          setWatches((current) => [result.watch as MailWatchMetadata, ...current.filter((item) => item.watch_id !== result.watch?.watch_id)]);
          setWatchError(null);
        } else {
          setWatchError("The original watch request is still unresolved. Its exact key is retained; no replacement watch was created.");
        }
      } else if (pendingWatch.watchId && pendingWatch.expectedBindingRevision !== null) {
        // A plain watch GET is only a current projection; it cannot prove that
        // this original control key committed. Explicitly retry the exact
        // idempotent intent, then verify its CAS receipt and readback.
        const expectedRevision = pendingWatch.expectedBindingRevision;
        const receipt = pendingWatch.action === "revoke"
          ? await bounded((signal) => revokeMailWatch(pendingWatch.watchId as string, { expected_binding_revision: expectedRevision, idempotency_key: pendingWatch.idempotencyKey, reason: "operator_requested" }, signal))
          : await bounded((signal) => controlMailWatch(pendingWatch.watchId as string, { action: pendingWatch.action as "pause" | "resume", expected_binding_revision: expectedRevision, idempotency_key: pendingWatch.idempotencyKey }, signal));
        const expectedState = pendingWatch.action === "pause" ? "paused" : pendingWatch.action === "resume" ? "active" : "revoked";
        if (receipt.binding_id !== pendingWatch.watchId || receipt.binding_revision !== expectedRevision + 1 || receipt.state !== expectedState) {
          throw new MailApiError(200, "watch_control_recovery_invalid", "The original watch control receipt could not be verified.", "refresh_watch");
        }
        const result = await bounded((signal) => getMailWatch(pendingWatch.watchId as string, signal));
        if (!mountedRef.current || generation !== generationRef.current) return;
        if (result.binding_revision === receipt.binding_revision && result.state === receipt.state) {
          clearPendingWatch(ownerScope);
          setPendingWatch(null);
          setWatch(result);
          setWatches((current) => current.map((item) => item.watch_id === result.watch_id ? result : item));
          setWatchError(null);
        } else {
          setWatchError("The exact watch readback has not reached the requested state. The original control key is retained; no replacement control was sent.");
        }
      }
    } catch {
      if (mountedRef.current && generation === generationRef.current) setWatchError("The original watch request could not be confirmed. Its exact key is retained; retry reconciliation explicitly.");
    } finally {
      if (mountedRef.current && generation === generationRef.current) setWatchBusy(false);
    }
  };

  const refreshWatch = async (watchId: string) => {
    const generation = generationRef.current;
    setWatchBusy(true);
    setWatchError(null);
    try {
      const result = await bounded((signal) => getMailWatch(watchId, signal));
      if (!mountedRef.current || generation !== generationRef.current) return;
      setWatches((current) => current.map((item) => item.watch_id === result.watch_id ? result : item));
      setWatch(result);
      setWatchListError(null);
      setWatchListStale(false);
    } catch (error) {
      if (mountedRef.current && generation === generationRef.current && !isAbort(error)) setWatchError(safeError(error));
    } finally {
      if (mountedRef.current && generation === generationRef.current) setWatchBusy(false);
    }
  };

  const controlWatch = async (item: MailWatchMetadata, action: "pause" | "resume" | "revoke") => {
    if (pendingWatch) {
      setWatchError("The previous watch request has no confirmed outcome. Reconcile its exact key before sending another control.");
      return;
    }
    const generation = generationRef.current;
    setWatchBusy(true);
    setWatchError(null);
    const expectedRevision = item.binding_revision;
    const key = idempotencyKey(`gmail-watch-${action}`);
    const pending = { version: 1 as const, idempotencyKey: key, action, watchId: item.watch_id, expectedBindingRevision: expectedRevision };
    if (!writePendingWatch(ownerScope, pending)) {
      setWatchError("Browser recovery storage is unavailable. The watch control is blocked until its opaque request key can be retained.");
      return;
    }
    setPendingWatch(pending);
    try {
      const receipt = action === "revoke"
        ? await bounded((signal) => revokeMailWatch(item.watch_id, { expected_binding_revision: expectedRevision, idempotency_key: key, reason: "operator_requested" }, signal))
        : await bounded((signal) => controlMailWatch(item.watch_id, { action, expected_binding_revision: expectedRevision, idempotency_key: key }, signal));
      const expectedState = action === "pause" ? "paused" : action === "resume" ? "active" : "revoked";
      if (receipt.binding_id !== item.watch_id || receipt.binding_revision !== expectedRevision + 1 || receipt.state !== expectedState) {
        throw new MailApiError(200, "watch_control_receipt_invalid", "The Mail watch control receipt could not be verified.", "refresh_watch");
      }
      // The control receipt proves the CAS transition; the exact watch GET
      // proves the redacted projection and occurrence state before rendering.
      const result = await bounded((signal) => getMailWatch(item.watch_id, signal));
      if (result.binding_revision !== receipt.binding_revision || result.state !== receipt.state) {
        throw new MailApiError(200, "watch_control_readback_invalid", "The Mail watch state changed without a matching control readback.", "refresh_watch");
      }
      if (!mountedRef.current || generation !== generationRef.current) return;
      clearPendingWatch(ownerScope);
      setPendingWatch(null);
      setWatches((current) => current.map((watchItem) => watchItem.watch_id === result.watch_id ? result : watchItem));
      setWatch(result);
      setWatchError(null);
    } catch (error) {
      if (mountedRef.current && generation === generationRef.current && !isAbort(error)) {
        if (!isUnknown(error)) {
          clearPendingWatch(ownerScope);
          setPendingWatch(null);
        }
        setWatchError(isUnknown(error) ? "The watch control outcome is unknown. The original control key is retained; reconcile the exact watch before attempting another control." : safeError(error));
      }
    } finally {
      if (mountedRef.current && generation === generationRef.current) setWatchBusy(false);
    }
  };

  const chooseConnection = (connectionId: string) => {
    generationRef.current += 1;
    setConsentBusy(false);
    setModelBusy(false);
    setSelectedConnectionId(connectionId);
    setSelectedLabelIds([]);
    setConsents((current) => current.filter((item) => item.connection_id === connectionId));
    clearPrivateState();
    setConsentError(null);
    setScanError(null);
  };

  return (
    <section className="px-1" aria-label="Mail connection and private source controls">
      <div className="text-[10px] uppercase tracking-wider text-retro-border font-bold mb-1">Gmail read-only Mail</div>
      <p className="text-[9px] text-retro-text/50 mb-3">Credentials are write-only setup material. Metadata reads stay local and provider-free until you explicitly verify, refresh labels, scan, or read one selected message.</p>

      <div className="rounded border border-retro-text/10 p-2 mb-3">
        <div className="text-[9px] uppercase tracking-wider text-retro-text/50 mb-2">Import connection</div>
        <form className="grid gap-2 sm:grid-cols-2" onSubmit={(event) => void submitConnection(event)}>
          <label className="text-[10px]">Label<input className="cockpit-input mt-1 w-full" maxLength={200} value={label} onChange={(event) => setLabel(event.currentTarget.value)} autoComplete="off" /></label>
          <label className="text-[10px]">Client ID<input className="cockpit-input mt-1 w-full" maxLength={4096} value={clientId} onChange={(event) => setClientId(event.currentTarget.value)} autoComplete="off" disabled={Boolean(pendingSetup)} /></label>
          <label className="text-[10px]">Client secret (optional)<input className="cockpit-input mt-1 w-full" type="password" maxLength={4096} value={clientSecret} onChange={(event) => setClientSecret(event.currentTarget.value)} autoComplete="new-password" disabled={Boolean(pendingSetup)} /></label>
          <label className="text-[10px]">Refresh token<input className="cockpit-input mt-1 w-full" type="password" maxLength={8192} required={!pendingSetup} value={refreshToken} onChange={(event) => setRefreshToken(event.currentTarget.value)} autoComplete="new-password" disabled={Boolean(pendingSetup)} /></label>
          <div className="sm:col-span-2 text-[9px] text-retro-text/45">Declared scope: <code>{GMAIL_SCOPE}</code></div>
          {pendingSetup && <div className="sm:col-span-2 rounded border border-amber-500/40 p-2 text-[10px]" role="status">Setup key <span className="font-mono">{pendingSetup.idempotencyKey}</span> has no confirmed outcome. Refresh metadata to reconcile it; credentials were cleared.</div>}
          {formError && <div className="sm:col-span-2 rounded border border-amber-500/40 p-2 text-[10px]" role="alert">{formError}</div>}
          <div className="sm:col-span-2 flex flex-wrap gap-2"><button type={pendingSetup ? "button" : "submit"} className="cockpit-feedback-button" onClick={pendingSetup ? () => void reconcileSetup() : undefined} disabled={submitting || !ownerScope}>{submitting ? "Saving…" : pendingSetup ? "Reconcile with metadata" : "Save connection"}</button><button type="button" className="cockpit-feedback-button" onClick={() => void loadMetadata()} disabled={loading}>{loading ? "Refreshing…" : "Refresh metadata"}</button></div>
        </form>
      </div>

      {connectionError && <div className="mb-2 rounded border border-amber-500/40 p-2 text-[10px]" role="alert">{connectionError}{metadataStale ? " Last confirmed values remain available." : ""}<button type="button" className="ml-2 underline" onClick={() => void loadMetadata()}>Retry</button></div>}
      <div className="text-[10px] uppercase tracking-wider text-retro-border font-bold mb-1">Saved connections</div>
      {connections.length === 0 ? <div className="cockpit-empty">{loading ? "Loading Mail metadata…" : "No Gmail read-only connection is configured."}</div> : <div className="grid gap-2">{connections.map((connection) => {
        const selected = connection.connection_id === selectedConnectionId;
        const busy = connectionBusy === connection.connection_id;
        return <article key={connection.connection_id} className={`rounded border p-2 text-[10px] ${selected ? "border-retro-highlight/50" : "border-retro-text/10"}`}>
          <div className="flex flex-wrap items-center justify-between gap-2"><strong>{connection.label}</strong><span className="uppercase tracking-wider">{connection.state} · revision {connection.revision}</span></div>
          <div className="mt-1 text-retro-text/50">Scope evidence: {connection.scope_status} · {connection.provider_scopes_verified ? "observed" : "unverified"} · metadata only</div>
          <div className="mt-2 flex flex-wrap gap-2"><button type="button" className="cockpit-feedback-button" onClick={() => chooseConnection(connection.connection_id)} aria-pressed={selected}>{selected ? "Selected" : "Use connection"}</button><button type="button" className="cockpit-feedback-button" onClick={() => void verify(connection)} disabled={busy || connection.state !== "active"}>{busy ? "Checking…" : "Verify (provider read)"}</button><button type="button" className="cockpit-feedback-button" onClick={() => void refreshLabelsFor(connection)} disabled={busy || connection.state !== "active"}>{busy ? "Refreshing…" : "Refresh labels (provider read)"}</button></div>
          {labelStatus[connection.connection_id] && <div className="mt-2 text-retro-text/60" role="status">{labelStatus[connection.connection_id]}</div>}
        </article>;
      })}</div>}

      {selectedConnection && <>
        <div className="mt-3 rounded border border-retro-text/10 p-2">
          <div className="text-[10px] uppercase tracking-wider text-retro-border font-bold mb-1">Cached account labels</div>
          <div className="text-[9px] text-retro-text/45 mb-2">Selecting labels is local. A refresh button above is the only path that contacts the provider.</div>
          {selectedLabels.length === 0 ? <div className="text-[10px] text-retro-text/45">No current labels. Use explicit Refresh labels.</div> : <div className="grid gap-1 sm:grid-cols-2">{selectedLabels.filter((item) => item.state === "active").map((item) => <label key={item.label_id} className="flex items-center gap-2 text-[10px]"><input type="checkbox" checked={selectedLabelIds.includes(item.label_id)} onChange={(event) => { const checked = event.currentTarget.checked; generationRef.current += 1; setConsentBusy(false); setModelBusy(false); setSelectedLabelIds((current) => checked ? [...current, item.label_id].slice(0, 3) : current.filter((id) => id !== item.label_id)); }} />{item.name}<span className="opacity-50">{item.type}</span></label>)}</div>}
        </div>

        <form className="mt-3 rounded border border-retro-text/10 p-2" onSubmit={(event) => void createConsentForSource(event)}>
          <div className="text-[10px] uppercase tracking-wider text-retro-border font-bold mb-1">Finite source consent</div>
          <div className="grid gap-2 sm:grid-cols-2">
            <label className="text-[10px]">Goal<select className="cockpit-input mt-1 w-full" value={selectedGoalId} onChange={(event) => { generationRef.current += 1; setConsentBusy(false); setModelBusy(false); setSelectedGoalId(event.currentTarget.value); clearPrivateState(); }}><option value="">Choose an active Goal</option>{goals.map((goal) => <option key={goal.id} value={goal.id}>{goal.title} · revision {goal.revision}</option>)}</select></label>
            <label className="text-[10px]">Expires<input className="cockpit-input mt-1 w-full" type="datetime-local" value={consentExpiry} onChange={(event) => setConsentExpiry(event.currentTarget.value)} /></label>
            <label className="text-[10px]">Maximum messages<input className="cockpit-input mt-1 w-full" type="number" min={1} max={10} value={maxMessages} onChange={(event) => setMaxMessages(event.currentTarget.value)} /></label>
            <div className="text-[10px]">Model egress<div className="mt-1 text-retro-text/50">Off until a separate exact-field acknowledgement below.</div></div>
          </div>
          {goalError && <div className="mt-2 text-amber-300" role="alert">{goalError}</div>}
          {consentError && <div className="mt-2 text-amber-300" role="alert">{consentError}</div>}
          <label className="mt-2 flex items-start gap-2 text-[10px]"><input type="checkbox" checked={sourceAcknowledged} onChange={(event) => setSourceAcknowledged(event.currentTarget.checked)} />I acknowledge one bounded metadata/body read within the selected labels, Goal revision, expiry, and message limit. This does not grant model egress.</label>
          <button type="submit" className="cockpit-feedback-button mt-2" disabled={consentBusy}>{consentBusy ? "Saving consent…" : "Create source consent"}</button>
        </form>

        <div className="mt-3 rounded border border-retro-text/10 p-2">
          <div className="text-[10px] uppercase tracking-wider text-retro-border font-bold mb-1">Current consents</div>
          {consents.filter((item) => item.connection_id === selectedConnection.connection_id).length === 0 ? <div className="text-[10px] text-retro-text/45">No consent metadata loaded.</div> : <div className="grid gap-2">{consents.filter((item) => item.connection_id === selectedConnection.connection_id).map((item) => <article key={item.consent_id} className={`rounded border p-2 ${item.consent_id === selectedConsent?.consent_id ? "border-retro-highlight/40" : "border-retro-text/10"}`}><div className="flex flex-wrap justify-between gap-2"><span className="font-mono">{item.consent_id}</span><span>{item.state} · source rev {item.source_revision} · model rev {item.model_revision}</span></div><div className="mt-1 text-retro-text/50">Goal {item.goal_id} rev {item.goal_revision} · labels {item.label_ids.length} · expires {new Date(item.expires_at).toLocaleString()}</div></article>)}</div>}
          {selectedConsent && <div className="mt-2 rounded border border-cyan-500/30 p-2"><div className="text-[10px]">Separate model consent</div><div className="mt-1 text-[9px] text-retro-text/50">Exact ordered fields: {selectedConsent.allowed_body_fields.join(", ")}. Source reading works without this grant.</div><label className="mt-2 flex items-start gap-2 text-[10px]"><input type="checkbox" checked={modelAcknowledged} onChange={(event) => setModelAcknowledged(event.currentTarget.checked)} />I acknowledge exactly these text fields may be sent through the governed model route for a local draft.</label>{modelError && <div className="mt-2 text-amber-300" role="alert">{modelError}</div>}<div className="mt-2 flex flex-wrap gap-2"><button type="button" className="cockpit-feedback-button" disabled={modelBusy || !modelAcknowledged} onClick={() => void changeModelConsent(true)}>{modelBusy ? "Saving…" : modelAllowed || selectedConsent.model_egress_allowed ? "Model consent enabled" : "Allow model for draft"}</button>{(modelAllowed || selectedConsent.model_egress_allowed) && <button type="button" className="cockpit-feedback-button" disabled={modelBusy || !modelAcknowledged} onClick={() => void changeModelConsent(false)}>Revoke model consent</button>}</div></div>}
        </div>

        <div className="mt-3 rounded border border-retro-text/10 p-2">
          <div className="text-[10px] uppercase tracking-wider text-retro-border font-bold mb-1">Bounded metadata scan</div>
          <div className="grid gap-2 sm:grid-cols-2"><label className="text-[10px]">Received after<input className="cockpit-input mt-1 w-full" type="datetime-local" value={receivedAfter} onChange={(event) => setReceivedAfter(event.currentTarget.value)} /></label><label className="text-[10px]">Messages<input className="cockpit-input mt-1 w-full" type="number" min={1} max={10} value={maxMessages} onChange={(event) => setMaxMessages(event.currentTarget.value)} /></label></div>
          {scanError && <div className="mt-2 text-amber-300" role="alert">{scanError}</div>}
          <button type="button" className="cockpit-feedback-button mt-2" disabled={scanBusy || !activeConsent(selectedConsent)} onClick={() => void scanSource()}>{scanBusy ? "Scanning one bounded page…" : "Scan metadata (provider read)"}</button>
          {scan && <div className="mt-2 rounded border border-retro-text/10 p-2"><div>{scan.messages.length} metadata records · {scan.coverage.more_available ? "more available; no pagination was followed" : "single page complete"}</div><div className="mt-2 grid gap-1">{scan.messages.map((message) => <button key={message.source_binding_id} type="button" className={`rounded border p-2 text-left ${selectedMessageId === message.source_binding_id ? "border-retro-highlight/50" : "border-retro-text/10"}`} onClick={() => { setSelectedMessageId(message.source_binding_id); setPrivateMessage(null); setBodyReadAcknowledged(false); }}><div className="font-semibold">{message.subject}</div><div className="text-retro-text/60">{message.preview}</div><div className="text-[9px] opacity-50">{message.message_revision} · {message.read_status}</div></button>)}</div></div>}
          {selectedMessage && <div className="mt-2 rounded border border-amber-500/30 p-2"><div className="text-[10px]">Selected message stays metadata-only until acknowledgement.</div><label className="mt-2 flex items-start gap-2 text-[10px]"><input type="checkbox" checked={bodyReadAcknowledged} onChange={(event) => setBodyReadAcknowledged(event.currentTarget.checked)} />I acknowledge one bounded full read of this selected message within the current source consent.</label>{readError && <div className="mt-2 text-amber-300" role="alert">{readError}</div>}<button type="button" className="cockpit-feedback-button mt-2" disabled={readBusy || !bodyReadAcknowledged} onClick={() => void readSelectedMessage()}>{readBusy ? "Reading one message…" : "Read selected message"}</button></div>}
          {privateMessage && <article className="mt-2 rounded border border-emerald-500/30 p-2" aria-label="Private Mail message"><div className="font-semibold">{privateMessage.subject}</div><div className="mt-2 whitespace-pre-wrap break-words">{privateMessage.plain_text}</div>{privateMessage.truncated && <div className="mt-2 text-amber-300">The bounded body was truncated by the server.</div>}<div className="mt-2 text-[9px] text-retro-text/50">Local-only read · no learning · attachments and links were not fetched.</div></article>}
        </div>

        <section className="mt-3 rounded border border-retro-text/10 p-2" aria-label="Saved Mail watches">
          <div className="flex flex-wrap items-center justify-between gap-2"><div><div className="text-[10px] uppercase tracking-wider text-retro-border font-bold">Saved metadata watches</div><div className="text-[9px] text-retro-text/50">Goal-governed metadata only. A baseline creates no notice; later periods allow at most three neutral notices and never create a task automatically.</div></div><button type="button" className="cockpit-feedback-button" onClick={() => void loadWatches()} disabled={watchBusy}>{watchBusy ? "Refreshing…" : "Refresh watches"}</button></div>
          {watchListError && <div className="mt-2 rounded border border-amber-500/40 p-2 text-[10px]" role="alert">{watchListError}{watchListStale ? " Last confirmed watch states remain visible." : ""}</div>}
          {pendingWatch && <div className="mt-2 rounded border border-amber-500/40 p-2 text-[10px]" role="status">A watch {pendingWatch.action} request has an unconfirmed outcome. The original opaque request key is retained in this operator session; reconcile the exact server watch before another control.<button type="button" className="cockpit-feedback-button mt-2" onClick={() => void reconcilePendingWatch()} disabled={watchBusy}>{watchBusy ? "Reconciling…" : "Reconcile original watch request"}</button></div>}
          {watches.length === 0 ? <div className="mt-2 text-[10px] text-retro-text/45">{watchListStale ? "Saved watch state is unavailable." : "No metadata watches have been created."}</div> : <div className="mt-2 grid gap-2">{watches.map((item) => {
            const canPause = item.state === "active";
            const canResume = item.state === "paused";
            const canRevoke = canPause || canResume;
            const latest = item.latest_occurrence;
            return <article key={item.watch_id} className="rounded border border-retro-text/10 p-2 text-[10px]">
              <div className="flex flex-wrap items-center justify-between gap-2"><strong>Watch {item.watch_id}</strong><span className="uppercase tracking-wider">{item.state} · binding revision {item.binding_revision}</span></div>
              <div className="mt-1 text-retro-text/60">{item.cadence.kind} · {item.cadence.timezone} · Goal {item.goal_id} revision {item.goal_revision} · expires {new Date(item.expires_at).toLocaleString()}</div>
              <div className="mt-1">Baseline: {item.baseline_complete ? "complete" : "pending"} · coverage: {item.watch_state}{item.skipped_coverage_reason ? ` · ${item.skipped_coverage_reason}` : ""}</div>
              {latest && <div className="mt-1 text-retro-text/60">Latest occurrence: {latest.state}{latest.failure_code ? ` · ${latest.failure_code}` : ""}{latest.recovery_action ? ` · ${latest.recovery_action}` : ""}</div>}
              <div className="mt-2 flex flex-wrap gap-2"><button type="button" className="cockpit-feedback-button" onClick={() => void refreshWatch(item.watch_id)} disabled={watchBusy}>Refresh exact watch</button>{canPause && <button type="button" className="cockpit-feedback-button" onClick={() => void controlWatch(item, "pause")} disabled={watchBusy}>Pause watch</button>}{canResume && <button type="button" className="cockpit-feedback-button" onClick={() => void controlWatch(item, "resume")} disabled={watchBusy}>Resume watch</button>}{canRevoke && <button type="button" className="cockpit-feedback-button" onClick={() => void controlWatch(item, "revoke")} disabled={watchBusy}>Revoke watch</button>}</div>
            </article>;
          })}</div>}
          {watchError && <div className="mt-2 text-amber-300" role="alert">{watchError}</div>}
        </section>

        <form className="mt-3 rounded border border-retro-text/10 p-2" onSubmit={(event) => void createWatch(event)}>
          <div className="text-[10px] uppercase tracking-wider text-retro-border font-bold mb-1">Finite metadata watch</div>
          <div className="grid gap-2 sm:grid-cols-2"><label className="text-[10px]">Cadence<select className="cockpit-input mt-1 w-full" value={watchCadence} onChange={(event) => setWatchCadence(event.currentTarget.value as "hourly" | "6h")}><option value="hourly">Hourly</option><option value="6h">Every 6 hours</option></select></label><label className="text-[10px]">Timezone<input className="cockpit-input mt-1 w-full" value={watchTimezone} maxLength={64} onChange={(event) => setWatchTimezone(event.currentTarget.value)} /></label><label className="text-[10px]">Expires<input className="cockpit-input mt-1 w-full" type="datetime-local" value={watchExpiry} onChange={(event) => setWatchExpiry(event.currentTarget.value)} /></label></div>
          <div className="mt-2 text-[9px] text-retro-text/50">The first scan establishes a baseline. Later notices are bounded and neutral; no body read or model call occurs per arrival. Quiet hours and Goal budget are enforced by the server.</div>
          <button type="submit" className="cockpit-feedback-button mt-2" disabled={watchBusy || Boolean(pendingWatch) || !activeConsent(selectedConsent) || !selectedGoal}>{watchBusy ? "Creating watch…" : pendingWatch ? "Reconcile existing watch" : "Create metadata watch"}</button>
          {watch && <div className="mt-2 rounded border border-emerald-500/30 p-2" role="status"><div>Watch {watch.watch_id} · {watch.state} · binding revision {watch.binding_revision}</div><div>State {watch.watch_state} · {watch.baseline_complete ? "baseline complete" : "baseline pending"} · expires {new Date(watch.expires_at).toLocaleString()}</div><div className="mt-1 text-[9px] text-retro-text/50">Pause, resume, and revoke remain governed schedule actions; this view never restarts an unknown occurrence automatically.</div></div>}
        </form>
      </>}
    </section>
  );
}
