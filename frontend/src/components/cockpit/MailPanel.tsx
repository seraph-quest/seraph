import { useCallback, useEffect, useRef, useState } from "react";

import {
  createMailReplyTask,
  getMailReplyRecovery,
  getMailReplyDraft,
  getMailWatch,
  listMailConnections,
  listMailConsents,
  makeMailIdempotencyKey,
  MailApiError,
  readMailMessage,
  withMailDeadline as bounded,
} from "../../lib/mailApi";
import type {
  MailConnectionMetadata,
  MailConsentMetadata,
  MailDraftResponse,
  MailMessageReadResponse,
  MailWatchMetadata,
} from "../../lib/mailApi";
import type { GuardianInboxMailOrigin } from "../../types";

export interface MailPanelProps {
  taskId: string | null;
  ownerPrincipalId?: string | null;
  ownerSessionId?: string | null;
  /** Detail-only Inbox origin; no source body or provider identity is accepted. */
  mailOrigin?: GuardianInboxMailOrigin | null;
  goalId?: string | null;
  goalRevision?: number | null;
}

function safeError(error: unknown): string {
  if (error instanceof MailApiError) {
    if (error.status === 401) return "The operator session is unavailable. Sign in again before reviewing this private draft.";
    if (error.status === 409) return `${error.message} Refresh the canonical task readback explicitly.`;
    if (error.status >= 400 && error.status < 500) return error.message;
  }
  return "The draft outcome is unconfirmed. Keep the current task selected and refresh canonical readback explicitly.";
}

function privateError(error: unknown): string {
  if (error instanceof MailApiError) {
    if (error.status === 401) return "The operator session is unavailable. Sign in again before reading private Mail.";
    if (error.status === 409) return `${error.message} Refresh the current Mail authority explicitly.`;
    if (error.status >= 400 && error.status < 500) return error.message;
  }
  return "The private Mail outcome is unconfirmed. Keep the exact selection and reconcile it explicitly.";
}

function isUnknown(error: unknown): boolean {
  return error instanceof MailApiError && (
    error.status === 0
    || error.status >= 500
    || error.status === 200
  );
}

function exactSourceContext(
  connections: MailConnectionMetadata[],
  consents: MailConsentMetadata[],
  sourceWatch: MailWatchMetadata,
  goalId: string,
  goalRevision: number,
): { connection: MailConnectionMetadata; consent: MailConsentMetadata; watch: MailWatchMetadata } | null {
  const now = Date.now();
  if (
    sourceWatch.state !== "active"
    || Date.parse(sourceWatch.expires_at) <= now
    || sourceWatch.goal_id !== goalId
    || sourceWatch.goal_revision !== goalRevision
    || !sourceWatch.connection_id
    || sourceWatch.connection_revision === null
    || !sourceWatch.mail_consent_id
    || sourceWatch.source_consent_revision === null
  ) return null;
  const connection = connections.find((item) => (
    item.connection_id === sourceWatch.connection_id
    && item.revision === sourceWatch.connection_revision
    && item.state === "active"
    && item.provider_scopes_verified
  ));
  if (!connection) return null;
  const consent = consents.find((item) => (
    item.consent_id === sourceWatch.mail_consent_id
    && item.connection_id === connection.connection_id
    && item.connection_revision === connection.revision
    && item.source_revision === sourceWatch.source_consent_revision
    && item.goal_id === goalId
    && item.goal_revision === goalRevision
    && item.state === "active"
    && item.source_read_allowed
    && Date.parse(item.expires_at) > now
  ));
  return consent ? { connection, consent, watch: sourceWatch } : null;
}

const REPLY_PENDING_PREFIX = "seraph:mail-reply-recovery:v1:";

interface PendingMailReply {
  version: 1;
  idempotencyKey: string;
}

function replyStorageKey(ownerScope: string | null, messageBindingId: string): string | null {
  if (!ownerScope) return null;
  return `${REPLY_PENDING_PREFIX}${encodeURIComponent(`${ownerScope}\u0000${messageBindingId}`)}`;
}

