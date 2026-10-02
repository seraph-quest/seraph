import { useCallback, useEffect, useRef, useState } from "react";

import {
  CalendarApiError,
  createCalendarConnection,
  listCalendarConnections,
  listGovernedSchedules,
  controlGovernedSchedule,
  revokeGovernedSchedule,
  listCalendars,
  revokeCalendarConnection,
  verifyCalendarConnection,
} from "../../lib/calendar";
import type {
  CalendarConnectionMetadata,
  CalendarOption,
  CalendarVerifyResponse,
  CreateCalendarConnectionRequest,
  GovernedScheduleBinding,
} from "../../types";

const SERVICE = "calendar_readonly" as const;
const REQUEST_TIMEOUT_MS = 15_000;
const STALE_RECONCILIATION_CODES = new Set([
  "calendar_connection_revision_stale",
  "calendar_revision_stale",
  "calendar_event_revision_stale",
  "calendar_schedule_revision_stale",
  "calendar_schedule_binding_stale",
]);

type PendingScheduleMutation =
  | {
      action: "pause" | "resume";
      request: {
        action: "pause" | "resume";
        expected_binding_revision: number;
        idempotency_key: string;
      };
    }
  | {
      action: "revoke";
      request: {
        expected_binding_revision: number;
        idempotency_key: string;
        reason: string;
      };
    };

function idempotencyKey(prefix: string): string {
  try {
    return `${prefix}:${crypto.randomUUID()}`;
  } catch {
    return `${prefix}:${Date.now().toString(36)}:${Math.random().toString(36).slice(2)}`;
  }
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

class CalendarRequestTimeoutError extends Error {
  constructor() {
    super("The calendar request timed out without a receipt.");
    this.name = "CalendarRequestTimeoutError";
  }
}

function isDefinitiveClientError(error: unknown): error is CalendarApiError {
  return error instanceof CalendarApiError
    && error.status >= 400
    && error.status < 500
    && ![408, 409, 429].includes(error.status);
}

async function boundedCalendarRequest<T>(
  operation: (signal: AbortSignal) => Promise<T>,
  parentSignal?: AbortSignal,
): Promise<T> {
  const controller = new AbortController();
  let timedOut = false;
  const abortFromParent = () => controller.abort();
  if (parentSignal?.aborted) controller.abort();
  else parentSignal?.addEventListener("abort", abortFromParent, { once: true });
  const timeout = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, REQUEST_TIMEOUT_MS);
  try {
    try {
      return await operation(controller.signal);
    } catch (error) {
      if (timedOut) throw new CalendarRequestTimeoutError();
      throw error;
    }
  } finally {
    window.clearTimeout(timeout);
    parentSignal?.removeEventListener("abort", abortFromParent);
  }
}

function safeError(error: unknown): string {
  if (error instanceof CalendarApiError) {
    if (error.status === 409) return "The request needs reconciliation. Refresh metadata, then retry the exact request. A new attempt is available only after the server confirms a stale revision.";
    if (error.status === 401) return "The operator session is unavailable. Sign in again before retrying.";
    if (error.status >= 400 && error.status < 500) return `${error.message}${error.recovery ? ` ${error.recovery}` : " Correct the setup fields and submit a new attempt."}`;
    return "The outcome is unconfirmed. Keep the exact request open and retry it only when ready.";
  }
  return "The outcome is unconfirmed. Keep the form open and retry the exact request.";
}

function connectionStateLabel(connection: CalendarConnectionMetadata): string {
  return connection.state.replace(/_/g, " ");
}

export interface CalendarConnectionPanelProps {
  service?: typeof SERVICE;
  ownerPrincipalId?: string | null;
  ownerSessionId?: string | null;
}

type ConnectionControlRequest = {
  expected_revision: number;
  idempotency_key: string;
};

type CalendarControlPending = Record<string, { action: "verify" | "revoke"; request: ConnectionControlRequest }>;

interface CalendarControlRecovery {
  controls: CalendarControlPending;
  schedules: Record<string, PendingScheduleMutation>;
}

const MAX_CALENDAR_CONTROL_SCOPES = 32;
const MAX_CALENDAR_CONTROL_INTENTS = 32;
const pendingCalendarControls = new Map<string, CalendarControlRecovery>();

function calendarControlScope(ownerPrincipalId?: string | null, ownerSessionId?: string | null): string | null {
  if (!ownerPrincipalId || !ownerSessionId) return null;
  return `${ownerPrincipalId}\u0000${ownerSessionId}`;
}

