import { useEffect, useRef, useState } from "react";

import { MailApiError, MailDraftResponse, getMailReplyDraft } from "../../lib/mailApi";

export interface MailPanelProps {
  taskId: string | null;
  ownerPrincipalId?: string | null;
  ownerSessionId?: string | null;
}

function safeError(error: unknown): string {
  if (error instanceof MailApiError) {
    if (error.status === 401) return "The operator session is unavailable. Sign in again before reviewing this private draft.";
    if (error.status === 409) return `${error.message} Refresh the canonical task readback explicitly.`;
    if (error.status >= 400 && error.status < 500) return error.message;
  }
  return "The draft outcome is unconfirmed. Keep the current task selected and refresh canonical readback explicitly.";
}

export function MailPanel({ taskId, ownerPrincipalId, ownerSessionId }: MailPanelProps) {
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
      const result = await getMailReplyDraft(taskId);
      if (!mountedRef.current || generation !== generationRef.current) return;
      setDraft(result);
      setSubject(result.draft?.subject ?? "");
      setPlainbody(result.draft?.plainbody ?? "");
    } catch (reason) {
      if (!mountedRef.current || generation !== generationRef.current) return;
      setUnknown(reason instanceof MailApiError && (reason.status === 0 || reason.status >= 500));
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
