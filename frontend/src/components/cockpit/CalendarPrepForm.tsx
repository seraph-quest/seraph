import { useEffect, useMemo, useRef, useState } from "react";
import { CalendarReschedulePanel } from "./CalendarReschedulePanel";

import {
  CalendarApiError,
  createCalendarPrep,
  createGovernedSchedule,
  createReadConsent,
  listCalendarConnections,
  listCalendarEvents,
  verifyCalendarConnection,
} from "../../lib/calendar";
import type {
  CalendarAllowedField,
  CalendarConnectionMetadata,
  CalendarEventListResponse,
  CalendarEventOption,
  CalendarPrepResponse,
  CreateCalendarPrepRequest,
  CreateCalendarReadConsentRequest,
  CreateCalendarScheduleRequest,
  GoalInfo,
  GovernedScheduleBinding,
  WorkBoardTask,
} from "../../types";

const CAPABILITY = "calendar.meeting-prep.v1" as const;
const REQUEST_TIMEOUT_MS = 15_000;
const MAX_TITLE = 200;
const SCHEDULE_MAX_WINDOW_MS = 24 * 60 * 60 * 1000;
const REQUIRED_ALLOWED_FIELDS: CalendarAllowedField[] = ["summary", "start", "end"];
const STALE_RECONCILIATION_CODES = new Set([
  "calendar_connection_revision_stale",
  "calendar_revision_stale",
  "calendar_event_revision_stale",
  "calendar_schedule_revision_stale",
  "calendar_schedule_binding_stale",
]);

function idempotencyKey(prefix: string): string {
  try {
    return `${prefix}:${crypto.randomUUID()}`;
  } catch {
    return `${prefix}:${Date.now().toString(36)}:${Math.random().toString(36).slice(2)}`;
  }
}

function localDateTime(value: Date): string {
  const offset = value.getTimezoneOffset() * 60_000;
  return new Date(value.getTime() - offset).toISOString().slice(0, 16);
}

function safeError(error: unknown): string {
  if (error instanceof CalendarApiError) {
    if (error.status === 409) return "The calendar revision changed or an earlier operation is unresolved. Refresh metadata and retry the exact request.";
    if (error.status === 401) return "The operator session is unavailable. Sign in again before continuing.";
    if (error.status >= 400 && error.status < 500) return `${error.message}${error.recovery ? ` ${error.recovery}` : " Correct the form and submit a new request."}`;
  }
  return "The outcome is unconfirmed. Keep this form open and retry the same request when ready.";
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function isDefinitiveClientError(error: unknown): error is CalendarApiError {
  return error instanceof CalendarApiError
    && error.status >= 400
    && error.status < 500
    && ![408, 409, 429].includes(error.status);
}

async function boundedRequest<T>(operation: (signal: AbortSignal) => Promise<T>, parentSignal?: AbortSignal): Promise<T> {
  const controller = new AbortController();
  const abortFromParent = () => controller.abort();
  if (parentSignal?.aborted) controller.abort();
  else parentSignal?.addEventListener("abort", abortFromParent, { once: true });
  const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    return await operation(controller.signal);
  } finally {
    window.clearTimeout(timeout);
    parentSignal?.removeEventListener("abort", abortFromParent);
  }
}

function goalRevision(goal: GoalInfo | undefined): number | null {
  return goal && typeof goal.revision === "number" && Number.isSafeInteger(goal.revision) && goal.revision > 0 ? goal.revision : null;
}

function toExpiry(value: string): string | null {
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime())) return null;
  return parsed.toISOString();
}

function scheduleExpiryLimitMs(consentExpiresAt?: string | null, prepExpiresAt?: string | null): number {
  const limits = [Date.now() + SCHEDULE_MAX_WINDOW_MS];
  for (const value of [consentExpiresAt, prepExpiresAt]) {
    if (!value) continue;
    const parsed = Date.parse(value);
    if (Number.isFinite(parsed)) limits.push(parsed);
  }
  return Math.min(...limits);
}

function boundedScheduleInput(
  value: string,
  consentExpiresAt?: string | null,
  prepExpiresAt?: string | null,
): string | null {
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) return null;
  return localDateTime(new Date(Math.min(parsed, scheduleExpiryLimitMs(consentExpiresAt, prepExpiresAt))));
}

function isIanaTimeZone(value: string): boolean {
  const zone = value.trim();
  if (!zone || zone.length > 128 || /[\u0000-\u001f\u007f]/.test(zone)) return false;
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: zone }).format();
    return true;
  } catch {
    return false;
  }
}

function displayEventTime(value: string): string {
  return /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : new Date(value).toLocaleString();
}

