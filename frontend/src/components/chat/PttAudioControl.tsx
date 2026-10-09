import { useEffect, useRef, useState } from "react";
import type { GoalInfo } from "../../types";
import type { GeneralTaskCreateRequest } from "../../lib/generalTask";

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
  | "unknown"
  | "degraded"
  | "error";

type AudioSnapshot = {
  request_id: string;
  status: PttAudioState | string;
  error_code?: string | null;
  transcript?: { text?: string; digest?: string | null; confirmed_digest?: string | null };
};

function stateForRecovery(status: string): PttAudioState {
  if (["queued", "processing", "transporting"].includes(status)) return "processing";
  if (status === "degraded") return "degraded";
  if (status === "blocked") return "blocked";
  if (status === "failed") return "error";
  if (status === "transcript_ready") return "review_unavailable";
  return "error";
}

export function choosePttMimeType(): string | undefined {
  if (typeof MediaRecorder === "undefined") return undefined;
  const candidates = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"];
  return candidates.find((candidate) => MediaRecorder.isTypeSupported(candidate));
}

interface PttAudioControlProps {
  sessionId: string | null;
  disabled?: boolean;
  endpoint?: string;
  ownerPrincipalId?: string | null;
  ownerSessionId?: string | null;
  goals?: GoalInfo[];
  onTaskCreated?: (taskId: string) => void;
}