function rememberCalendarControls(scope: string | null, recovery: CalendarControlRecovery): void {
  if (!scope) return;
  const entries = [
    ...Object.entries(recovery.controls).map(([key, value]) => ["control", key, value] as const),
    ...Object.entries(recovery.schedules).map(([key, value]) => ["schedule", key, value] as const),
  ].slice(-MAX_CALENDAR_CONTROL_INTENTS);
  const controls: CalendarControlPending = {};
  const schedules: Record<string, PendingScheduleMutation> = {};
  entries.forEach(([kind, key, value]) => {
    if (kind === "control") controls[key] = value as CalendarControlPending[string];
    else schedules[key] = value as PendingScheduleMutation;
  });
  pendingCalendarControls.delete(scope);
  pendingCalendarControls.set(scope, { controls, schedules });
  while (pendingCalendarControls.size > MAX_CALENDAR_CONTROL_SCOPES) {
    const oldest = pendingCalendarControls.keys().next().value;
    if (typeof oldest !== "string") break;
    pendingCalendarControls.delete(oldest);
  }
}

export function clearCalendarControlRecovery(ownerPrincipalId?: string | null, ownerSessionId?: string | null): void {
  const scope = calendarControlScope(ownerPrincipalId, ownerSessionId);
  if (scope) pendingCalendarControls.delete(scope);
}

