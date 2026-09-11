import { useRef, useState } from "react";

export type PttAudioState =
  | "idle"
  | "requesting_capture"
  | "capturing"
  | "uploading"
  | "review"
  | "confirmed"
  | "blocked"
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
  sessionId: string;
  disabled?: boolean;
  endpoint?: string;
}

function consentReference(prefix: string): string {
  const generated = typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${prefix}-${Date.now()}`;
  return `${prefix}-${generated}`.slice(0, 120);
}

export function PttAudioControl({ sessionId, disabled = false, endpoint = "/api/audio/ptt" }: PttAudioControlProps) {
  const [state, setState] = useState<PttAudioState>("idle");
  const [captureConsent, setCaptureConsent] = useState(false);
  const [modelConsent, setModelConsent] = useState(false);
  const [transcript, setTranscript] = useState("");
  const [snapshot, setSnapshot] = useState<AudioSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Blob[]>([]);

  const stopStream = () => {
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
  };

  const beginCapture = async () => {
    if (disabled || !captureConsent || state !== "idle") return;
    const mimeType = choosePttMimeType();
    if (!mimeType || !navigator.mediaDevices?.getUserMedia) {
      setState("blocked");
      setError("This browser cannot provide a supported audio recorder.");
      return;
    }
    setError(null);
    setState("requesting_capture");
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
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
      stopStream();
      setState("error");
      setError("Microphone capture was unavailable.");
    }
  };

  const endCapture = () => {
    if (state !== "capturing") return;
    recorderRef.current?.stop();
    recorderRef.current = null;
  };

  const uploadCapture = async (blob: Blob) => {
    setState("uploading");
    try {
      const encoded = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onerror = () => reject(new Error("audio_read_failed"));
        reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
        reader.readAsDataURL(blob);
      });
      const capturedAt = new Date();
      const body = {
        session_id: sessionId,
        audio_base64: encoded,
        captured_at: capturedAt.toISOString(),
        capture_consent_reference: consentReference("capture"),
        capture_consent_expires_at: new Date(capturedAt.getTime() + 15 * 60_000).toISOString(),
        ...(modelConsent
          ? {
              model_consent_reference: consentReference("model"),
              model_consent_expires_at: new Date(capturedAt.getTime() + 15 * 60_000).toISOString(),
            }
          : {}),
      };
      const response = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const payload = (await response.json()) as AudioSnapshot & { detail?: { code?: string } };
      if (!response.ok) throw new Error(payload.detail?.code || "audio_upload_failed");
      setSnapshot(payload);
      if (payload.status === "transcript_ready") {
        setTranscript(payload.transcript?.text || "");
        setState("review");
      } else if (payload.status === "blocked") {
        setState("blocked");
        setError(payload.error_code || "audio_processing_blocked");
      } else {
        setState("error");
        setError(payload.error_code || "audio_processing_failed");
      }
    } catch (cause) {
      setState("error");
      setError(cause instanceof Error ? cause.message : "audio_upload_failed");
    }
  };

  const confirmTranscript = async () => {
    if (!snapshot || state !== "review") return;
    const response = await fetch(`${endpoint}/${snapshot.request_id}/confirm`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        transcript,
        expected_transcript_digest: snapshot.transcript?.digest,
      }),
    });
    if (!response.ok) {
      setState("error");
      setError("Transcript changed before confirmation.");
      return;
    }
    setSnapshot((await response.json()) as AudioSnapshot);
    setState("confirmed");
  };

  return (
    <section className="flex flex-col gap-2 mt-2" aria-label="Push to talk">
      <div className="flex items-center gap-3 text-[10px] font-pixel uppercase">
        <label className="flex items-center gap-1">
          <input type="checkbox" checked={captureConsent} onChange={(event) => setCaptureConsent(event.target.checked)} disabled={disabled || state !== "idle"} />
          Allow microphone capture
        </label>
        <label className="flex items-center gap-1">
          <input type="checkbox" checked={modelConsent} onChange={(event) => setModelConsent(event.target.checked)} disabled={disabled || state !== "idle"} />
          Allow model processing
        </label>
      </div>
      <button
        type="button"
        disabled={disabled || (!captureConsent && state === "idle") || ["uploading", "requesting_capture", "review", "confirmed"].includes(state)}
        onPointerDown={() => void beginCapture()}
        onPointerUp={endCapture}
        onPointerCancel={endCapture}
        onKeyDown={(event) => { if (event.key === " " || event.key === "Enter") void beginCapture(); }}
        onKeyUp={(event) => { if (event.key === " " || event.key === "Enter") endCapture(); }}
        className="pixel-border-thin px-3 py-2 font-pixel text-[10px] uppercase disabled:opacity-40"
      >
        {state === "capturing" ? "Release to stop" : "Hold to talk"}
      </button>
      {state === "review" && (
        <div className="flex gap-2 items-start">
          <textarea aria-label="Editable transcript" value={transcript} onChange={(event) => setTranscript(event.target.value)} className="flex-1 bg-retro-bg pixel-border-thin p-2 text-xs" />
          <button type="button" onClick={() => void confirmTranscript()} className="pixel-border-thin px-2 py-1 font-pixel text-[10px]">Confirm</button>
        </div>
      )}
      <span role="status" className="font-pixel text-[10px]" data-state={state}>{error || state}</span>
    </section>
  );
}