export function PttAudioControl({ sessionId, disabled = false, endpoint = "/api/audio/ptt",
  ownerPrincipalId, ownerSessionId, goals = [], onTaskCreated }: PttAudioControlProps) {
  const [state, setState] = useState<PttAudioState>("idle");
  const [captureConsent, setCaptureConsent] = useState(false);
  const [modelConsent, setModelConsent] = useState(false);
  const [audioBudget, setAudioBudget] = useState("0");
  const audioExecutionRef = useRef<{ requestId: string; budget: number } | null>(null);
  const [captureConsentReference, setCaptureConsentReference] = useState<string | null>(null);
  const [modelConsentReference, setModelConsentReference] = useState<string | null>(null);
  const [transcript, setTranscript] = useState("");
  const [snapshot, setSnapshot] = useState<AudioSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [consentPending, setConsentPending] = useState<"capture" | "model" | null>(null);
  const [confirmedIntent, setConfirmedIntent] = useState("");
  const [taskGoalId, setTaskGoalId] = useState("");
  const [taskBudget, setTaskBudget] = useState("0");
  const [taskCalls, setTaskCalls] = useState("1");
  const [taskEgress, setTaskEgress] = useState(false);
  const [taskBusy, setTaskBusy] = useState(false);
  const [taskNotice, setTaskNotice] = useState<string | null>(null);
  const [createdTaskId, setCreatedTaskId] = useState<string | null>(null);
  const taskPendingRef = useRef<{ digest: string; requestId: string; task: GeneralTaskCreateRequest } | null>(null);
  const taskScope = `${ownerPrincipalId ?? ""}:${ownerSessionId ?? ""}:${sessionId ?? ""}`;
  const taskScopeRef = useRef(taskScope);
  taskScopeRef.current = taskScope;
  const ownedGoals = goals.filter(goal => goal.status === "active" && goal.revision
    && goal.owner_session_id === ownerSessionId && goal.ownership_access !== "recovered_read_only");
  const selectedTaskGoal = ownedGoals.find(goal => goal.id === taskGoalId);
  const currentGoalsRef = useRef(ownedGoals);
  currentGoalsRef.current = ownedGoals;
  const consentMutationRef = useRef({ capture: 0, model: 0 });
  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const captureGenerationRef = useRef(0);
  const captureActiveRef = useRef(false);
  const actionSequenceRef = useRef(0);
  const actionRef = useRef<{ sequence: number; controller: AbortController } | null>(null);
  const mountedSessionRef = useRef(sessionId);
  const currentSessionRef = useRef(sessionId);
  // Consent is an operator-level decision and does not require a conversation
  // yet. Capture itself still needs a concrete session for durable ownership.
  const captureDisabled = disabled || !sessionId;
  const consentDisabled = disabled || state !== "idle" || consentPending !== null;
  const controlDisabledRef = useRef(captureDisabled);
  // Keep event callbacks created by an earlier render from uploading bytes
  // after the conversation or browser capture gate has changed.  React runs
  // effects after commit, while MediaRecorder may invoke ``onstop`` in that
  // interval, so these two refs are updated during render as well.
  currentSessionRef.current = sessionId;
  controlDisabledRef.current = captureDisabled;

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

  const stopCaptureResources = (abortAction = true) => {
    if (abortAction) {
      actionRef.current?.controller.abort();
      actionRef.current = null;
    }
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

  useEffect(() => () => stopCaptureResources(), []);

  useEffect(() => {
    taskPendingRef.current = null;
    audioExecutionRef.current = null;
    setConfirmedIntent(""); setTaskGoalId(""); setTaskBudget("0"); setTaskCalls("1");
    setTaskEgress(false); setTaskBusy(false); setTaskNotice(null); setCreatedTaskId(null);
    return () => { actionRef.current?.controller.abort(); actionRef.current = null; };
  }, [taskScope]);

  useEffect(() => {
    const sessionChanged = mountedSessionRef.current !== sessionId;
    mountedSessionRef.current = sessionId;
    if (sessionChanged) {
      // A switched conversation cannot retain another session's job handle.
      stopCaptureResources();
      setSnapshot(null);
      setTranscript("");
      setState("idle");
      setError(null);
      return;
    }
    if (captureDisabled) {
      // Busy/disabled is a browser capture gate, not a server-job revocation.
      // Keep the active request and durable snapshot available for recovery.
      stopCaptureResources(false);
      if (state === "capturing" || state === "requesting_capture") {
        setState("idle");
        setError(null);
      }
    }
  }, [captureDisabled, sessionId]);

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
      if (payload.transcript?.text) setConfirmedIntent(payload.transcript.text);
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
    } else if (payload.status === "unknown") {
      setTranscript(""); setConfirmedIntent(""); setState("unknown");
      setError("Original audio contact or cost is unknown. Inspect its durable accounting; do not submit another processing request.");
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
    if (captureDisabled || !captureConsent || state !== "idle") return;
    const mimeType = choosePttMimeType();
    if (typeof window !== "undefined" && window.isSecureContext === false) {
      setState("blocked");
      setError("Microphone capture requires HTTPS or localhost. This LAN HTTP page cannot request microphone access.");
      return;
    }
    if (!mimeType || !navigator.mediaDevices?.getUserMedia) {
      setState("blocked");
      setError("This browser cannot provide a supported audio recorder. Use a current browser over HTTPS or localhost.");
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
        // ``endCapture`` advances this generation before stopping the
        // recorder.  The upload is therefore accepted only for that exact
        // capture and session; a disable/session switch invalidates it before
        // the browser's asynchronous ``onstop`` callback can submit bytes.
        void uploadCapture(new Blob(chunksRef.current, { type: mimeType }), {
          generation: captureGeneration + 1,
          sessionId,
        });
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
    // ``stop`` may deliver ``onstop`` on a later task.  Ignore duplicate
    // pointer/key release events while that first stop is still pending; a
    // second generation would otherwise invalidate the legitimate upload.
    if (!captureActiveRef.current && state === "capturing") return;
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

  const processRequest = async (
    requestId: string,
    action: { sequence: number; controller: AbortController },
  ) => {
    const selectedBudget = Number(audioBudget);
    if (!audioExecutionRef.current) {
      if (!Number.isSafeInteger(selectedBudget) || selectedBudget <= 0 || selectedBudget > 1_000_000_000)
        throw new Error("Select a positive audio-only allowance before processing. It permits one call and does not fund a Task.");
      audioExecutionRef.current = { requestId, budget: selectedBudget };
    }
    if (audioExecutionRef.current.requestId !== requestId) throw new Error("Inspect the original audio request before another capture.");
    const response = await fetch(`${endpoint}/${requestId}/process`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ audio_budget_microusd: audioExecutionRef.current.budget, max_calls: 1 }),
      signal: action.controller.signal,
    });
    const payload = (await response.json()) as AudioSnapshot & { detail?: { code?: string } };
    if (!response.ok) throw new Error(payload.detail?.code || "audio_processing_failed");
    if (!isCurrentAction(action.sequence)) return;
    applySnapshot(payload);
  };

  const uploadCapture = async (
    blob: Blob,
    captureFence?: { generation: number; sessionId: string | null },
  ) => {
    const canUploadCapture = () =>
      !controlDisabledRef.current &&
      captureFence !== undefined &&
      captureGenerationRef.current === captureFence.generation &&
      currentSessionRef.current === captureFence.sessionId;
    // MediaRecorder callbacks can arrive after the browser capture gate or
    // active conversation changed.  Do not even read or encode stale bytes.
    if (!canUploadCapture()) return;
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
      if (!isCurrentAction(action.sequence) || !canUploadCapture()) return;
      const capturedAt = new Date();
      const body = {
        session_id: captureFence?.sessionId ?? "",
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
      if (["queued", "processing", "transporting"].includes(payload.status)) {
        await processRequest(payload.request_id, action);
      }
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
      const payload = (await response.json()) as AudioSnapshot;
      // Parsing the response yields to the event loop.  A reload/cancel
      // started during that yield owns the UI and the confirmation result is
      // stale even though the request was current immediately beforehand.
      if (!isCurrentAction(action.sequence)) return;
      if (payload.status === "confirmed" && payload.transcript?.confirmed_digest) setConfirmedIntent(transcript);
      applySnapshot(payload);
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
      if ([401, 403, 409].includes(response.status) && isCurrentAction(action.sequence)) {
        setConfirmedIntent("");
        taskPendingRef.current = null;
      }
      if (!response.ok) throw new Error(payload.detail?.code || "audio_reload_failed");
      if (!isCurrentAction(action.sequence)) return;
      applySnapshot(payload);
    } catch (cause) {
      if (!isCurrentAction(action.sequence)) return;
      if (snapshot.status === "transcript_ready") setTranscript("");
      setState(stateForRecovery(snapshot.status));
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
      await processRequest(snapshot.request_id, action);
    } catch (cause) {
      if (!isCurrentAction(action.sequence)) return;
      setState(stateForRecovery(snapshot.status));
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
      setState(previousState === "review" ? "review" : stateForRecovery(previousState));
      setError(cause instanceof Error ? cause.message : "audio_cancel_failed");
    } finally {
      finishAction(action.sequence);
    }
  };

  const captureTask = async () => {
    if (taskBusy || createdTaskId || disabled || !snapshot || state !== "confirmed" || !ownerPrincipalId || !ownerSessionId) return;
    const capturedScope = taskScope;
    const digest = snapshot.transcript?.confirmed_digest;
    if (!digest || !/^[a-f0-9]{64}$/.test(digest) || !confirmedIntent) {
      setTaskNotice("Reload the current confirmed Message before preparing a Task."); return;
    }
    if (!taskPendingRef.current) {
      const budget = Number(taskBudget), calls = Number(taskCalls);
      if (!selectedTaskGoal?.revision || !Number.isSafeInteger(budget) || budget <= 0
        || !Number.isSafeInteger(calls) || calls < 1 || calls > 12 || !taskEgress) {
        setTaskNotice("Select an active owned Goal, a positive Task allowance and separate Task model consent."); return;
      }
      taskPendingRef.current = { digest, requestId: snapshot.request_id, task: {
        goal_revision: selectedTaskGoal.revision, idempotency_key: crypto.randomUUID(), input: {
          goal_ref: selectedTaskGoal.id, intent: confirmedIntent, evidence_refs: [],
          requested_output: { type: "object", properties: { result: { type: "string" } }, required: ["result"], additionalProperties: false },
          inference_egress_acknowledged: true,
          limits: { max_steps: 16, max_inference_calls: calls, wall_seconds: 900,
            depth: 0, max_outstanding_children: 2, max_cost_microusd: budget },
        },
      } };
    }
    const pending = taskPendingRef.current;
    const currentGoal = ownedGoals.find(goal => goal.id === pending.task.input.goal_ref);
    if (pending.digest !== digest || pending.requestId !== snapshot.request_id || currentGoal?.revision !== pending.task.goal_revision) {
      setTaskNotice("The confirmed source or Goal changed. Inspect the original request in Work."); return;
    }
    const action = beginAction(); setTaskBusy(true); setTaskNotice(null);
    try {
      const response = await fetch(`${endpoint}/${encodeURIComponent(pending.requestId)}/task`, {
        method: "POST", headers: { "Content-Type": "application/json" }, signal: action.controller.signal,
        body: JSON.stringify({ confirmed_transcript_digest: pending.digest, task: pending.task }),
      });
      if (!isCurrentAction(action.sequence) || capturedScope !== taskScopeRef.current) return;
      if (!response.ok) {
        if ([401, 403, 409].includes(response.status)) {
          setConfirmedIntent(""); taskPendingRef.current = null;
          setTaskNotice("Current source or Goal authority is unavailable. Refresh the confirmed source and inspect Work."); return;
        }
        throw new Error("Task capture was not confirmed. Inspect Work or check this exact request.");
      }
      const value: unknown = await response.json();
      if (!isCurrentAction(action.sequence) || capturedScope !== taskScopeRef.current) return;
      if (currentGoalsRef.current.find(goal => goal.id === pending.task.input.goal_ref)?.revision !== pending.task.goal_revision) {
        setConfirmedIntent(""); setTaskNotice("Goal authority changed. Inspect the original Task request in Work."); return;
      }
      const receipt = value as { task?: Record<string, unknown>; audio_budget_transferred?: unknown };
      const task = receipt?.task;
      if (!task || receipt.audio_budget_transferred !== false || typeof task.task_id !== "string"
        || task.owner_principal_id !== ownerPrincipalId || task.owner_session_id !== ownerSessionId
        || task.goal_id !== pending.task.input.goal_ref || task.goal_revision !== pending.task.goal_revision
        || task.capability_id !== "agent.task.v1" || task.status !== "triage" || task.requires_review !== true
        || task.idempotency_key !== pending.task.idempotency_key) {
        throw new Error("Task receipt is unconfirmed. Inspect Work or check this exact request.");
      }
      taskPendingRef.current = null;
      setCreatedTaskId(task.task_id);
      setTaskNotice("Review-only Task prepared. Audio accounting remains separate. Review the plan in Work before execution.");
      onTaskCreated?.(task.task_id);
    } catch (cause) {
      if (isCurrentAction(action.sequence) && capturedScope === taskScopeRef.current)
        setTaskNotice(cause instanceof Error ? cause.message : "Task outcome is uncertain. Check the same request.");
    } finally {
      if (isCurrentAction(action.sequence) && capturedScope === taskScopeRef.current) setTaskBusy(false);
      finishAction(action.sequence);
    }
  };

  return (
    <section className="cockpit-audio-control flex flex-col gap-2 mt-2" aria-label="Push to talk">
      <div className="cockpit-audio-consent-row flex items-center gap-3 text-[10px] uppercase">
        <label className="flex items-center gap-1">
          <input type="checkbox" checked={captureConsent} onChange={(event) => void setServerConsent("capture", event.target.checked)} disabled={consentDisabled} />
          Allow microphone capture
        </label>
        <label className="flex items-center gap-1">
          <input type="checkbox" checked={modelConsent} onChange={(event) => void setServerConsent("model", event.target.checked)} disabled={consentDisabled} />
          Allow model processing
        </label>
      </div>
      <label className="text-xs">Audio-only allowance (microusd, one call)<input aria-label="Audio-only allowance" type="number" min="1" max="1000000000" step="1" value={audioBudget} disabled={state !== "idle"} onChange={event => setAudioBudget(event.target.value)} /></label>
      <p className="text-xs">Audio processing stays blocked without current endpoint, format, pricing and local decoder proof. This allowance is separate from any later Task.</p>
      <button
        type="button"
        disabled={captureDisabled || consentPending !== null || (!captureConsent && state === "idle") || (modelConsent && (!Number.isSafeInteger(Number(audioBudget)) || Number(audioBudget) <= 0)) || ["uploading", "processing", "confirming", "reloading", "cancelling", "requesting_capture", "review", "review_unavailable", "blocked", "unknown", "degraded", "error", "confirmed", "cancelled"].includes(state)}
        onPointerDown={() => void beginCapture()}
        onPointerUp={endCapture}
        onPointerCancel={endCapture}
        onKeyDown={(event) => { if (!event.repeat && (event.key === " " || event.key === "Enter")) void beginCapture(); }}
        onKeyUp={(event) => { if (event.key === " " || event.key === "Enter") endCapture(); }}
        className="cockpit-audio-button px-3 py-2 text-[10px] uppercase disabled:opacity-40"
      >
        {state === "capturing" ? "Release to stop" : "Hold to talk"}
      </button>
      {snapshot && ["review", "review_unavailable", "confirming", "reloading", "cancelling", "processing", "degraded", "blocked", "error"].includes(state) && (
        <div className="flex gap-2 items-start">
          {state === "review" ? (
            <textarea aria-label="Editable transcript" value={transcript} onChange={(event) => setTranscript(event.target.value)} className="cockpit-audio-transcript flex-1 p-2 text-xs" />
          ) : (
            <span className="flex-1 text-xs">
              {state === "processing" ? "Audio is processing. You can cancel or reload its durable status." : state === "review_unavailable" ? "Transcript text is unavailable in this worker." : error || "Audio processing needs recovery."}
            </span>
          )}
          <div className="flex flex-col gap-2">
            <button type="button" onClick={() => void confirmTranscript()} disabled={state !== "review"} className="cockpit-audio-secondary px-2 py-1 text-[10px]">Confirm</button>
            <button type="button" onClick={() => void reloadSnapshot()} disabled={state === "reloading"} className="cockpit-audio-secondary px-2 py-1 text-[10px]">Reload review</button>
            {["processing", "degraded", "blocked", "error", "review_unavailable"].includes(state) && <button type="button" onClick={() => void retryProcessing()} disabled={state === "processing"} className="cockpit-audio-secondary px-2 py-1 text-[10px]">Request original processing</button>}
            <button type="button" onClick={() => void cancelAudio()} disabled={state === "cancelling"} className="cockpit-audio-secondary px-2 py-1 text-[10px]">Cancel</button>
          </div>
        </div>
      )}
      {snapshot && ["confirming", "reloading", "cancelling"].includes(state) && (
        <div className="cockpit-audio-status p-2 text-xs" role="status">
          {state === "confirming" ? "Confirming transcript…" : state === "reloading" ? "Reloading transcript review…" : "Cancelling audio…"}
        </div>
      )}
      <span role="status" className="cockpit-audio-status text-[10px]" data-state={state}>{!sessionId ? "Choose or start a conversation before recording." : error || state}</span>
      {state === "confirmed" && ownerPrincipalId && ownerSessionId && <section aria-label="Prepare Task from confirmed Message" className="flex flex-col gap-2 text-xs">
        <p>Confirmation saves a Message. Preparing a Task uses a separate Goal and allowance; it does not execute a plan or transfer audio costs.</p>
        <label>Task Goal<select aria-label="Audio Task Goal" value={taskGoalId} disabled={taskBusy || Boolean(taskPendingRef.current)} onChange={event => setTaskGoalId(event.target.value)}>
          <option value="">Choose active Goal</option>{ownedGoals.map(goal => <option key={goal.id} value={goal.id}>{goal.title} · revision {goal.revision}</option>)}
        </select></label>
        <label>Task allowance (microusd)<input aria-label="Audio Task allowance" type="number" min="1" step="1" value={taskBudget} disabled={taskBusy || Boolean(taskPendingRef.current)} onChange={event => setTaskBudget(event.target.value)} /></label>
        <label>Task model calls<input aria-label="Audio Task model calls" type="number" min="1" max="12" step="1" value={taskCalls} disabled={taskBusy || Boolean(taskPendingRef.current)} onChange={event => setTaskCalls(event.target.value)} /></label>
        <label><input type="checkbox" checked={taskEgress} disabled={taskBusy || Boolean(taskPendingRef.current)} onChange={event => setTaskEgress(event.target.checked)} />Allow this confirmed intent to enter Task model planning after separate review</label>
        <button type="button" disabled={disabled || taskBusy || Boolean(createdTaskId) || !confirmedIntent} onClick={() => void captureTask()}>{createdTaskId ? "Review-only Task prepared" : taskBusy ? "Preparing Task…" : taskPendingRef.current ? "Check same Task request" : "Prepare review-only Task"}</button>
        {taskNotice && <p role="status">{taskNotice}</p>}
      </section>}
    </section>
  );
}
