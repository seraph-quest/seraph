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
  createMailConnection,
  createMailConsent,
  createMailWatch,
  listMailConnections,
  listMailConsents,
  listMailLabels,
  makeMailIdempotencyKey,
  readMailMessage,
  refreshMailLabels,
  setMailModelConsent,
  scanMailMessages,
  verifyMailConnection,
} from "../../lib/mailApi";
import type { GoalInfo } from "../../types";

const GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly" as const;
const BODY_FIELDS: MailBodyField[] = ["subject", "plainbody", "replyintent"];
const REQUEST_TIMEOUT_MS = 15_000;

export interface MailConnectionPanelProps {
  ownerPrincipalId?: string | null;
  ownerSessionId?: string | null;
}

interface PendingSetup {
  idempotencyKey: string;
  label: string;
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
  return error instanceof MailApiError && (error.status === 0 || error.status >= 500);
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

async function bounded<T>(operation: (signal: AbortSignal) => Promise<T>, parentSignal?: AbortSignal): Promise<T> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (parentSignal?.aborted) controller.abort();
  else parentSignal?.addEventListener("abort", abort, { once: true });
  const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    return await operation(controller.signal);
  } finally {
    window.clearTimeout(timeout);
    parentSignal?.removeEventListener("abort", abort);
  }
}

function activeConsent(consent: MailConsentMetadata | null): boolean {
  return Boolean(consent && consent.state === "active" && consent.source_read_allowed && Date.parse(consent.expires_at) > Date.now());
}