function readPendingReply(ownerScope: string | null, messageBindingId: string): PendingMailReply | null {
  const key = replyStorageKey(ownerScope, messageBindingId);
  if (!key || typeof window === "undefined") return null;
  try {
    const raw = window.sessionStorage.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<PendingMailReply>;
    return parsed.version === 1 && typeof parsed.idempotencyKey === "string" && parsed.idempotencyKey.length > 0
      ? { version: 1, idempotencyKey: parsed.idempotencyKey }
      : null;
  } catch {
    return null;
  }
}

function writePendingReply(ownerScope: string | null, messageBindingId: string, pending: PendingMailReply): boolean {
  const key = replyStorageKey(ownerScope, messageBindingId);
  if (!key || typeof window === "undefined") return false;
  try {
    const serialized = JSON.stringify(pending);
    window.sessionStorage.setItem(key, serialized);
    return window.sessionStorage.getItem(key) === serialized;
  } catch {
    // The server remains authoritative; an unavailable browser store only
    return false;
  }
}

function clearPendingReply(ownerScope: string | null, messageBindingId: string): void {
  const key = replyStorageKey(ownerScope, messageBindingId);
  if (!key || typeof window === "undefined") return;
  try {
    window.sessionStorage.removeItem(key);
  } catch {
    // Ignore storage cleanup failures; no credential or source content is in it.
  }
}