export function CalendarConnectionPanel({ service = SERVICE, ownerPrincipalId, ownerSessionId }: CalendarConnectionPanelProps) {
  const mountedRef = useRef(true);
  const controlScope = calendarControlScope(ownerPrincipalId, ownerSessionId);
  const recoveryAtMount = controlScope ? pendingCalendarControls.get(controlScope) : null;
  const [connections, setConnections] = useState<CalendarConnectionMetadata[]>([]);
  const [loading, setLoading] = useState(true);
  const [metadataError, setMetadataError] = useState<string | null>(null);
  const [label, setLabel] = useState("");
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [refreshToken, setRefreshToken] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [authBlocked, setAuthBlocked] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [pending, setPending] = useState<{ request: CreateCalendarConnectionRequest; timedOut?: boolean } | null>(null);
  const [verification, setVerification] = useState<Record<string, CalendarVerifyResponse | null>>({});
  const [verifyBusy, setVerifyBusy] = useState<string | null>(null);
  const [verifyError, setVerifyError] = useState<Record<string, string>>({});
  const [verifyErrorCode, setVerifyErrorCode] = useState<Record<string, string | null>>({});
  const [revokeBusy, setRevokeBusy] = useState<string | null>(null);
  const [revokeError, setRevokeError] = useState<Record<string, string>>({});
  const [revokeErrorCode, setRevokeErrorCode] = useState<Record<string, string | null>>({});
  const [controlPending, setControlPending] = useState<CalendarControlPending>(() => ({ ...(recoveryAtMount?.controls ?? {}) }));
  const [reconciledControls, setReconciledControls] = useState<Record<string, boolean>>({});
  const [schedules, setSchedules] = useState<GovernedScheduleBinding[]>([]);
  const [scheduleLoading, setScheduleLoading] = useState(false);
  const [scheduleError, setScheduleError] = useState<string | null>(null);
  const [scheduleErrorCode, setScheduleErrorCode] = useState<string | null>(null);
  const [scheduleBusy, setScheduleBusy] = useState<string | null>(null);
  const [scheduleReason, setScheduleReason] = useState("");
  const [schedulePending, setSchedulePending] = useState<Record<string, PendingScheduleMutation>>(() => ({ ...(recoveryAtMount?.schedules ?? {}) }));
  const controlPendingRef = useRef(controlPending);
  const verifyErrorCodeRef = useRef(verifyErrorCode);
  const revokeErrorCodeRef = useRef(revokeErrorCode);
  controlPendingRef.current = controlPending;
  verifyErrorCodeRef.current = verifyErrorCode;
  revokeErrorCodeRef.current = revokeErrorCode;
  const connectionOperationsRef = useRef<Record<string, { generation: number; controller: AbortController | null }>>({});
  const scheduleOperationRef = useRef<{ generation: number; controller: AbortController | null }>({ generation: 0, controller: null });
  const previousControlScopeRef = useRef<string | null>(controlScope);

  useEffect(() => {
    const previousScope = previousControlScopeRef.current;
    if (previousScope && previousScope !== controlScope) pendingCalendarControls.delete(previousScope);
    previousControlScopeRef.current = controlScope;
  }, [controlScope]);

  const updateControlPending = useCallback((updater: (state: CalendarControlPending) => CalendarControlPending) => {
    setControlPending((current) => {
      const next = updater(current);
      const previous = controlScope ? pendingCalendarControls.get(controlScope) : null;
      rememberCalendarControls(controlScope, { controls: next, schedules: previous?.schedules ?? schedulePending });
      return next;
    });
  }, [controlScope, schedulePending]);

  const updateSchedulePending = useCallback((updater: (state: Record<string, PendingScheduleMutation>) => Record<string, PendingScheduleMutation>) => {
    setSchedulePending((current) => {
      const next = updater(current);
      const previous = controlScope ? pendingCalendarControls.get(controlScope) : null;
      rememberCalendarControls(controlScope, { controls: previous?.controls ?? controlPending, schedules: next });
      return next;
    });
  }, [controlScope, controlPending]);

  const clearControl = useCallback((pendingKey: string) => {
    updateControlPending((state) => {
      const next = { ...state };
      delete next[pendingKey];
      return next;
    });
  }, [updateControlPending]);

  const startFreshControlAttempt = useCallback((pendingKey: string) => {
    clearControl(pendingKey);
    setReconciledControls((state) => ({ ...state, [pendingKey]: false }));
    const connectionId = pendingKey.split(":", 1)[0];
    if (pendingKey.endsWith(":verify")) {
      setVerifyError((state) => { const next = { ...state }; delete next[connectionId]; return next; });
      setVerifyErrorCode((state) => { const next = { ...state }; delete next[connectionId]; return next; });
    } else {
      setRevokeError((state) => { const next = { ...state }; delete next[connectionId]; return next; });
      setRevokeErrorCode((state) => { const next = { ...state }; delete next[connectionId]; return next; });
    }
  }, [clearControl]);

  const startFreshScheduleAttempt = useCallback((pendingKey: string) => {
    updateSchedulePending((state) => {
      const next = { ...state };
      delete next[pendingKey];
      return next;
    });
    setReconciledControls((state) => ({ ...state, [pendingKey]: false }));
    setScheduleError(null);
    setScheduleErrorCode(null);
  }, [updateSchedulePending]);

  const beginConnectionOperation = useCallback((connectionId: string) => {
    const previous = connectionOperationsRef.current[connectionId];
    previous?.controller?.abort();
    const operation = { generation: (previous?.generation ?? 0) + 1, controller: new AbortController() };
    connectionOperationsRef.current[connectionId] = operation;
    return operation;
  }, []);

  const isCurrentConnectionOperation = useCallback((connectionId: string, operation: { generation: number; controller: AbortController }) => {
    const current = connectionOperationsRef.current[connectionId];
    return mountedRef.current
      && current?.generation === operation.generation
      && current.controller === operation.controller
      && !operation.controller.signal.aborted;
  }, []);

  const invalidateConnectionOperation = useCallback((connectionId: string) => {
    const current = connectionOperationsRef.current[connectionId];
    if (!current) return;
    current.controller?.abort();
    connectionOperationsRef.current[connectionId] = { generation: current.generation + 1, controller: null };
  }, []);

  const loadConnections = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    try {
      const next = await boundedCalendarRequest((requestSignal) => listCalendarConnections(requestSignal), signal);
      if (!mountedRef.current || signal?.aborted) return;
      Object.keys(connectionOperationsRef.current).forEach(invalidateConnectionOperation);
      setConnections(next);
      setMetadataError(null);
      setAuthBlocked(false);
      setReconciledControls((current) => {
        const nextState = { ...current };
        Object.keys(controlPendingRef.current).forEach((pendingKey) => {
          const connectionId = pendingKey.split(":", 1)[0];
          const code = pendingKey.endsWith(":verify") ? verifyErrorCodeRef.current[connectionId] : revokeErrorCodeRef.current[connectionId];
          if (code && STALE_RECONCILIATION_CODES.has(code)) nextState[pendingKey] = true;
        });
        return nextState;
      });
    } catch (error) {
      if (!mountedRef.current || signal?.aborted || isAbort(error)) return;
      setMetadataError("Calendar connection metadata is unavailable. The service did not return a confirmed empty state.");
    } finally {
      if (mountedRef.current && !signal?.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    const controller = new AbortController();
    void loadConnections(controller.signal);
    return () => {
      mountedRef.current = false;
      controller.abort();
      Object.values(connectionOperationsRef.current).forEach((operation) => operation.controller?.abort());
      scheduleOperationRef.current.controller?.abort();
      setClientSecret("");
      setRefreshToken("");
    };
  }, [loadConnections]);

  if (service !== SERVICE) {
    return <div className="rounded border border-red-500/40 p-3 text-xs" role="alert">Unsupported calendar service.</div>;
  }

  const submit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setFormError(null);
    let current = pending;
    if (!current) {
      if (!label.trim() || !clientId.trim() || !refreshToken.trim()) {
        setFormError("Label, client ID, and refresh token are required.");
        return;
      }
      current = {
        request: {
          schema_version: 1,
          service: SERVICE,
          label: label.trim(),
          client_id: clientId.trim(),
          ...(clientSecret ? { client_secret: clientSecret } : {}),
          refresh_token: refreshToken,
          idempotency_key: idempotencyKey("calendar-connection"),
        },
      };
      setPending(current);
    }
    const controller = new AbortController();
    let timedOut = false;
    const timeout = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, REQUEST_TIMEOUT_MS);
    setSubmitting(true);
    try {
      const created = await boundedCalendarRequest((signal) => createCalendarConnection(current!.request, signal), controller.signal);
      if (!mountedRef.current || controller.signal.aborted) return;
      setConnections((items) => [created, ...items.filter((item) => item.connection_id !== created.connection_id)]);
      setPending(null);
      setLabel("");
      setClientId("");
      setClientSecret("");
      setRefreshToken("");
      setFormError(null);
    } catch (error) {
      if (!mountedRef.current) return;
      if (isAbort(error) || timedOut || !(error instanceof CalendarApiError) || error.status >= 500 || error.status === 408 || error.status === 429) {
        setFormError(timedOut ? "The connection request timed out without a receipt. The exact request is preserved; retry it to reconcile." : safeError(error));
      } else if (error.status === 409) {
        setFormError(safeError(error));
        void loadConnections();
      } else if (error.status === 401) {
        // The auth gate will return the cockpit to login. Drop the write-only
        // fields and the pending draft immediately so a revoked session can
        // never leave credentials available for a later resubmission.
        setPending(null);
        setAuthBlocked(true);
        setLabel("");
        setClientId("");
        setClientSecret("");
        setRefreshToken("");
        setFormError("The operator session expired. Sign in again before entering a new connection.");
      } else {
        setPending(null);
        setFormError(safeError(error));
      }
    } finally {
      window.clearTimeout(timeout);
      if (mountedRef.current) setSubmitting(false);
    }
  };

  const verify = async (connection: CalendarConnectionMetadata) => {
    const operation = beginConnectionOperation(connection.connection_id);
    setVerifyBusy(connection.connection_id);
    setVerifyError((state) => ({ ...state, [connection.connection_id]: "" }));
    setVerifyErrorCode((state) => ({ ...state, [connection.connection_id]: null }));
    const pendingKey = `${connection.connection_id}:verify`;
    const request = controlPending[pendingKey]?.request ?? { expected_revision: connection.revision, idempotency_key: idempotencyKey("calendar-verify") };
    updateControlPending((state) => ({ ...state, [pendingKey]: { action: "verify", request } }));
    try {
      const result = await boundedCalendarRequest((signal) => verifyCalendarConnection(connection.connection_id, request, signal), operation.controller.signal);
      if (!isCurrentConnectionOperation(connection.connection_id, operation) || result.connection.connection_id !== connection.connection_id) return;
      setVerification((state) => ({ ...state, [connection.connection_id]: result }));
      setConnections((items) => items.map((item) => item.connection_id === result.connection.connection_id ? result.connection : item));
      clearControl(pendingKey);
      setReconciledControls((state) => ({ ...state, [pendingKey]: false }));
    } catch (error) {
      if (isCurrentConnectionOperation(connection.connection_id, operation)) {
        if (isDefinitiveClientError(error)) clearControl(pendingKey);
        if (error instanceof CalendarApiError) setVerifyErrorCode((state) => ({ ...state, [connection.connection_id]: error.code }));
        setVerifyError((state) => ({ ...state, [connection.connection_id]: safeError(error) }));
      }
    } finally {
      if (isCurrentConnectionOperation(connection.connection_id, operation)) setVerifyBusy(null);
    }
  };

  const refreshCalendars = async (connection: CalendarConnectionMetadata) => {
    const operation = beginConnectionOperation(connection.connection_id);
    setVerifyBusy(connection.connection_id);
    setVerifyError((state) => ({ ...state, [connection.connection_id]: "" }));
    setVerifyErrorCode((state) => ({ ...state, [connection.connection_id]: null }));
    try {
      const result = await boundedCalendarRequest((signal) => listCalendars(connection.connection_id, signal), operation.controller.signal);
      if (isCurrentConnectionOperation(connection.connection_id, operation) && result.connection.connection_id === connection.connection_id) {
        setVerification((state) => ({ ...state, [connection.connection_id]: result }));
        setConnections((items) => items.map((item) => item.connection_id === result.connection.connection_id ? result.connection : item));
      }
    } catch (error) {
      if (isCurrentConnectionOperation(connection.connection_id, operation)) {
        if (error instanceof CalendarApiError) setVerifyErrorCode((state) => ({ ...state, [connection.connection_id]: error.code }));
        setVerifyError((state) => ({ ...state, [connection.connection_id]: safeError(error) }));
      }
    } finally {
      if (isCurrentConnectionOperation(connection.connection_id, operation)) setVerifyBusy(null);
    }
  };

  const revoke = async (connection: CalendarConnectionMetadata) => {
    const pendingKey = `${connection.connection_id}:revoke`;
    if (!controlPending[pendingKey] && !window.confirm(`Revoke ${connection.label}?`)) return;
    const operation = beginConnectionOperation(connection.connection_id);
    setRevokeBusy(connection.connection_id);
    setRevokeError((state) => ({ ...state, [connection.connection_id]: "" }));
    setRevokeErrorCode((state) => ({ ...state, [connection.connection_id]: null }));
    try {
      const request = controlPending[pendingKey]?.request ?? { expected_revision: connection.revision, idempotency_key: idempotencyKey("calendar-revoke") };
      updateControlPending((state) => ({ ...state, [pendingKey]: { action: "revoke", request } }));
      const revoked = await boundedCalendarRequest((signal) => revokeCalendarConnection(connection.connection_id, request, signal), operation.controller.signal);
      if (!isCurrentConnectionOperation(connection.connection_id, operation) || revoked.connection_id !== connection.connection_id) return;
      setConnections((items) => items.map((item) => item.connection_id === revoked.connection_id ? revoked : item));
      setVerification((state) => ({ ...state, [connection.connection_id]: null }));
      clearControl(pendingKey);
      setReconciledControls((state) => ({ ...state, [pendingKey]: false }));
    } catch (error) {
      if (isCurrentConnectionOperation(connection.connection_id, operation)) {
        if (isDefinitiveClientError(error)) clearControl(pendingKey);
        if (error instanceof CalendarApiError) setRevokeErrorCode((state) => ({ ...state, [connection.connection_id]: error.code }));
        setRevokeError((state) => ({ ...state, [connection.connection_id]: safeError(error) }));
      }
    } finally {
      if (isCurrentConnectionOperation(connection.connection_id, operation)) setRevokeBusy(null);
    }
  };

  const loadSchedules = async () => {
    setScheduleLoading(true);
    setScheduleError(null);
    try {
      const result = await boundedCalendarRequest((signal) => listGovernedSchedules(signal));
      if (mountedRef.current) {
        setSchedules(result);
        if (scheduleErrorCode && STALE_RECONCILIATION_CODES.has(scheduleErrorCode)) {
          setReconciledControls((state) => {
            const next = { ...state };
            Object.keys(schedulePending).forEach((key) => { next[key] = true; });
            return next;
          });
        }
        setScheduleErrorCode(null);
      }
    } catch (error) {
      if (mountedRef.current) {
        if (error instanceof CalendarApiError) setScheduleErrorCode(error.code);
        setScheduleError(safeError(error));
      }
    } finally {
      if (mountedRef.current) setScheduleLoading(false);
    }
  };

  const controlSchedule = async (binding: GovernedScheduleBinding, action: "pause" | "resume") => {
    const pendingKey = `${binding.binding_id}:${action}`;
    const existing = schedulePending[pendingKey];
    const request = existing?.action === action ? existing.request : {
      action,
      expected_binding_revision: binding.binding_revision,
      idempotency_key: idempotencyKey(`calendar-schedule-${action}`),
    };
    scheduleOperationRef.current.controller?.abort();
    const operation = { generation: scheduleOperationRef.current.generation + 1, controller: new AbortController() };
    scheduleOperationRef.current = operation;
    updateSchedulePending((state) => ({ ...state, [pendingKey]: { action, request } }));
    setScheduleBusy(binding.binding_id);
    setScheduleError(null);
    setScheduleErrorCode(null);
    try {
      const result = await boundedCalendarRequest((signal) => controlGovernedSchedule(binding.binding_id, request, signal), operation.controller.signal);
      if (mountedRef.current && scheduleOperationRef.current.generation === operation.generation && !operation.controller.signal.aborted) {
        setSchedules((items) => items.map((item) => item.binding_id === result.binding_id ? result : item));
        updateSchedulePending((state) => { const next = { ...state }; delete next[pendingKey]; return next; });
      }
    } catch (error) {
      if (mountedRef.current && scheduleOperationRef.current.generation === operation.generation && !operation.controller.signal.aborted) {
        if (isDefinitiveClientError(error)) {
          updateSchedulePending((state) => { const next = { ...state }; delete next[pendingKey]; return next; });
        }
        if (error instanceof CalendarApiError) setScheduleErrorCode(error.code);
        setScheduleError(safeError(error));
      }
    } finally {
      if (mountedRef.current && scheduleOperationRef.current.generation === operation.generation) setScheduleBusy(null);
    }
  };

  const revokeSchedule = async (binding: GovernedScheduleBinding) => {
    const pendingKey = `${binding.binding_id}:revoke`;
    const pendingMutation = schedulePending[pendingKey];
    const reason = pendingMutation?.action === "revoke" ? pendingMutation.request.reason : scheduleReason.trim();
    if (!reason || reason.length > 500) {
      setScheduleError("Enter a bounded reason before revoking a schedule.");
      return;
    }
    const request = pendingMutation?.action === "revoke" ? pendingMutation.request : {
      expected_binding_revision: binding.binding_revision,
      idempotency_key: idempotencyKey("calendar-schedule-revoke"),
      reason,
    };
    scheduleOperationRef.current.controller?.abort();
    const operation = { generation: scheduleOperationRef.current.generation + 1, controller: new AbortController() };
    scheduleOperationRef.current = operation;
    updateSchedulePending((state) => ({ ...state, [pendingKey]: { action: "revoke", request } }));
    setScheduleBusy(binding.binding_id);
    setScheduleError(null);
    setScheduleErrorCode(null);
    try {
      const result = await boundedCalendarRequest((signal) => revokeGovernedSchedule(binding.binding_id, request, signal), operation.controller.signal);
      if (mountedRef.current && scheduleOperationRef.current.generation === operation.generation && !operation.controller.signal.aborted) {
        setSchedules((items) => items.map((item) => item.binding_id === result.binding_id ? result : item));
        updateSchedulePending((state) => { const next = { ...state }; delete next[pendingKey]; return next; });
      }
    } catch (error) {
      if (mountedRef.current && scheduleOperationRef.current.generation === operation.generation && !operation.controller.signal.aborted) {
        if (isDefinitiveClientError(error)) {
          updateSchedulePending((state) => { const next = { ...state }; delete next[pendingKey]; return next; });
        }
        if (error instanceof CalendarApiError) setScheduleErrorCode(error.code);
        setScheduleError(safeError(error));
      }
    } finally {
      if (mountedRef.current && scheduleOperationRef.current.generation === operation.generation) setScheduleBusy(null);
    }
  };

  return (
    <section className="px-1" aria-label="Calendar connection settings">
      <div className="text-[10px] uppercase tracking-wider text-retro-border font-bold mb-1">Calendar read connection</div>
      <p className="text-[9px] text-retro-text/50 mb-2">Read-only calendar access. Credentials are used once for setup and are never shown, cached, or sent to the model.</p>
      <div className="rounded border border-retro-text/10 p-2 mb-3">
        <div className="text-[9px] uppercase tracking-wider text-retro-text/50 mb-2">Fixed service · calendar_readonly</div>
        <form className="grid gap-2 sm:grid-cols-2" onSubmit={(event) => void submit(event)}>
          {authBlocked && <div className="sm:col-span-2 rounded border border-amber-500/40 p-2 text-[10px]" role="status">Sign in through the operator login gate before entering a new connection.</div>}
          <fieldset className="contents" disabled={authBlocked || submitting}>
          <label className="text-[10px]">Label<input className="cockpit-input mt-1 w-full" maxLength={200} value={label} onChange={(event) => setLabel(event.currentTarget.value)} autoComplete="off" /></label>
          <label className="text-[10px]">Client ID<input className="cockpit-input mt-1 w-full" maxLength={1024} value={clientId} onChange={(event) => setClientId(event.currentTarget.value)} autoComplete="off" /></label>
          <label className="text-[10px]">Client secret (optional)<input className="cockpit-input mt-1 w-full" type="password" maxLength={4096} value={clientSecret} onChange={(event) => setClientSecret(event.currentTarget.value)} autoComplete="new-password" /></label>
          <label className="text-[10px]">Refresh token<input className="cockpit-input mt-1 w-full" type="password" maxLength={4096} required value={refreshToken} onChange={(event) => setRefreshToken(event.currentTarget.value)} autoComplete="new-password" /></label>
          {pending && <div className="sm:col-span-2 rounded border border-amber-500/40 p-2 text-[10px]" role="status">The exact setup request has no confirmed receipt. Retry it to reconcile; editing the fields starts a new deliberate attempt.</div>}
          {formError && <div className="sm:col-span-2 rounded border border-amber-500/40 p-2 text-[10px]" role="alert">{formError}</div>}
          <div className="sm:col-span-2 flex flex-wrap gap-2">
            <button type="submit" className="cockpit-feedback-button" disabled={authBlocked || submitting}>{submitting ? "Saving…" : pending ? "Retry exact setup" : "Save connection"}</button>
            <button type="button" className="cockpit-feedback-button" onClick={() => void loadConnections()} disabled={loading}>Refresh metadata</button>
          </div>
          </fieldset>
        </form>
      </div>

      <div className="text-[10px] uppercase tracking-wider text-retro-border font-bold mb-1">Saved connections</div>
      {loading && connections.length === 0 ? <div className="cockpit-empty" role="status">Loading calendar metadata…</div> : metadataError ? <div className="rounded border border-amber-500/40 p-2 text-[10px]" role="alert">{metadataError}<button type="button" className="ml-2 underline" onClick={() => void loadConnections()}>Retry</button></div> : connections.length === 0 ? <div className="cockpit-empty">No calendar connection is configured.</div> : (
        <div className="grid gap-2">
          {connections.map((connection) => {
            const result = verification[connection.connection_id];
            const verifyMessage = verifyError[connection.connection_id];
            const connectionBusy = verifyBusy === connection.connection_id || revokeBusy === connection.connection_id;
            const verifyPendingKey = `${connection.connection_id}:verify`;
            const revokePendingKey = `${connection.connection_id}:revoke`;
            return (
              <article key={connection.connection_id} className="rounded border border-retro-text/10 p-2 text-[10px]">
                <div className="flex flex-wrap items-center justify-between gap-2"><strong>{connection.label}</strong><span className="uppercase tracking-wider">{connectionStateLabel(connection)} · revision {connection.revision}</span></div>
                <div className="mt-1 text-retro-text/50">Fingerprint {connection.credential_fingerprint} · updated {new Date(connection.updated_at).toLocaleString()}</div>
                <div className="mt-2 flex flex-wrap gap-2">
                  <button type="button" className="cockpit-feedback-button" onClick={() => void verify(connection)} disabled={connectionBusy || connection.state !== "active"}>{verifyBusy === connection.connection_id ? "Checking…" : controlPending[verifyPendingKey] ? "Retry exact verify" : "Verify and list calendars"}</button>
                  {result && <button type="button" className="cockpit-feedback-button" onClick={() => void refreshCalendars(connection)} disabled={connectionBusy}>Refresh list</button>}
                  <button type="button" className="cockpit-feedback-button" onClick={() => void revoke(connection)} disabled={connectionBusy || connection.state === "revoked"}>{revokeBusy === connection.connection_id ? "Revoking…" : controlPending[revokePendingKey] ? "Retry exact revoke" : "Revoke"}</button>
                </div>
                {verifyMessage && <div className="mt-2 text-amber-300" role="alert">{verifyMessage}{reconciledControls[verifyPendingKey] && verifyErrorCode[connection.connection_id] && STALE_RECONCILIATION_CODES.has(verifyErrorCode[connection.connection_id]!) && <button type="button" className="ml-2 underline" onClick={() => startFreshControlAttempt(verifyPendingKey)}>Start new verify attempt</button>}</div>}
                {revokeError[connection.connection_id] && <div className="mt-2 text-amber-300" role="alert">{revokeError[connection.connection_id]}{reconciledControls[revokePendingKey] && revokeErrorCode[connection.connection_id] && STALE_RECONCILIATION_CODES.has(revokeErrorCode[connection.connection_id]!) && <button type="button" className="ml-2 underline" onClick={() => startFreshControlAttempt(revokePendingKey)}>Start new revoke attempt</button>}</div>}
                {result && <div className="mt-2 rounded border border-emerald-500/30 p-2" aria-label="Verified calendars"><div>Verified revision {result.connection.revision} · {result.calendars.length} shown · {result.truncated ? "more calendars omitted by the server" : "bounded list complete"}</div><div className="mt-1 grid gap-1">{result.calendars.map((calendar: CalendarOption) => <div key={calendar.calendar_id} className="flex justify-between gap-2"><span>{calendar.summary}</span><span className="font-mono opacity-60">{calendar.calendar_id}</span></div>)}</div></div>}
              </article>
            );
          })}
        </div>
      )}
      <div className="mt-3 rounded border border-retro-text/10 p-2">
        <div className="flex flex-wrap items-center justify-between gap-2"><div><div className="text-[10px] uppercase tracking-wider text-retro-border font-bold">Governed observation schedules</div><div className="text-[9px] text-retro-text/50">Schedules are finite, owner-bound, and separate from one-off preparation tasks.</div></div><button type="button" className="cockpit-feedback-button" onClick={() => void loadSchedules()} disabled={scheduleLoading}>{scheduleLoading ? "Loading…" : "Load schedules"}</button></div>
        {scheduleError && <div className="mt-2 text-[10px] text-amber-300" role="alert">{scheduleError}</div>}
        {schedules.length > 0 && (
          <div className="mt-2 grid gap-2">
            {schedules.map((binding) => {
              const pausePending = schedulePending[`${binding.binding_id}:pause`];
              const resumePending = schedulePending[`${binding.binding_id}:resume`];
              const revokePending = schedulePending[`${binding.binding_id}:revoke`];
              return (
                <article key={binding.binding_id} className="rounded border border-retro-text/10 p-2 text-[10px]">
                  <div className="flex flex-wrap justify-between gap-2"><span className="font-mono break-all">{binding.binding_id}</span><span>{binding.state} · revision {binding.binding_revision}</span></div>
                  <div className="mt-1">{binding.cadence.kind} · {binding.cadence.timezone} · expires {new Date(binding.expires_at).toLocaleString()}</div>
                  {binding.latest_occurrence && <div className="mt-2 rounded border border-retro-text/10 p-2" role="region" aria-label="Latest governed occurrence">
                    <div>Latest occurrence: <strong>{binding.latest_occurrence.state}</strong> · updated {new Date(binding.latest_occurrence.updated_at).toLocaleString()}</div>
                    {binding.latest_occurrence.failure_code && <div className="mt-1">Failure code: <code>{binding.latest_occurrence.failure_code}</code></div>}
                    {binding.latest_occurrence.recovery_action && <div className="mt-1">Recovery: <code>{binding.latest_occurrence.recovery_action}</code></div>}
                  </div>}
                  <div className="mt-2 flex flex-wrap gap-2">
                    {binding.state === "active" && <button type="button" className="cockpit-feedback-button" onClick={() => void controlSchedule(binding, "pause")} disabled={scheduleBusy === binding.binding_id}>{pausePending ? "Retry exact pause" : "Pause"}</button>}
                    {binding.state === "paused" && <button type="button" className="cockpit-feedback-button" onClick={() => void controlSchedule(binding, "resume")} disabled={scheduleBusy === binding.binding_id}>{resumePending ? "Retry exact resume" : "Resume"}</button>}
                    {scheduleErrorCode && STALE_RECONCILIATION_CODES.has(scheduleErrorCode) && (reconciledControls[`${binding.binding_id}:pause`] || reconciledControls[`${binding.binding_id}:resume`] || reconciledControls[`${binding.binding_id}:revoke`]) && <button type="button" className="cockpit-feedback-button" onClick={() => startFreshScheduleAttempt(`${binding.binding_id}:${pausePending ? "pause" : resumePending ? "resume" : "revoke"}`)}>Start new schedule attempt</button>}
                    {!(["revoked", "expired"].includes(binding.state)) && <button type="button" className="cockpit-feedback-button" onClick={() => void revokeSchedule(binding)} disabled={scheduleBusy === binding.binding_id}>{revokePending ? "Retry exact revoke" : "Revoke"}</button>}
                  </div>
                </article>
              );
            })}
          </div>
        )}
        {schedules.length === 0 && !scheduleLoading && <div className="mt-2 text-[10px] text-retro-text/40">No loaded schedule records.</div>}
        {schedules.some((binding) => !["revoked", "expired"].includes(binding.state)) && <label className="mt-2 block text-[10px]">Revoke reason<input className="cockpit-input mt-1 w-full" maxLength={500} value={scheduleReason} onChange={(event) => setScheduleReason(event.currentTarget.value)} /></label>}
      </div>
    </section>
  );
}
