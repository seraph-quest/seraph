import { useEffect, useRef, useState } from "react";

export type PttAudioState =
  | "idle"
  | "requesting_capture"
  | "capturing"
  | "uploading"
  | "processing"
  | "confirming"
  | "reloading"
  | "cancelling"
  | "review"
  | "review_unavailable"
  | "confirmed"
  | "cancelled"
  | "blocked"
  | "degraded"
  | "error";

type AudioSnapshot = {
  request_id: string;
  status: PttAudioState | string;
  error_code?: string | null;
  transcript?: { text?: string; digest?: string | null; confirmed_digest?: string | null };
};

export function choosePttMimeType(): string | undefined {
  if (typeof MediaRecorder === "undefined") return undefined;
  const candidates = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"];
  return candidates.find((candidate) => MediaRecorder.isTypeSupported(candidate));
}

interface PttAudioControlProps {
  sessionId: string | null;
  disabled?: boolean;
  endpoint?: string;
}

export function PttAudioControl({ sessionId, disabled = false, endpoint = "/api/audio/ptt" }: PttAudioControlProps) {
  const controlDisabled = disabled || !sessionId;
  const [state, setState] = useState<PttAudioState>("idle");
  const [captureConsent, setCaptureConsent] = useState(false);
  const [modelConsent, setModelConsent] = useState(false);
  const [captureConsentReference, setCaptureConsentReference] = useState<string | null>(null);
  const [modelConsentReference, setModelConsentReference] = useState<string | null>(null);
  const [transcript, setTranscript] = useState("");
  const [snapshot, setSnapshot] = useState<AudioSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [consentPending, setConsentPending] = useState<"capture" | "model" | null>(null);
  const consentMutationRef = useRef({ capture: 0, model: 0 });
  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const captureGenerationRef = useRef(0);
  const captureActiveRef = useRef(false);
  const actionSequenceRef = useRef(0);
  const actionRef = useRef<{ sequence: number; controller: AbortController } | null>(null);
  const mountedSessionRef = useRef(sessionId);

  const beginAction = () => {
    actionRef.current?.controller.abort();
    const action = {
      sequence: ++actionSequenceRef.current,
      controller: new AbortController(),
    };
    actionRef.current = action;
    return action;
  };

  const isCurrentAction = (sequence: number) => actionRef.current?.sequence === sequence;

  const finishAction = (sequence: number) => {
    if (isCurrentAction(sequence)) actionRef.current = null;
  };

  const setServerConsent = async (boundary: "capture" | "model", enabled: boolean) => {
    if (consentPending) return;
    const currentReference = boundary === "capture" ? captureConsentReference : modelConsentReference;
    const mutation = ++consentMutationRef.current[boundary];
    const isCurrentMutation = () => consentMutationRef.current[boundary] === mutation;
    setConsentPending(boundary);
    setError(null);
    try {
      if (!enabled) {
        if (currentReference) {
          const response = await fetch(`${endpoint}/consent/${encodeURIComponent(currentReference)}/revoke`, { method: "POST" });
          const payload = (await response.json()) as { state?: string; detail?: { code?: string } };
          if (!response.ok || payload.state !== "revoked") {
            throw new Error(payload.detail?.code || "audio_consent_revocation_unconfirmed");
          }
        }
        if (!isCurrentMutation()) return;
        if (boundary === "capture") {
          setCaptureConsentReference(null);
          setCaptureConsent(false);
        } else {
          setModelConsentReference(null);
          setModelConsent(false);
        }
        return;
      }
      const response = await fetch(`${endpoint}/consent`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ boundary }),
      });
      const payload = (await response.json()) as { reference?: string; state?: string; detail?: { code?: string } };
      if (!response.ok || !payload.reference || payload.state !== "active") {
        throw new Error(payload.detail?.code || "audio_consent_unavailable");
      }
      if (!isCurrentMutation()) return;
      if (boundary === "capture") {
        setCaptureConsentReference(payload.reference);
        setCaptureConsent(true);
      } else {
        setModelConsentReference(payload.reference);
        setModelConsent(true);
      }
    } catch (cause) {
      if (!isCurrentMutation()) return;
      // A failed revoke leaves the server grant potentially active, so keep
      // the local consent state active until a later read or retry confirms it.
      if (enabled) {
        if (boundary === "capture") setCaptureConsent(false);
        else setModelConsent(false);
      }
      setError(cause instanceof Error ? cause.message : "audio_consent_unavailable");
    } finally {
      if (isCurrentMutation()) setConsentPending(null);
    }
  };

  const stopStream = () => {
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
  };

  const stopCaptureResources = () => {
    actionRef.current?.controller.abort();
    actionRef.current = null;
    captureActiveRef.current = false;
    captureGenerationRef.current += 1;
    const recorder = recorderRef.current;
    recorderRef.current = null;
    if (recorder) {
      recorder.ondataavailable = null;
      recorder.onstop = null;
      try {
        if (recorder.state !== "inactive") recorder.stop();
      } catch {
        // The browser may already have torn down the recorder during unmount.
      }
    }
    stopStream();
    chunksRef.current = [];
  };

  useEffect(() => stopCaptureResources, []);

  useEffect(() => {
    const sessionChanged = mountedSessionRef.current !== sessionId;
    mountedSessionRef.current = sessionId;
    if (!controlDisabled && !sessionChanged) return;
    // A disabled cockpit (or a switched conversation) must immediately revoke
    // browser-side capture resources and invalidate pending upload responses.
    // The server remains the authority for any already-admitted job.
    stopCaptureResources();
    setSnapshot(null);
    setTranscript("");
    setState("idle");
    setError(null);
  }, [controlDisabled, sessionId]);

  const applySnapshot = (payload: AudioSnapshot) => {
    setSnapshot(payload);
    if (payload.status === "transcript_ready") {
      const reviewText = payload.transcript?.text?.trim();
      if (reviewText && payload.transcript?.digest) {
        setTranscript(reviewText);
        setError(null);
        setState("review");
      } else {
        setTranscript("");
        setState("review_unavailable");
        setError("Transcript review is unavailable. Reload or retry processing.");
      }
    } else if (payload.status === "confirmed") {
      setTranscript("");
      setError(null);
      setState("confirmed");
    } else if (payload.status === "confirming" || payload.status === "confirming_reserved") {
      setError(null);
      setState("confirming");
    } else if (payload.status === "cancelled") {
      setTranscript("");
      setError(null);
      setState("cancelled");
    } else if (payload.status === "blocked") {
      setTranscript("");
      setState("blocked");
      setError(payload.error_code || "audio_processing_blocked");
    } else if (payload.status === "degraded") {
      setTranscript("");
      setState("degraded");
      setError(payload.error_code || "audio_processing_degraded");
    } else if (payload.status === "queued" || payload.status === "processing" || payload.status === "transporting") {
      setTranscript("");
      setError(null);
      setState("processing");
    } else {
      setTranscript("");
      setState("error");
      setError(payload.error_code || "audio_processing_failed");
    }
  };

  const beginCapture = async () => {
    if (controlDisabled || !captureConsent || state !== "idle") return;
    const mimeType = choosePttMimeType();
    if (!mimeType || !navigator.mediaDevices?.getUserMedia) {
      setState("blocked");
      setError("This browser cannot provide a supported audio recorder.");
      return;
    }
    const captureGeneration = ++captureGenerationRef.current;
    captureActiveRef.current = true;
    setError(null);
    setState("requesting_capture");
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (!captureActiveRef.current || captureGenerationRef.current !== captureGeneration) {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      const recorder = new MediaRecorder(stream, { mimeType });
      chunksRef.current = [];
      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) chunksRef.current.push(event.data);
      };
      recorder.onstop = () => {
        stopStream();
        void uploadCapture(new Blob(chunksRef.current, { type: mimeType }));
      };
      streamRef.current = stream;
      recorderRef.current = recorder;
      recorder.start();
      setState("capturing");
    } catch {
      if (!captureActiveRef.current || captureGenerationRef.current !== captureGeneration) return;
      captureActiveRef.current = false;
      stopStream();
      setState("error");
      setError("Microphone capture was unavailable.");
    }
  };

  const endCapture = () => {
    if (!captureActiveRef.current && state !== "capturing") return;
    captureActiveRef.current = false;
    captureGenerationRef.current += 1;
    const recorder = recorderRef.current;
    recorderRef.current = null;
    if (recorder) {
      try {
        if (recorder.state !== "inactive") recorder.stop();
      } catch {
        stopStream();
        setState("error");
        setError("Microphone capture could not be stopped.");
      }
      return;
    }
    stopStream();
    if (state === "requesting_capture") {
      setState("idle");
      setError(null);
    }
  };

  const uploadCapture = async (blob: Blob) => {
    const action = beginAction();
    setState("uploading");
    try {
      if (!captureConsentReference) throw new Error("capture_consent_missing");
      const encoded = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onerror = () => reject(new Error("audio_read_failed"));
        reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
        reader.readAsDataURL(blob);
      });
      if (!isCurrentAction(action.sequence)) return;
      const capturedAt = new Date();
      const body = {
        session_id: sessionId ?? "",
        audio_base64: encoded,
        captured_at: capturedAt.toISOString(),
        capture_consent_reference: captureConsentReference,
        ...(modelConsentReference ? { model_consent_reference: modelConsentReference } : {}),
      };
      const response = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: action.controller.signal,
      });
      const payload = (await response.json()) as AudioSnapshot & { detail?: { code?: string } };
      if (!isCurrentAction(action.sequence)) return;
      if (!response.ok) throw new Error(payload.detail?.code || "audio_upload_failed");
      applySnapshot(payload);
    } catch (cause) {
      if (!isCurrentAction(action.sequence)) return;
      setState("error");
      setError(cause instanceof Error ? cause.message : "audio_upload_failed");
    } finally {
      finishAction(action.sequence);
    }
  };

  const confirmTranscript = async () => {
    if (!snapshot || state !== "review") return;
    const action = beginAction();
    setState("confirming");
    setError(null);
    try {
      const response = await fetch(`${endpoint}/${snapshot.request_id}/confirm`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          transcript,
          expected_transcript_digest: snapshot.transcript?.digest,
        }),
        signal: action.controller.signal,
      });
      if (!isCurrentAction(action.sequence)) return;
      if (!response.ok) {
        setState("review");
        setError("Transcript changed before confirmation.");
        return;
      }
      applySnapshot((await response.json()) as AudioSnapshot);
    } catch (cause) {
      if (!isCurrentAction(action.sequence)) return;
      setState("review");
      setError(cause instanceof Error ? cause.message : "audio_confirmation_failed");
    } finally {
      finishAction(action.sequence);
    }
  };

  const reloadSnapshot = async () => {
    if (!snapshot || state === "reloading") return;
    const action = beginAction();
    setState("reloading");
    setError(null);
    try {
      const response = await fetch(`${endpoint}/${snapshot.request_id}`, { signal: action.controller.signal });
      const payload = (await response.json()) as AudioSnapshot & { detail?: { code?: string } };
      if (!response.ok) throw new Error(payload.detail?.code || "audio_reload_failed");
      if (!isCurrentAction(action.sequence)) return;
      applySnapshot(payload);
    } catch (cause) {
      if (!isCurrentAction(action.sequence)) return;
      setTranscript("");
      setState("review_unavailable");
      setError(cause instanceof Error ? cause.message : "audio_reload_failed");
    } finally {
      finishAction(action.sequence);
    }
  };

  const retryProcessing = async () => {
    if (!snapshot) return;
    const action = beginAction();
    setState("processing");
    setError(null);
    try {
      const response = await fetch(`${endpoint}/${snapshot.request_id}/process`, {
        method: "POST",
        signal: action.controller.signal,
      });
      const payload = (await response.json()) as AudioSnapshot & { detail?: { code?: string } };
      if (!response.ok) throw new Error(payload.detail?.code || "audio_retry_failed");
      if (!isCurrentAction(action.sequence)) return;
      applySnapshot(payload);
    } catch (cause) {
      if (!isCurrentAction(action.sequence)) return;
      setState("review_unavailable");
      setError(cause instanceof Error ? cause.message : "audio_retry_failed");
    } finally {
      finishAction(action.sequence);
    }
  };

  const cancelAudio = async () => {
    if (!snapshot || state === "cancelling") return;
    const previousState = state;
    const action = beginAction();
    setState("cancelling");
    setError(null);
    try {
      const response = await fetch(`${endpoint}/${snapshot.request_id}/cancel`, {
        method: "POST",
        signal: action.controller.signal,
      });
      const payload = (await response.json()) as AudioSnapshot & { detail?: { code?: string } };
      if (!response.ok) throw new Error(payload.detail?.code || "audio_cancel_failed");
      if (!isCurrentAction(action.sequence)) return;
      applySnapshot(payload);
    } catch (cause) {
      if (!isCurrentAction(action.sequence)) return;
      setState(previousState === "review" ? "review" : "review_unavailable");
      setError(cause instanceof Error ? cause.message : "audio_cancel_failed");
    } finally {
      finishAction(action.sequence);
    }
  };

  return (
    <section className="flex flex-col gap-2 mt-2" aria-label="Push to talk">
      <div className="flex items-center gap-3 text-[10px] font-pixel uppercase">
        <label className="flex items-center gap-1">
          <input type="checkbox" checked={captureConsent} onChange={(event) => void setServerConsent("capture", event.target.checked)} disabled={controlDisabled || state !== "idle" || consentPending !== null} />
          Allow microphone capture
        </label>
        <label className="flex items-center gap-1">
          <input type="checkbox" checked={modelConsent} onChange={(event) => void setServerConsent("model", event.target.checked)} disabled={controlDisabled || state !== "idle" || consentPending !== null} />
          Allow model processing
        </label>
      </div>
      <button
        type="button"
        disabled={controlDisabled || consentPending !== null || (!captureConsent && state === "idle") || ["uploading", "processing", "confirming", "reloading", "cancelling", "requesting_capture", "review", "review_unavailable", "confirmed", "cancelled"].includes(state)}
        onPointerDown={() => void beginCapture()}
        onPointerUp={endCapture}
        onPointerCancel={endCapture}
        onKeyDown={(event) => { if (!event.repeat && (event.key === " " || event.key === "Enter")) void beginCapture(); }}
        onKeyUp={(event) => { if (event.key === " " || event.key === "Enter") endCapture(); }}
        className="pixel-border-thin px-3 py-2 font-pixel text-[10px] uppercase disabled:opacity-40"
      >
        {state === "capturing" ? "Release to stop" : "Hold to talk"}
      </button>
      {snapshot && ["review", "review_unavailable", "confirming", "reloading", "cancelling"].includes(state) && (
        <div className="flex gap-2 items-start">
          <textarea aria-label="Editable transcript" value={transcript} onChange={(event) => setTranscript(event.target.value)} className="flex-1 bg-retro-bg pixel-border-thin p-2 text-xs" />
          <div className="flex flex-col gap-2">
            <button type="button" onClick={() => void confirmTranscript()} disabled={state !== "review"} className="pixel-border-thin px-2 py-1 font-pixel text-[10px]">Confirm</button>
            <button type="button" onClick={() => void reloadSnapshot()} disabled={state === "reloading"} className="pixel-border-thin px-2 py-1 font-pixel text-[10px]">Reload review</button>
            <button type="button" onClick={() => void cancelAudio()} disabled={state === "cancelling"} className="pixel-border-thin px-2 py-1 font-pixel text-[10px]">Cancel</button>
          </div>
        </div>
      )}
      {snapshot && ["confirming", "reloading", "cancelling"].includes(state) && (
        <div className="pixel-border-thin p-2 text-xs" role="status">
          {state === "confirming" ? "Confirming transcript…" : state === "reloading" ? "Reloading transcript review…" : "Cancelling audio…"}
        </div>
      )}
      {snapshot && state === "review_unavailable" && (
        <div className="flex flex-col gap-2 pixel-border-thin p-2 text-xs">
          <span>Transcript text is unavailable in this worker. Reload the review or retry processing.</span>
          <div className="flex gap-2">
            <button type="button" onClick={() => void reloadSnapshot()} className="pixel-border-thin px-2 py-1 font-pixel text-[10px]">Reload review</button>
            <button type="button" onClick={() => void retryProcessing()} className="pixel-border-thin px-2 py-1 font-pixel text-[10px]">Retry processing</button>
            <button type="button" onClick={() => void cancelAudio()} className="pixel-border-thin px-2 py-1 font-pixel text-[10px]">Cancel</button>
          </div>
        </div>
      )}
      <span role="status" className="font-pixel text-[10px]" data-state={state}>{!sessionId ? "Select a conversation before recording." : error || state}</span>
    </section>
  );
}