type PendingIntent =
  | { kind: "consent"; request: CreateCalendarReadConsentRequest }
  | { kind: "prep"; request: CreateCalendarPrepRequest }
  | { kind: "schedule"; request: CreateCalendarScheduleRequest };

export interface PendingCalendarSubmission {
  intent: PendingIntent;
  confirmedPrep?: CalendarPrepResponse | null;
}

export interface CalendarPrepFormProps {
  ownerPrincipalId?: string | null;
  ownerSessionId?: string | null;
  goals: GoalInfo[];
  onCreated: (task: WorkBoardTask, receipt: CalendarPrepResponse) => void | Promise<void>;
  onClose: () => void;
  onOpenSettings?: () => void;
  initialPending?: PendingCalendarSubmission | null;
  onPendingChange?: (pending: PendingCalendarSubmission | null) => void;
}

export function CalendarPrepForm({ goals, onCreated, onClose, onOpenSettings, initialPending, onPendingChange, ownerPrincipalId, ownerSessionId }: CalendarPrepFormProps) {
  const mountedRef = useRef(true);
  const [connections, setConnections] = useState<CalendarConnectionMetadata[]>([]);
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [connectionLoading, setConnectionLoading] = useState(true);
  const [selectedConnectionId, setSelectedConnectionId] = useState("");
  const [calendars, setCalendars] = useState<{ calendar_id: string; summary: string }[]>([]);
  const [calendarListRevision, setCalendarListRevision] = useState("");
  const [calendarError, setCalendarError] = useState<string | null>(null);
  const [calendarLoading, setCalendarLoading] = useState(false);
  const [selectedCalendarId, setSelectedCalendarId] = useState("");
  const [goalId, setGoalId] = useState(goals.find((goal) => goalRevision(goal))?.id ?? "");
  const [allowedFields, setAllowedFields] = useState<CalendarAllowedField[]>(["summary", "start", "end", "location"]);
  const [windowMinutes, setWindowMinutes] = useState("1440");
  const [maxEvents, setMaxEvents] = useState("20");
  const [allowRemoteModel, setAllowRemoteModel] = useState(false);
  const [expiresAt, setExpiresAt] = useState(() => localDateTime(new Date(Date.now() + 24 * 60 * 60 * 1000)));
  const [consent, setConsent] = useState<Awaited<ReturnType<typeof createReadConsent>> | null>(null);
  const [eventResponse, setEventResponse] = useState<CalendarEventListResponse | null>(null);
  const [selectedEventId, setSelectedEventId] = useState("");
  const [eventsLoading, setEventsLoading] = useState(false);
  const [eventsError, setEventsError] = useState<string | null>(null);
  const [title, setTitle] = useState("Prepare for meeting");
  const [scheduleEnabled, setScheduleEnabled] = useState(false);
  const [cadenceKind, setCadenceKind] = useState<"5min" | "hourly" | "6h" | "daily">("daily");
  const [timezone, setTimezone] = useState(() => Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC");
  const [dailyHour, setDailyHour] = useState("9");
  const [dailyMinute, setDailyMinute] = useState("0");
  const [scheduleExpiresAt, setScheduleExpiresAt] = useState(() => localDateTime(new Date(Date.now() + SCHEDULE_MAX_WINDOW_MS)));
  const [pending, setPending] = useState<PendingCalendarSubmission | null>(initialPending ?? null);
  const [confirmedPrep, setConfirmedPrep] = useState<CalendarPrepResponse | null>(initialPending?.confirmedPrep ?? null);
  const [schedule, setSchedule] = useState<GovernedScheduleBinding | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reconciledAfterStale, setReconciledAfterStale] = useState(false);
  const [staleReconciliationCode, setStaleReconciliationCode] = useState<string | null>(null);
  const verifyGenerationRef = useRef(0);
  const verifyControllerRef = useRef<AbortController | null>(null);
  const eventsGenerationRef = useRef(0);
  const eventsControllerRef = useRef<AbortController | null>(null);
  const consentRef = useRef<typeof consent>(consent);
  useEffect(() => {
    consentRef.current = consent;
  }, [consent]);

  const selectedConnection = useMemo(() => connections.find((item) => item.connection_id === selectedConnectionId) ?? null, [connections, selectedConnectionId]);
  const selectedGoal = useMemo(() => goals.find((goal) => goal.id === goalId) ?? null, [goals, goalId]);
  const selectedEvent = useMemo(() => eventResponse?.events.find((event) => event.event_binding_id === selectedEventId) ?? null, [eventResponse, selectedEventId]);
  const hasRequiredAllowedFields = REQUIRED_ALLOWED_FIELDS.every((field) => allowedFields.includes(field));
  const selectedCalendarIsCurrent = Boolean(selectedCalendarId && calendars.some((calendar) => calendar.calendar_id === selectedCalendarId));
  const consentIsActiveAndFuture = Boolean(consent && consent.state === "active" && Date.parse(consent.expires_at) > Date.now());
  const scheduleExpiryLimit = useMemo(
    () => scheduleExpiryLimitMs(consent?.expires_at, confirmedPrep?.input_artifact.expires_at),
    [consent?.expires_at, confirmedPrep?.input_artifact.expires_at],
  );
  const scheduleExpiryMax = localDateTime(new Date(scheduleExpiryLimit));

  useEffect(() => {
    setScheduleExpiresAt((current) => boundedScheduleInput(current, consent?.expires_at, confirmedPrep?.input_artifact.expires_at) ?? current);
  }, [consent?.expires_at, confirmedPrep?.input_artifact.expires_at]);

  useEffect(() => {
    mountedRef.current = true;
    const controller = new AbortController();
    setConnectionLoading(true);
    void listCalendarConnections(controller.signal).then((result) => {
      if (!mountedRef.current || controller.signal.aborted) return;
      setConnections(result);
      setSelectedConnectionId((current) => current || result.find((item) => item.state === "active")?.connection_id || "");
      setConnectionError(null);
    }).catch((reason) => {
      if (!mountedRef.current || controller.signal.aborted || isAbort(reason)) return;
      setConnectionError("Calendar connections are unavailable. Configure an active read-only connection in Settings.");
    }).finally(() => {
      if (mountedRef.current && !controller.signal.aborted) setConnectionLoading(false);
    });
    return () => {
      mountedRef.current = false;
      controller.abort();
      verifyGenerationRef.current += 1;
      verifyControllerRef.current?.abort();
      eventsGenerationRef.current += 1;
      eventsControllerRef.current?.abort();
    };
  }, []);

  useEffect(() => {
    onPendingChange?.(pending);
  }, [onPendingChange, pending]);

  const chooseConnection = (connectionId: string) => {
    verifyGenerationRef.current += 1;
    verifyControllerRef.current?.abort();
    eventsGenerationRef.current += 1;
    eventsControllerRef.current?.abort();
    setCalendarLoading(false);
    setEventsLoading(false);
    setSelectedConnectionId(connectionId);
    setCalendars([]);
    setCalendarListRevision("");
    setSelectedCalendarId("");
    consentRef.current = null;
    setConsent(null);
    setEventResponse(null);
    setSelectedEventId("");
    setCalendarError(null);
    setEventsError(null);
  };

  const verify = async () => {
    if (!selectedConnection || selectedConnection.state !== "active") {
      setCalendarError("Choose an active connection before verifying calendars.");
      return;
    }
    const connectionAtStart = selectedConnection;
    const generation = ++verifyGenerationRef.current;
    verifyControllerRef.current?.abort();
    const requestController = new AbortController();
    verifyControllerRef.current = requestController;
    setCalendarLoading(true);
    setCalendarError(null);
    try {
      const result = await boundedRequest((signal) => verifyCalendarConnection(connectionAtStart.connection_id, { expected_revision: connectionAtStart.revision, idempotency_key: idempotencyKey("calendar-verify") }, signal), requestController.signal);
      if (!mountedRef.current || generation !== verifyGenerationRef.current || requestController.signal.aborted || selectedConnectionId !== connectionAtStart.connection_id) return;
      setCalendars(result.calendars);
      setCalendarListRevision(result.calendar_list_revision);
      setSelectedCalendarId(result.calendars[0]?.calendar_id ?? "");
      setConnections((items) => items.map((item) => item.connection_id === result.connection.connection_id ? result.connection : item));
      setReconciledAfterStale(true);
      setStaleReconciliationCode(null);
    } catch (reason) {
      if (mountedRef.current && generation === verifyGenerationRef.current && !requestController.signal.aborted) setCalendarError(safeError(reason));
    } finally {
      if (mountedRef.current && generation === verifyGenerationRef.current) setCalendarLoading(false);
      if (verifyControllerRef.current === requestController) verifyControllerRef.current = null;
    }
  };

  const createConsent = async () => {
    setError(null);
    const revision = goalRevision(selectedGoal ?? undefined);
    const expiry = toExpiry(expiresAt);
    const window = Number(windowMinutes);
    const events = Number(maxEvents);
    if (!selectedConnection || selectedConnection.state !== "active") return setError("Choose an active connection.");
    if (!selectedCalendarId || !calendarListRevision || !selectedCalendarIsCurrent) return setError("Verify the connection and choose one calendar from the current returned list.");
    if (!selectedGoal || revision === null) return setError("Choose an owned goal with a current revision.");
    if (!hasRequiredAllowedFields) return setError("Summary, start, and end are required consent fields.");
    if (!expiry || new Date(expiry).getTime() <= Date.now() || new Date(expiry).getTime() > Date.now() + 7 * 24 * 60 * 60 * 1000) return setError("Consent expiry must be in the future and within seven days.");
    if (!Number.isInteger(window) || window < 5 || window > 1440) return setError("The consent window must be between 5 and 1440 minutes.");
    if (!Number.isInteger(events) || events < 1 || events > 50) return setError("Maximum events must be between 1 and 50.");
    const request: CreateCalendarReadConsentRequest = {
      schema_version: 1,
      connection_id: selectedConnection.connection_id,
      calendar_id: selectedCalendarId,
      goal_id: selectedGoal.id,
      goal_revision: revision,
      allowed_fields: allowedFields,
      window_minutes: window,
      max_events: events,
      allow_remote_model: allowRemoteModel,
      expires_at: expiry,
      idempotency_key: idempotencyKey("calendar-consent"),
    };
    await submitConsent({ kind: "consent", request });
  };

  const submitConsent = async (intent: Extract<PendingIntent, { kind: "consent" }>) => {
    setPending({ intent });
    setBusy("consent");
    try {
      const result = await boundedRequest((signal) => createReadConsent(intent.request, signal));
      if (!mountedRef.current) return;
      consentRef.current = result;
      setConsent(result);
      setPending(null);
      setError(null);
      setEventsError(null);
      await loadEvents(result.connection_id, result);
    } catch (reason) {
      if (!mountedRef.current) return;
      if (isDefinitiveClientError(reason)) setPending(null);
      if (reason instanceof CalendarApiError && STALE_RECONCILIATION_CODES.has(reason.code)) {
        setReconciledAfterStale(false);
        setStaleReconciliationCode(reason.code);
      }
      setError(safeError(reason));
    } finally {
      if (mountedRef.current) setBusy(null);
    }
  };

  const loadEvents = async (connectionId: string, currentConsent: NonNullable<typeof consent>) => {
    const generation = ++eventsGenerationRef.current;
    eventsControllerRef.current?.abort();
    const requestController = new AbortController();
    eventsControllerRef.current = requestController;
    setEventsLoading(true);
    setEventsError(null);
    try {
      const result = await boundedRequest((signal) => listCalendarEvents(connectionId, currentConsent.consent_id, signal), requestController.signal);
      if (!mountedRef.current || generation !== eventsGenerationRef.current || requestController.signal.aborted || selectedConnectionId !== connectionId || consentRef.current?.consent_id !== currentConsent.consent_id || consentRef.current?.revision !== currentConsent.revision) return;
      if (result.consent_id !== currentConsent.consent_id || result.consent_revision !== currentConsent.revision || result.connection_revision !== currentConsent.connection_revision) {
        setEventsError("The returned event list does not match the active consent revision. Refresh consent before selecting an event.");
        return;
      }
      setEventResponse(result);
      setCalendarListRevision(result.calendar_list_revision);
      setSelectedEventId("");
      setReconciledAfterStale(true);
      setStaleReconciliationCode(null);
    } catch (reason) {
      if (mountedRef.current && generation === eventsGenerationRef.current && !requestController.signal.aborted) {
        if (reason instanceof CalendarApiError && STALE_RECONCILIATION_CODES.has(reason.code)) {
          setReconciledAfterStale(false);
          setStaleReconciliationCode(reason.code);
        }
        setEventsError(safeError(reason));
      }
    } finally {
      if (mountedRef.current && generation === eventsGenerationRef.current) setEventsLoading(false);
      if (eventsControllerRef.current === requestController) eventsControllerRef.current = null;
    }
  };

  const buildPrepRequest = (): CreateCalendarPrepRequest | null => {
    const revision = goalRevision(selectedGoal ?? undefined);
    if (!selectedGoal || revision === null || !selectedConnection || !consent || !consentIsActiveAndFuture || !selectedEvent || !calendarListRevision || !selectedCalendarIsCurrent || !hasRequiredAllowedFields) return null;
    const currentConsent = consent;
    if (!title.trim() || title.trim().length > MAX_TITLE) return null;
    return {
      schema_version: 1,
      input: {
        schema_version: 1,
        consent_id: currentConsent.consent_id,
        event_binding_id: selectedEvent.event_binding_id,
        expected_event_binding_revision: selectedEvent.event_binding_revision,
        expected_consent_revision: currentConsent.revision,
        expected_connection_revision: selectedConnection.revision,
        event_revision: selectedEvent.event_revision,
        // The event binding carries the revision that was observed for this
        // exact event. The envelope's current list revision can advance when
        // an unrelated event changes after the selected event was read.
        calendar_list_revision: selectedEvent.calendar_list_revision,
        goal_id: selectedGoal.id,
        goal_revision: revision,
        purpose: "bounded preparation request",
      },
      title: title.trim(),
      idempotency_key: idempotencyKey("calendar-prep"),
    };
  };

  const buildScheduleRequest = (
    prep: CalendarPrepResponse | null = confirmedPrep,
    expiryInput: string = scheduleExpiresAt,
  ): CreateCalendarScheduleRequest | null => {
    const revision = goalRevision(selectedGoal ?? undefined);
    const expiry = toExpiry(expiryInput);
    if (!selectedGoal || revision === null || !consent || !consentIsActiveAndFuture || !selectedCalendarId || !selectedCalendarIsCurrent || !expiry || new Date(expiry).getTime() <= Date.now()) return null;
    const currentConsent = consent;
    if (new Date(expiry).getTime() > scheduleExpiryLimitMs(currentConsent.expires_at, prep?.input_artifact.expires_at)) return null;
    if (!isIanaTimeZone(timezone)) return null;
    const hour = Number(dailyHour);
    const minute = Number(dailyMinute);
    if (cadenceKind === "daily" && (!Number.isInteger(hour) || hour < 0 || hour > 23 || !Number.isInteger(minute) || minute < 0 || minute > 59)) return null;
    return {
      schema_version: 1,
      consent_id: currentConsent.consent_id,
      goal_id: selectedGoal.id,
      goal_revision: revision,
      calendar_id: selectedCalendarId,
      cadence: { kind: cadenceKind, timezone: timezone.trim(), daily_hour: cadenceKind === "daily" ? hour : null, daily_minute: cadenceKind === "daily" ? minute : null },
      expires_at: expiry,
      idempotency_key: idempotencyKey("calendar-schedule"),
    };
  };

  const continueWithPrep = async (prep: CalendarPrepResponse) => {
    setPending(null);
    onPendingChange?.(null);
    await onCreated(prep.task, prep);
  };

  const submitPrep = async () => {
    setError(null);
    const request = buildPrepRequest();
    if (!request) {
      setError("Choose a current goal, active consent, returned event, and bounded title before preparing.");
      return;
    }
    await runPrep({ kind: "prep", request });
  };

  const runPrep = async (intent: Extract<PendingIntent, { kind: "prep" }>) => {
    setPending({ intent });
    setBusy("prep");
    try {
      const result = await boundedRequest((signal) => createCalendarPrep(intent.request, signal));
      if (!mountedRef.current) return;
      setConfirmedPrep(result);
      setPending(null);
      if (scheduleEnabled) {
        const boundedExpiry = boundedScheduleInput(scheduleExpiresAt, consent?.expires_at, result.input_artifact.expires_at);
        if (boundedExpiry) setScheduleExpiresAt(boundedExpiry);
        const scheduleRequest = buildScheduleRequest(result, boundedExpiry ?? scheduleExpiresAt);
        if (!scheduleRequest) {
          setError("Preparation was created, but the schedule fields are invalid. Correct them or continue without a schedule.");
          return;
        }
        await runSchedule({ kind: "schedule", request: scheduleRequest }, result);
      } else {
        await continueWithPrep(result);
      }
    } catch (reason) {
      if (!mountedRef.current) return;
      if (isDefinitiveClientError(reason)) setPending(null);
      if (reason instanceof CalendarApiError && STALE_RECONCILIATION_CODES.has(reason.code)) {
        setReconciledAfterStale(false);
        setStaleReconciliationCode(reason.code);
      }
      setError(safeError(reason));
    } finally {
      if (mountedRef.current) setBusy(null);
    }
  };

  const runSchedule = async (intent: Extract<PendingIntent, { kind: "schedule" }>, prep: CalendarPrepResponse = confirmedPrep as CalendarPrepResponse) => {
    if (!prep) {
      setError("The preparation receipt is unavailable; refresh the Work Board before retrying the schedule.");
      return;
    }
    setPending({ intent, confirmedPrep: prep });
    setBusy("schedule");
    try {
      const result = await boundedRequest((signal) => createGovernedSchedule(intent.request, signal));
      if (!mountedRef.current) return;
      setSchedule(result);
      setPending(null);
      setError(null);
      await continueWithPrep(prep);
    } catch (reason) {
      if (!mountedRef.current) return;
      if (isDefinitiveClientError(reason)) setPending(null);
      if (reason instanceof CalendarApiError && STALE_RECONCILIATION_CODES.has(reason.code)) {
        setReconciledAfterStale(false);
        setStaleReconciliationCode(reason.code);
      }
      setError(`Preparation created; schedule not created. ${safeError(reason)}`);
    } finally {
      if (mountedRef.current) setBusy(null);
    }
  };

  const retryPending = async () => {
    if (!pending) return;
    if (pending.intent.kind === "consent") return submitConsent(pending.intent);
    if (pending.intent.kind === "prep") return runPrep(pending.intent);
    if (!confirmedPrep || pending.confirmedPrep?.task.task_id !== confirmedPrep.task.task_id) {
      setError("The preparation receipt is unavailable; refresh the Work Board before retrying the schedule.");
      return;
    }
    return runSchedule(pending.intent, confirmedPrep);
  };

  const startNewReconciledAttempt = () => {
    if (!reconciledAfterStale) return;
    setPending(null);
    setConfirmedPrep(null);
    setSchedule(null);
    setConsent(null);
    consentRef.current = null;
    setEventResponse(null);
    setSelectedEventId("");
    setCalendarListRevision("");
    setSelectedCalendarId("");
    setReconciledAfterStale(false);
    setStaleReconciliationCode(null);
    setError(null);
    setEventsError(null);
    onPendingChange?.(null);
  };

  const requestClose = () => {
    if (pending || busy) {
      setError("An outcome is unconfirmed. Keep this form open and retry the exact request before closing it.");
      return;
    }
    onClose();
  };

  const canRefreshCalendarMetadata = Boolean(
    staleReconciliationCode
    && ["calendar_connection_revision_stale", "calendar_revision_stale"].includes(staleReconciliationCode)
    && selectedConnection,
  );
  const canRefreshEventMetadata = Boolean(
    staleReconciliationCode === "calendar_event_revision_stale"
    && consent,
  );

  const toggleField = (field: CalendarAllowedField, checked: boolean) => {
    if (!checked && REQUIRED_ALLOWED_FIELDS.includes(field)) {
      setError("Summary, start, and end are required and cannot be removed.");
      return;
    }
    setAllowedFields((current) => checked ? [...new Set([...current, field])] : current.filter((item) => item !== field));
  };

  return (
    <div className="fixed inset-0 z-[95] flex items-center justify-center bg-black/70 p-4" role="presentation">
      <form className="max-h-[94vh] w-full max-w-4xl overflow-y-auto rounded border border-white/15 bg-slate-950 p-4 text-slate-100 shadow-2xl" role="dialog" aria-modal="true" aria-labelledby="calendar-prep-title" onSubmit={(event) => { event.preventDefault(); void submitPrep(); }}>
        <div className="flex items-center justify-between gap-2"><div><div className="cockpit-key">bounded calendar read lane</div><h2 id="calendar-prep-title" className="text-lg font-semibold">Prepare for a calendar meeting</h2></div><button type="button" className="cockpit-feedback-button" onClick={requestClose} disabled={Boolean(busy)}>Close</button></div>
        <p className="mt-1 text-xs opacity-75">The form reads only the selected owner-bound calendar after explicit verification and finite consent. Provider identity and private event metadata stay behind the server binding.</p>
        {error && <div className="mt-2 rounded border border-amber-500/40 p-2 text-sm" role="alert">{error}{pending && <button type="button" className="ml-2 underline" onClick={() => void retryPending()}>Retry exact request</button>}{canRefreshCalendarMetadata && <button type="button" className="ml-2 underline" onClick={() => void verify()} disabled={calendarLoading}>Refresh calendar metadata</button>}{canRefreshEventMetadata && <button type="button" className="ml-2 underline" onClick={() => consent && void loadEvents(consent.connection_id, consent)} disabled={eventsLoading}>Refresh event metadata</button>}{reconciledAfterStale && pending && <button type="button" className="ml-2 underline" onClick={startNewReconciledAttempt}>Start a new reconciled attempt</button>}</div>}
        {pending && <div className="mt-2 rounded border border-amber-500/40 p-2 text-sm" role="status">The {pending.intent.kind} request has no confirmed receipt. Its exact key and fields are retained; no automatic replay was made.</div>}
        <div className="mt-3 grid gap-3 sm:grid-cols-2">
          <label>Goal<select className="cockpit-input mt-1 w-full" value={goalId} onChange={(event) => setGoalId(event.currentTarget.value)} disabled={Boolean(pending)}><option value="">Choose an owned goal</option>{goals.map((goal) => <option key={goal.id} value={goal.id}>{goal.title} · revision {goal.revision ?? "unavailable"}</option>)}</select></label>
          <label>Goal revision<input className="cockpit-input mt-1 w-full" value={goalRevision(selectedGoal ?? undefined) ?? "unavailable"} readOnly aria-readonly="true" /></label>
          <label>Connection<select className="cockpit-input mt-1 w-full" value={selectedConnectionId} onChange={(event) => chooseConnection(event.currentTarget.value)} disabled={Boolean(pending)}><option value="">Choose an active connection</option>{connections.map((connection) => <option key={connection.connection_id} value={connection.connection_id} disabled={connection.state !== "active"}>{connection.label} · {connection.state} · revision {connection.revision}</option>)}</select></label>
          <div className="flex items-end gap-2">{connectionLoading ? <span className="text-xs opacity-70">Loading connection metadata…</span> : connectionError ? <span className="text-xs text-amber-200">{connectionError}</span> : <><button type="button" className="cockpit-feedback-button" onClick={() => void verify()} disabled={calendarLoading || Boolean(pending)}>{calendarLoading ? "Verifying…" : "Verify calendars"}</button>{onOpenSettings && <button type="button" className="cockpit-feedback-button" onClick={onOpenSettings}>Open Settings</button>}</>}</div>
          {calendarError && <div className="sm:col-span-2 text-xs text-amber-200" role="alert">{calendarError}</div>}
          <label>Verified calendar<select className="cockpit-input mt-1 w-full" value={selectedCalendarId} onChange={(event) => setSelectedCalendarId(event.currentTarget.value)} disabled={!calendars.length || Boolean(pending)}><option value="">Choose one returned calendar</option>{calendars.map((calendar) => <option key={calendar.calendar_id} value={calendar.calendar_id}>{calendar.summary}</option>)}</select></label>
          <div className="text-xs opacity-70 sm:self-end">{calendars.length ? `${calendars.length} calendars returned · list revision ${calendarListRevision.slice(0, 18)}…` : "Verify the connection to load a bounded list."}</div>
          <fieldset className="sm:col-span-2 rounded border border-white/10 p-2" disabled={Boolean(pending)}><legend className="px-1 text-xs font-semibold">Finite read consent</legend><div className="grid gap-2 sm:grid-cols-2"><label>Window minutes<input className="cockpit-input mt-1 w-full" type="number" min={5} max={1440} value={windowMinutes} onChange={(event) => setWindowMinutes(event.currentTarget.value)} /></label><label>Maximum events<input className="cockpit-input mt-1 w-full" type="number" min={1} max={50} value={maxEvents} onChange={(event) => setMaxEvents(event.currentTarget.value)} /></label><label>Expires at (UTC)<input className="cockpit-input mt-1 w-full" type="datetime-local" value={expiresAt} onChange={(event) => setExpiresAt(event.currentTarget.value)} /></label><label className="flex items-center gap-2 self-end"><input type="checkbox" checked={allowRemoteModel} onChange={(event) => setAllowRemoteModel(event.currentTarget.checked)} />Allow the governed OpenRouter model</label></div><div className="mt-2 flex flex-wrap gap-3 text-xs">{(["summary", "start", "end", "location", "description", "attendees"] as CalendarAllowedField[]).map((field) => <label key={field} className="flex items-center gap-1"><input type="checkbox" checked={allowedFields.includes(field)} disabled={REQUIRED_ALLOWED_FIELDS.includes(field)} onChange={(event) => toggleField(field, event.currentTarget.checked)} />{field}{REQUIRED_ALLOWED_FIELDS.includes(field) ? " (required)" : ""}</label>)}</div><button type="button" className="cockpit-feedback-button mt-2" onClick={() => void createConsent()} disabled={Boolean(busy) || Boolean(pending) || !hasRequiredAllowedFields || !selectedCalendarIsCurrent}>Create finite consent and read events</button></fieldset>
          {consent && <div className={`sm:col-span-2 rounded border p-2 text-xs ${consent.state === "active" ? "border-emerald-500/30" : "border-amber-500/40"}`} role="status">Consent {consent.consent_id} · {consent.state} · revision {consent.revision} · digest {consent.consent_digest} · expires {new Date(consent.expires_at).toLocaleString()}{consent.state !== "active" ? " · refresh consent before continuing" : ""}</div>}
          {eventsError && <div className="sm:col-span-2 text-xs text-amber-200" role="alert">{eventsError}<button type="button" className="ml-2 underline" onClick={() => consent && void loadEvents(consent.connection_id, consent)}>Refresh events</button></div>}
          {eventsLoading && <div className="sm:col-span-2 text-xs opacity-70" role="status">Loading redacted event bindings…</div>}
          {eventResponse && <label className="sm:col-span-2">Event<select aria-label="Event binding" className="cockpit-input mt-1 w-full" value={selectedEventId} onChange={(event) => setSelectedEventId(event.currentTarget.value)} disabled={Boolean(pending)}><option value="">Choose a returned event</option>{eventResponse.events.map((event: CalendarEventOption) => <option key={event.event_binding_id} value={event.event_binding_id}>{event.summary} · {displayEventTime(event.start)} · binding {event.event_binding_id}</option>)}</select><span className="mt-1 block text-[10px] opacity-60">{eventResponse.events.length} events · pages {eventResponse.pages_read} · {eventResponse.truncated ? "more omitted by server" : "bounded list complete"} · returned list revision {eventResponse.calendar_list_revision}{selectedEvent ? ` · selected event revision ${selectedEvent.calendar_list_revision}` : ""}</span></label>}
          <label className="sm:col-span-2">Preparation title<input className="cockpit-input mt-1 w-full" maxLength={MAX_TITLE} value={title} onChange={(event) => setTitle(event.currentTarget.value)} disabled={Boolean(pending)} /></label>
          <fieldset className="sm:col-span-2 rounded border border-white/10 p-2" disabled={Boolean(pending) || Boolean(confirmedPrep)}><legend className="px-1 text-xs font-semibold">Optional governed observation</legend><label className="flex items-center gap-2 text-xs"><input type="checkbox" checked={scheduleEnabled} onChange={(event) => setScheduleEnabled(event.currentTarget.checked)} />Observe this calendar on a finite schedule after preparation</label>{scheduleEnabled && <div className="mt-2 grid gap-2 sm:grid-cols-2"><label>Cadence<select className="cockpit-input mt-1 w-full" value={cadenceKind} onChange={(event) => setCadenceKind(event.currentTarget.value as typeof cadenceKind)}><option value="5min">Every 5 minutes</option><option value="hourly">Hourly</option><option value="6h">Every 6 hours</option><option value="daily">Daily</option></select></label><label>Timezone<input className="cockpit-input mt-1 w-full" maxLength={128} value={timezone} onChange={(event) => setTimezone(event.currentTarget.value)} /></label>{cadenceKind === "daily" && <><label>Daily hour<input className="cockpit-input mt-1 w-full" type="number" min={0} max={23} value={dailyHour} onChange={(event) => setDailyHour(event.currentTarget.value)} /></label><label>Daily minute<input className="cockpit-input mt-1 w-full" type="number" min={0} max={59} value={dailyMinute} onChange={(event) => setDailyMinute(event.currentTarget.value)} /></label></>}<label>Schedule expires at<input className="cockpit-input mt-1 w-full" type="datetime-local" max={scheduleExpiryMax} aria-describedby="calendar-schedule-expiry-limit" value={scheduleExpiresAt} onChange={(event) => setScheduleExpiresAt(event.currentTarget.value)} /><span id="calendar-schedule-expiry-limit" className="mt-1 block text-[10px] opacity-70">Schedule limit: up to 24 hours, and no later than the current consent or preparation artifact expiry ({new Date(scheduleExpiryLimit).toLocaleString()}).</span></label></div>}</fieldset>
        </div>
        {selectedEvent && selectedGoal && goalRevision(selectedGoal) && <CalendarReschedulePanel
          ownerPrincipalId={ownerPrincipalId} ownerSessionId={ownerSessionId}
          eventBindingId={selectedEvent.event_binding_id} eventBindingRevision={selectedEvent.event_binding_revision}
          goalId={selectedGoal.id} goalRevision={goalRevision(selectedGoal)!} goals={goals}
        />}
        {confirmedPrep && <div className="mt-3 rounded border border-emerald-500/30 p-2 text-xs" role="status">Preparation created: task {confirmedPrep.task.task_id} · artifact {confirmedPrep.input_artifact.artifact_id} · digest {confirmedPrep.input_artifact.typed_input_digest}{confirmedPrep.idempotent_replay ? " · exact replay" : ""}{schedule ? ` · schedule ${schedule.binding_id} ${schedule.state}` : scheduleEnabled ? " · schedule not confirmed" : ""}<div className="mt-2 flex gap-2"><button type="button" className="cockpit-feedback-button" onClick={() => void continueWithPrep(confirmedPrep)} disabled={Boolean(pending)}>Open task without schedule</button>{pending?.intent.kind === "schedule" && <button type="button" className="cockpit-feedback-button" onClick={() => void retryPending()} disabled={Boolean(busy)}>Retry schedule</button>}</div></div>}
        <div className="mt-3 flex flex-wrap justify-end gap-2"><button type="button" className="cockpit-feedback-button" onClick={requestClose} disabled={Boolean(busy) || Boolean(pending)}>Cancel</button><button type="submit" className="cockpit-feedback-button" disabled={Boolean(busy) || Boolean(pending) || !consentIsActiveAndFuture || !selectedCalendarIsCurrent || !selectedEventId || !allowRemoteModel || !hasRequiredAllowedFields}>{busy === "prep" ? "Preparing…" : "Prepare meeting"}</button></div>
      </form>
    </div>
  );
}

export { CAPABILITY };