export function MailConnectionPanel({ ownerPrincipalId, ownerSessionId }: MailConnectionPanelProps) {
  const ownerScope = ownerPrincipalId && ownerSessionId ? `${ownerPrincipalId}\u0000${ownerSessionId}` : null;
  const mountedRef = useRef(true);
  const generationRef = useRef(0);
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
  const [watchCadence, setWatchCadence] = useState<"hourly" | "6h">("hourly");
  const [watchTimezone, setWatchTimezone] = useState(() => Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC");
  const [watchExpiry, setWatchExpiry] = useState(() => localInput(new Date(Date.now() + 24 * 60 * 60 * 1000)));

  const selectedConnection = useMemo(() => connections.find((item) => item.connection_id === selectedConnectionId) ?? null, [connections, selectedConnectionId]);
  const selectedGoal = useMemo(() => goals.find((goal) => goal.id === selectedGoalId) ?? null, [goals, selectedGoalId]);
  const selectedLabels = useMemo(() => labels[selectedConnectionId] ?? [], [labels, selectedConnectionId]);
  const selectedConsent = useMemo(() => consents.find((item) => item.connection_id === selectedConnectionId && item.goal_id === selectedGoalId && item.state === "active") ?? null, [consents, selectedConnectionId, selectedGoalId]);
  const selectedMessage = useMemo(() => scan?.messages.find((item) => item.source_binding_id === selectedMessageId) ?? null, [scan, selectedMessageId]);

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

  const loadGoals = useCallback(async (generation: number, signal: AbortSignal) => {
    try {
      const response = await bounded((innerSignal) => apiFetch(`${API_URL}/api/goals/tree`, { method: "GET", signal: innerSignal }), signal);
      const payload = await response.json().catch(() => null);
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

  const submitConnection = async (event: FormEvent) => {
    event.preventDefault();
    setFormError(null);
    if (!ownerScope) return setFormError("Sign in through the operator session before importing Gmail credentials.");
    const normalizedLabel = label.trim();
    if (!normalizedLabel || !clientId.trim() || !refreshToken.trim()) return setFormError("Label, client ID, and refresh token are required.");
    const key = pendingSetup?.idempotencyKey ?? idempotencyKey("gmail-setup");
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
      if (!mountedRef.current) return;
      setConnections((current) => [result, ...current.filter((item) => item.connection_id !== result.connection_id)]);
      setSelectedConnectionId(result.connection_id);
      setPendingSetup(null);
      setLabel("");
      setFormError(null);
    } catch (error) {
      if (!mountedRef.current || isAbort(error)) return;
      if (isUnknown(error)) {
        setPendingSetup({ idempotencyKey: key, label: normalizedLabel });
        setFormError("The setup outcome is unknown. Credentials were cleared; refresh Mail metadata to reconcile the exact setup key before trying another import.");
      } else {
        setFormError(safeError(error));
      }
    } finally {
      if (mountedRef.current) setSubmitting(false);
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
    setConsentBusy(true);
    try {
      const result = await bounded((signal) => createMailConsent({ schema_version: 1, connection_id: selectedConnection.connection_id, expected_connection_revision: selectedConnection.revision, goal_id: selectedGoal.id, expected_goal_revision: selectedGoal.revision, label_ids: selectedLabelIds, expires_at: expiry, max_messages: limit, allowed_body_fields: BODY_FIELDS, acknowledge_source_read: true, idempotency_key: idempotencyKey("gmail-consent") }, signal));
      if (!mountedRef.current) return;
      setConsents((current) => [result, ...current.filter((item) => item.consent_id !== result.consent_id)]);
      setSourceAcknowledged(false);
      setConsentError(null);
    } catch (error) {
      if (mountedRef.current && !isAbort(error)) setConsentError(safeError(error));
    } finally {
      if (mountedRef.current) setConsentBusy(false);
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
    setModelBusy(true);
    try {
      const result = await bounded((signal) => setMailModelConsent(selectedConsent.consent_id, { expected_revision: selectedConsent.revision, acknowledged_payload_fields: [...selectedConsent.allowed_body_fields], allow }, signal));
      if (!mountedRef.current) return;
      setConsents((current) => current.map((item) => item.consent_id === result.consent_id ? result : item));
      setModelAllowed(result.model_egress_allowed);
      setModelAcknowledged(false);
    } catch (error) {
      if (mountedRef.current && !isAbort(error)) setModelError(safeError(error));
    } finally {
      if (mountedRef.current) setModelBusy(false);
    }
  };

  const createWatch = async (event: FormEvent) => {
    event.preventDefault();
    setWatchError(null);
    const expiry = toIso(watchExpiry);
    if (!selectedConnection || !selectedConsent || !selectedGoal) return setWatchError("Choose an active connection, consent, and Goal revision first.");
    if (!activeConsent(selectedConsent)) return setWatchError("The source consent is not active or has expired.");
    if (!selectedLabelIds.length || !expiry || Date.parse(expiry) <= Date.now() || Date.parse(expiry) > Date.now() + 7 * 24 * 60 * 60 * 1000) return setWatchError("Choose current labels and a future expiry within seven days.");
    setWatchBusy(true);
    try {
      const result = await bounded((signal) => createMailWatch({ schema_version: 1, connection_id: selectedConnection.connection_id, expected_connection_revision: selectedConnection.revision, mail_consent_id: selectedConsent.consent_id, expected_source_consent_revision: selectedConsent.source_revision, goal_id: selectedGoal.id, expected_goal_revision: selectedGoal.revision, label_ids: selectedLabelIds, cadence: watchCadence, timezone: watchTimezone.trim(), expires_at: expiry, max_messages: Number(maxMessages), idempotency_key: idempotencyKey("gmail-watch") }, signal));
      if (mountedRef.current) setWatch(result);
    } catch (error) {
      if (mountedRef.current && !isAbort(error)) setWatchError(isUnknown(error) ? "The watch outcome is unknown. Keep the exact key and reconcile explicitly; the UI will not create a replacement watch." : safeError(error));
    } finally {
      if (mountedRef.current) setWatchBusy(false);
    }
  };

  const chooseConnection = (connectionId: string) => {
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
          <label className="text-[10px]">Client ID<input className="cockpit-input mt-1 w-full" maxLength={4096} value={clientId} onChange={(event) => setClientId(event.currentTarget.value)} autoComplete="off" /></label>
          <label className="text-[10px]">Client secret (optional)<input className="cockpit-input mt-1 w-full" type="password" maxLength={4096} value={clientSecret} onChange={(event) => setClientSecret(event.currentTarget.value)} autoComplete="new-password" /></label>
          <label className="text-[10px]">Refresh token<input className="cockpit-input mt-1 w-full" type="password" maxLength={8192} required value={refreshToken} onChange={(event) => setRefreshToken(event.currentTarget.value)} autoComplete="new-password" /></label>
          <div className="sm:col-span-2 text-[9px] text-retro-text/45">Declared scope: <code>{GMAIL_SCOPE}</code></div>
          {pendingSetup && <div className="sm:col-span-2 rounded border border-amber-500/40 p-2 text-[10px]" role="status">Setup key <span className="font-mono">{pendingSetup.idempotencyKey}</span> has no confirmed outcome. Refresh metadata to reconcile it; credentials were cleared.</div>}
          {formError && <div className="sm:col-span-2 rounded border border-amber-500/40 p-2 text-[10px]" role="alert">{formError}</div>}
          <div className="sm:col-span-2 flex flex-wrap gap-2"><button type="submit" className="cockpit-feedback-button" disabled={submitting || !ownerScope}>{submitting ? "Saving…" : pendingSetup ? "Reconcile with metadata" : "Save connection"}</button><button type="button" className="cockpit-feedback-button" onClick={() => void loadMetadata()} disabled={loading}>{loading ? "Refreshing…" : "Refresh metadata"}</button></div>
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
          {selectedLabels.length === 0 ? <div className="text-[10px] text-retro-text/45">No current labels. Use explicit Refresh labels.</div> : <div className="grid gap-1 sm:grid-cols-2">{selectedLabels.filter((item) => item.state === "active").map((item) => <label key={item.label_id} className="flex items-center gap-2 text-[10px]"><input type="checkbox" checked={selectedLabelIds.includes(item.label_id)} onChange={(event) => { const checked = event.currentTarget.checked; setSelectedLabelIds((current) => checked ? [...current, item.label_id].slice(0, 3) : current.filter((id) => id !== item.label_id)); }} />{item.name}<span className="opacity-50">{item.type}</span></label>)}</div>}
        </div>

        <form className="mt-3 rounded border border-retro-text/10 p-2" onSubmit={(event) => void createConsentForSource(event)}>
          <div className="text-[10px] uppercase tracking-wider text-retro-border font-bold mb-1">Finite source consent</div>
          <div className="grid gap-2 sm:grid-cols-2">
            <label className="text-[10px]">Goal<select className="cockpit-input mt-1 w-full" value={selectedGoalId} onChange={(event) => { setSelectedGoalId(event.currentTarget.value); clearPrivateState(); }}><option value="">Choose an active Goal</option>{goals.map((goal) => <option key={goal.id} value={goal.id}>{goal.title} · revision {goal.revision}</option>)}</select></label>
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

        <form className="mt-3 rounded border border-retro-text/10 p-2" onSubmit={(event) => void createWatch(event)}>
          <div className="text-[10px] uppercase tracking-wider text-retro-border font-bold mb-1">Finite metadata watch</div>
          <div className="grid gap-2 sm:grid-cols-2"><label className="text-[10px]">Cadence<select className="cockpit-input mt-1 w-full" value={watchCadence} onChange={(event) => setWatchCadence(event.currentTarget.value as "hourly" | "6h")}><option value="hourly">Hourly</option><option value="6h">Every 6 hours</option></select></label><label className="text-[10px]">Timezone<input className="cockpit-input mt-1 w-full" value={watchTimezone} maxLength={64} onChange={(event) => setWatchTimezone(event.currentTarget.value)} /></label><label className="text-[10px]">Expires<input className="cockpit-input mt-1 w-full" type="datetime-local" value={watchExpiry} onChange={(event) => setWatchExpiry(event.currentTarget.value)} /></label></div>
          <div className="mt-2 text-[9px] text-retro-text/50">The first scan establishes a baseline. Later notices are bounded and neutral; no body read or model call occurs per arrival. Quiet hours and Goal budget are enforced by the server.</div>
          {watchError && <div className="mt-2 text-amber-300" role="alert">{watchError}</div>}
          <button type="submit" className="cockpit-feedback-button mt-2" disabled={watchBusy || !activeConsent(selectedConsent) || !selectedGoal}>{watchBusy ? "Creating watch…" : "Create metadata watch"}</button>
          {watch && <div className="mt-2 rounded border border-emerald-500/30 p-2" role="status"><div>Watch {watch.watch_id} · {watch.state} · binding revision {watch.binding_revision}</div><div>State {watch.watch_state} · {watch.baseline_complete ? "baseline complete" : "baseline pending"} · expires {new Date(watch.expires_at).toLocaleString()}</div><div className="mt-1 text-[9px] text-retro-text/50">Pause, resume, and revoke remain governed schedule actions; this view never restarts an unknown occurrence automatically.</div></div>}
        </form>
      </>}
    </section>
  );
}