function DraftPanel({ taskId, ownerPrincipalId, ownerSessionId }: Omit<MailPanelProps, "mailOrigin" | "goalId" | "goalRevision">) {
  const ownerScope = ownerPrincipalId && ownerSessionId ? `${ownerPrincipalId}\u0000${ownerSessionId}` : null;
  const mountedRef = useRef(true);
  const generationRef = useRef(0);
  const [draft, setDraft] = useState<MailDraftResponse | null>(null);
  const [subject, setSubject] = useState("");
  const [plainbody, setPlainbody] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [unknown, setUnknown] = useState(false);
  const [copyStatus, setCopyStatus] = useState<string | null>(null);

  const loadDraft = async () => {
    if (!taskId || !ownerScope) {
      setDraft(null);
      setSubject("");
      setPlainbody("");
      setError(ownerScope ? "Select a Mail reply task to inspect its private draft." : "Sign in through the operator session before opening private Mail work.");
      return;
    }
    const generation = generationRef.current + 1;
    generationRef.current = generation;
    setLoading(true);
    setError(null);
    setUnknown(false);
    setCopyStatus(null);
    try {
      const result = await bounded((signal) => getMailReplyDraft(taskId, signal));
      if (result.task_id !== taskId) {
        throw new MailApiError(200, "draft_origin_mismatch", "The draft readback did not match the selected task.", "refresh_task");
      }
      if (result.status === "verified" && !result.draft) {
        throw new MailApiError(200, "draft_receipt_invalid", "The verified draft readback did not include a local draft.", "refresh_task");
      }
      if (!mountedRef.current || generation !== generationRef.current) return;
      setDraft(result);
      setSubject(result.draft?.subject ?? "");
      setPlainbody(result.draft?.plainbody ?? "");
    } catch (reason) {
      if (!mountedRef.current || generation !== generationRef.current) return;
      setUnknown(reason instanceof MailApiError && (reason.status === 0 || reason.status >= 500 || reason.status === 200));
      setError(safeError(reason));
    } finally {
      if (mountedRef.current && generation === generationRef.current) setLoading(false);
    }
  };

  useEffect(() => {
    mountedRef.current = true;
    generationRef.current += 1;
    setDraft(null);
    setSubject("");
    setPlainbody("");
    setError(null);
    setUnknown(false);
    setCopyStatus(null);
    void loadDraft();
    return () => {
      mountedRef.current = false;
      generationRef.current += 1;
    };
    // The task and authenticated owner are the only identity inputs. The
    // handler is intentionally invoked on selection/remount, never on a
    // transport error or a server-unknown response.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId, ownerScope]);

  const copyDraft = async () => {
    if (!draft?.draft) return;
    try {
      await navigator.clipboard.writeText(`${subject}\n\n${plainbody}`);
      setCopyStatus("Copied locally. Nothing was sent or saved to the provider.");
    } catch {
      setCopyStatus("Clipboard access was unavailable; the draft remains editable locally.");
    }
  };

  if (!taskId) return null;

  return (
    <section className="rounded border border-cyan-500/30 bg-cyan-950/10 p-3" aria-label="Private Mail draft review">
      <div className="flex flex-wrap items-center justify-between gap-2"><div><div className="font-semibold">Private Mail draft</div><div className="text-[10px] text-retro-text/60">Task {taskId} · owner-scoped readback · no send capability</div></div><button type="button" className="cockpit-feedback-button" onClick={() => void loadDraft()} disabled={loading}>{loading ? "Refreshing…" : "Refresh draft readback"}</button></div>
      {error && <div className="mt-2 rounded border border-amber-500/40 p-2 text-[10px]" role="alert">{error}{unknown ? " The original task identity is retained; no replacement request was created." : ""}</div>}
      {draft?.status === "pending" && <div className="mt-2 rounded border border-amber-500/40 p-2 text-[10px]" role="status">Draft execution is still pending. {draft.recovery_action ?? "Refresh canonical readback when ready."}</div>}
      {draft?.status === "blocked" && <div className="mt-2 rounded border border-amber-500/40 p-2 text-[10px]" role="status">Draft execution is blocked. {draft.recovery_action ?? "Reconcile the existing task before any new request."}</div>}
      {draft?.status === "verified" && draft.draft && <div className="mt-2 grid gap-2"><label className="text-[10px]">Subject<input className="cockpit-input mt-1 w-full" maxLength={500} value={subject} onChange={(event) => setSubject(event.currentTarget.value)} /></label><label className="text-[10px]">Plain-text draft<textarea className="cockpit-input mt-1 w-full" rows={8} maxLength={64 * 1024} value={plainbody} onChange={(event) => setPlainbody(event.currentTarget.value)} /></label>{draft.draft.caveats.length > 0 && <div className="text-[10px] text-amber-200">Caveats: {draft.draft.caveats.join(" · ")}</div>}<div className="flex flex-wrap gap-2"><button type="button" className="cockpit-feedback-button" onClick={() => void copyDraft()}>Copy local draft</button><span className="text-[10px] text-retro-text/60 self-center">Verified local artifact · sent: no · provider draft: no · memory: no learning</span></div>{copyStatus && <div className="text-[10px]" role="status">{copyStatus}</div>}</div>}
    </section>
  );
}

interface PrivateMailReviewProps {
  ownerPrincipalId?: string | null;
  ownerSessionId?: string | null;
  origin: GuardianInboxMailOrigin;
  goalId?: string | null;
  goalRevision?: number | null;
}

function PrivateMailReview({ ownerPrincipalId, ownerSessionId, origin, goalId, goalRevision }: PrivateMailReviewProps) {
  const ownerScope = ownerPrincipalId && ownerSessionId ? `${ownerPrincipalId}\u0000${ownerSessionId}` : null;
  const mountedRef = useRef(true);
  const generationRef = useRef(0);
  const [context, setContext] = useState<{ connection: MailConnectionMetadata; consent: MailConsentMetadata; watch: MailWatchMetadata } | null>(null);
  const [contextLoading, setContextLoading] = useState(false);
  const [contextError, setContextError] = useState<string | null>(null);
  const [message, setMessage] = useState<MailMessageReadResponse | null>(null);
  const [readAcknowledged, setReadAcknowledged] = useState(false);
  const [readBusy, setReadBusy] = useState(false);
  const [readError, setReadError] = useState<string | null>(null);
  const [replyIntent, setReplyIntent] = useState("");
  const [replyStyle, setReplyStyle] = useState<"brief" | "formal">("brief");
  const [replyBusy, setReplyBusy] = useState(false);
  const [replyError, setReplyError] = useState<string | null>(null);
  const [replyTaskId, setReplyTaskId] = useState<string | null>(null);
  const [pendingReply, setPendingReply] = useState<PendingMailReply | null>(null);

  const loadContext = useCallback(async () => {
    const generation = generationRef.current + 1;
    generationRef.current = generation;
    setContext(null);
    setMessage(null);
    setReplyTaskId(null);
    setContextError(null);
    setReadError(null);
    setReplyError(null);
    setReadAcknowledged(false);
    if (!ownerScope || !goalId || !Number.isSafeInteger(goalRevision) || !origin.watch_id || !origin.message_binding_id || !origin.message_revision) {
      setPendingReply(null);
      setContextError("The selected Mail origin is incomplete or the operator session is unavailable. Refresh the accepted task.");
      return;
    }
    setPendingReply(readPendingReply(ownerScope, origin.message_binding_id));
    setContextLoading(true);
    const controller = new AbortController();
    try {
      const [connections, consentResult, sourceWatch] = await Promise.all([
        bounded((signal) => listMailConnections(signal), controller.signal),
        bounded((signal) => listMailConsents(undefined, signal), controller.signal),
        bounded((signal) => getMailWatch(origin.watch_id, signal), controller.signal),
      ]);
      if (!mountedRef.current || generation !== generationRef.current || controller.signal.aborted) return;
      const match = exactSourceContext(connections, consentResult.consents, sourceWatch, goalId, goalRevision as number);
      if (!match) {
        setContextError("The exact source watch, connection, consent, or Goal revision is no longer active. Private review is blocked until the accepted task is refreshed.");
        return;
      }
      setContext(match);
    } catch (error) {
      if (mountedRef.current && generation === generationRef.current && !(error instanceof DOMException && error.name === "AbortError")) {
        setContextError(privateError(error));
      }
    } finally {
      controller.abort();
      if (mountedRef.current && generation === generationRef.current) setContextLoading(false);
    }
  }, [goalId, goalRevision, origin.watch_id, origin.message_binding_id, origin.message_revision, ownerScope]);

  useEffect(() => {
    mountedRef.current = true;
    void loadContext();
    return () => {
      mountedRef.current = false;
      generationRef.current += 1;
    };
  }, [loadContext]);

  const readPrivateMessage = async () => {
    setReadError(null);
    if (!context || !readAcknowledged || !goalId || !Number.isSafeInteger(goalRevision) || context.watch.state !== "active" || Date.parse(context.watch.expires_at) <= Date.now()) {
      setReadError("Confirm the one-time bounded read of this selected message after current Mail authority is loaded.");
      return;
    }
    const generation = generationRef.current;
    setReadBusy(true);
    try {
      const result = await bounded((signal) => readMailMessage(origin.message_binding_id, {
        connection_id: context.connection.connection_id,
        expected_connection_revision: context.connection.revision,
        mail_consent_id: context.consent.consent_id,
        expected_source_consent_revision: context.consent.source_revision,
        message_binding_id: origin.message_binding_id,
        expected_message_revision: origin.message_revision,
        acknowledge_selected_body_read: true,
        request_uuid: makeMailIdempotencyKey("gmail-inbox-body"),
      }, signal));
      if (
        result.source_binding_id !== origin.message_binding_id
        || result.message_revision !== origin.message_revision
        || result.provenance.connection_id !== context.connection.connection_id
        || result.provenance.connection_revision !== context.connection.revision
        || result.provenance.consent_id !== context.consent.consent_id
        || result.provenance.source_consent_revision !== context.consent.source_revision
        || result.provenance.egress !== "local_only"
        || result.provenance.memory_status !== "no_learning"
        || result.provenance.connection_id !== context.watch.connection_id
        || result.provenance.connection_revision !== context.watch.connection_revision
        || result.provenance.consent_id !== context.watch.mail_consent_id
        || result.provenance.source_consent_revision !== context.watch.source_consent_revision
      ) {
        throw new MailApiError(200, "mail_origin_mismatch", "The private read did not match the accepted Mail origin. Refresh the task.", "refresh_task");
      }
      if (!mountedRef.current || generation !== generationRef.current) return;
      setMessage(result);
      setReadAcknowledged(false);
    } catch (error) {
      if (mountedRef.current && generation === generationRef.current) setReadError(privateError(error));
    } finally {
      if (mountedRef.current && generation === generationRef.current) setReadBusy(false);
    }
  };

  const submitReply = async () => {
    setReplyError(null);
    if (!context || !message || !goalId || !Number.isSafeInteger(goalRevision)) {
      setReplyError("Read the selected private message and confirm current Mail authority before requesting a draft.");
      return;
    }
    if (!context.consent.model_egress_allowed) {
      setReplyError("Model consent is not active for this current source consent. Grant it explicitly in Mail settings before requesting a draft.");
      return;
    }
    const intent = replyIntent.trim().slice(0, 2000);
    if (!intent) {
      setReplyError("Enter the reply intent to admit one bounded local draft task.");
      return;
    }
    if (pendingReply) {
      setReplyError("The previous reply admission has no confirmed outcome. Refresh the accepted task and reconcile the original request before trying again.");
      return;
    }
    const idempotencyKey = makeMailIdempotencyKey("gmail-reply");
    const generation = generationRef.current;
    const snapshot = {
      ownerScope,
      goalId,
      goalRevision,
      connectionId: context.connection.connection_id,
      connectionRevision: context.connection.revision,
      consentId: context.consent.consent_id,
      sourceRevision: context.consent.source_revision,
      messageBindingId: origin.message_binding_id,
      messageRevision: origin.message_revision,
    };
    const pending = { version: 1 as const, idempotencyKey };
    if (!writePendingReply(ownerScope, origin.message_binding_id, pending)) {
      setReplyError("Browser recovery storage is unavailable. The reply admission is blocked until its opaque request key can be retained.");
      return;
    }
    setPendingReply(pending);
    setReplyBusy(true);
    try {
      const result = await bounded((signal) => createMailReplyTask({
        schema_version: 1,
        connection_id: context.connection.connection_id,
        expected_connection_revision: context.connection.revision,
        message_binding_id: origin.message_binding_id,
        expected_message_revision: origin.message_revision,
        mail_consent_id: context.consent.consent_id,
        expected_source_consent_revision: context.consent.source_revision,
        expected_model_consent_revision: context.consent.model_revision,
        goal_id: goalId,
        expected_goal_revision: goalRevision as number,
        reply_intent: intent,
        style: replyStyle,
        idempotency_key: idempotencyKey,
      }, signal));
      const sameSnapshot = generation === generationRef.current
        && ownerScope === snapshot.ownerScope
        && goalId === snapshot.goalId
        && goalRevision === snapshot.goalRevision
        && context.connection.connection_id === snapshot.connectionId
        && context.connection.revision === snapshot.connectionRevision
        && context.consent.consent_id === snapshot.consentId
        && context.consent.source_revision === snapshot.sourceRevision
        && origin.message_binding_id === snapshot.messageBindingId
        && origin.message_revision === snapshot.messageRevision;
      if (!mountedRef.current || !sameSnapshot) return;
      if (result.goal_id !== goalId || result.goal_revision !== goalRevision
        || (result.message_key !== null && result.message_key !== message.message_key)
        || (result.message_revision !== null && result.message_revision !== message.message_revision)) {
        throw new MailApiError(200, "reply_origin_mismatch", "The reply receipt did not match the selected Mail origin.", "reconcile_reply");
      }
      if (result.status === "accepted" || result.status === "replayed") {
        clearPendingReply(ownerScope, origin.message_binding_id);
        setPendingReply(null);
        setReplyTaskId(result.task_id);
        setReplyError(null);
      } else if (result.status === "unknown") {
        setReplyError("The reply admission outcome is unknown. The original request key is retained; refresh the task before any retry.");
      } else {
        clearPendingReply(ownerScope, origin.message_binding_id);
        setPendingReply(null);
        setReplyError("The server blocked this reply admission. Refresh current Mail and Goal authority before correcting it.");
      }
    } catch (error) {
      if (!mountedRef.current || generation !== generationRef.current) return;
      if (!isUnknown(error)) {
        clearPendingReply(ownerScope, origin.message_binding_id);
        setPendingReply(null);
      }
      setReplyError(isUnknown(error)
        ? "The reply admission outcome is unknown. The original request key is retained; refresh the task before any retry."
        : privateError(error));
    } finally {
      if (mountedRef.current && generation === generationRef.current) setReplyBusy(false);
    }
  };

  const reconcileReply = async () => {
    setReplyError(null);
    if (!pendingReply || !ownerScope || !goalId || !Number.isSafeInteger(goalRevision)) {
      setReplyError("There is no owner-scoped reply request to reconcile. Refresh the accepted task before trying again.");
      return;
    }
    const generation = generationRef.current;
    const key = pendingReply.idempotencyKey;
    setReplyBusy(true);
    try {
      const result = await bounded((signal) => getMailReplyRecovery(key, signal));
      if (!mountedRef.current || generation !== generationRef.current) return;
      if (result.idempotency_key !== key || (result.goal_id !== null && result.goal_id !== goalId) || (result.goal_revision !== null && result.goal_revision !== goalRevision)) {
        throw new MailApiError(200, "reply_recovery_mismatch", "The reply recovery readback did not match the selected Goal or request.", "refresh_task");
      }
      if (result.status === "verified" && result.task_id) {
        clearPendingReply(ownerScope, origin.message_binding_id);
        setPendingReply(null);
        setReplyTaskId(result.task_id);
        setReplyError(null);
      } else if (result.status === "blocked") {
        clearPendingReply(ownerScope, origin.message_binding_id);
        setPendingReply(null);
        setReplyError("The original reply request is canonically blocked. Refresh Mail authority before correcting it.");
      } else {
        setReplyError("The original reply request is still unresolved. Its exact key is retained; no replacement request was created.");
      }
    } catch {
      if (!mountedRef.current || generation !== generationRef.current) return;
      setReplyError("The original reply request could not be confirmed. Its exact key is retained; refresh and reconcile it explicitly.");
    } finally {
      if (mountedRef.current && generation === generationRef.current) setReplyBusy(false);
    }
  };

  return (
    <section className="rounded border border-cyan-500/30 bg-cyan-950/10 p-3" aria-label="Private Mail source review">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div><div className="font-semibold">Private Mail source review</div><div className="text-[10px] text-retro-text/60">Accepted task origin · message {origin.message_binding_id} · no generic Inbox body</div></div>
        <button type="button" className="cockpit-feedback-button" onClick={() => void loadContext()} disabled={contextLoading}>{contextLoading ? "Refreshing…" : "Refresh Mail authority"}</button>
      </div>
      <div className="mt-2 text-[10px] text-retro-text/60">The accepted candidate supplies only an opaque message binding and revision. Current owner/session, connection, consent, Goal, and model permissions are read again before each operation.</div>
      {contextError && <div className="mt-2 rounded border border-amber-500/40 p-2 text-[10px]" role="alert">{contextError}</div>}
      {context && <div className="mt-2 rounded border border-white/10 p-2 text-[10px]">Source consent {context.consent.consent_id} · source revision {context.consent.source_revision} · Goal {context.consent.goal_id} rev {context.consent.goal_revision} · model {context.consent.model_egress_allowed ? `allowed rev ${context.consent.model_revision}` : "not granted"}</div>}
      {context && !message && <div className="mt-2 rounded border border-amber-500/30 p-2 text-[10px]"><div>Message metadata is private and remains unread until you acknowledge one bounded body read.</div><label className="mt-2 flex items-start gap-2"><input type="checkbox" checked={readAcknowledged} onChange={(event) => setReadAcknowledged(event.currentTarget.checked)} />I acknowledge this selected message may be read once within the current owner-scoped Mail consent.</label>{readError && <div className="mt-2 text-amber-300" role="alert">{readError}</div>}<button type="button" className="cockpit-feedback-button mt-2" disabled={readBusy || !readAcknowledged} onClick={() => void readPrivateMessage()}>{readBusy ? "Reading one message…" : "Read selected private message"}</button></div>}
      {message && <article className="mt-2 rounded border border-emerald-500/30 p-2" aria-label="Selected private Mail message"><div className="font-semibold">{message.subject}</div><div className="mt-2 whitespace-pre-wrap break-words">{message.plain_text}</div>{message.truncated && <div className="mt-2 text-amber-300">The bounded body was truncated by the server.</div>}<div className="mt-2 text-[9px] text-retro-text/50">Explicit local read · no learning · attachments and links were not fetched.</div></article>}
      {message && !replyTaskId && <div className="mt-2 rounded border border-white/10 p-2"><div className="font-semibold text-[10px]">Request a local reply draft</div>{!context?.consent.model_egress_allowed ? <div className="mt-1 text-[10px] text-amber-200">Model consent is not active. Use Mail settings to grant the exact subject/plainbody/replyintent fields before this action becomes available.</div> : <><label className="mt-2 block text-[10px]">Reply intent<textarea className="cockpit-input mt-1 w-full" rows={3} maxLength={2000} value={replyIntent} onChange={(event) => setReplyIntent(event.currentTarget.value)} placeholder="Describe the reply you want drafted; this is sent only through the governed local draft task." /></label><label className="mt-2 block text-[10px]">Style<select className="cockpit-input mt-1 w-full" value={replyStyle} onChange={(event) => setReplyStyle(event.currentTarget.value as "brief" | "formal")}><option value="brief">Brief</option><option value="formal">Formal</option></select></label>{!pendingReply && <button type="button" className="cockpit-feedback-button mt-2" disabled={replyBusy} onClick={() => void submitReply()}>{replyBusy ? "Admitting local draft…" : "Request local reply draft"}</button>}</>}</div>}
      {replyError && <div className="mt-2 rounded border border-amber-500/40 p-2 text-[10px]" role="alert">{replyError}</div>}
      {pendingReply && <div className="mt-2 text-[10px] text-amber-200" role="status">A reply admission is pending reconciliation. The original opaque request key is retained; no replacement task will be created.</div>}
      {pendingReply && !replyTaskId && <button type="button" className="cockpit-feedback-button mt-2" disabled={replyBusy} onClick={() => void reconcileReply()}>{replyBusy ? "Reconciling original draft…" : "Reconcile original draft"}</button>}
      {replyTaskId && <div className="mt-2"><DraftPanel taskId={replyTaskId} ownerPrincipalId={ownerPrincipalId} ownerSessionId={ownerSessionId} /></div>}
    </section>
  );
}

export function MailPanel({ taskId, ownerPrincipalId, ownerSessionId, mailOrigin, goalId, goalRevision }: MailPanelProps) {
  if (mailOrigin) {
    return <PrivateMailReview origin={mailOrigin} ownerPrincipalId={ownerPrincipalId} ownerSessionId={ownerSessionId} goalId={goalId} goalRevision={goalRevision} />;
  }
  return <DraftPanel taskId={taskId} ownerPrincipalId={ownerPrincipalId} ownerSessionId={ownerSessionId} />;
}
