import { useEffect, useState } from "react";
import type { GoalInfo } from "../../types";
import { originalExecution, type MoltbookConnection, type MoltbookJob, type MoltbookPending } from "../../lib/moltbook";

export function MoltbookWriteControls({ connection, goal, job, busy, act }: {
  connection: MoltbookConnection | null; goal?: GoalInfo; job: MoltbookJob | null; busy: boolean;
  act: (request: MoltbookPending) => Promise<void>;
}) {
  const [reviewId, setReviewId] = useState("");
  const [reviewDigest, setReviewDigest] = useState("");
  const [operation, setOperation] = useState("create_post");
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [post, setPost] = useState("");
  const [parent, setParent] = useState("");
  const [publicAcknowledged, setPublicAcknowledged] = useState(false);
  const [answer, setAnswer] = useState("");
  useEffect(() => {
    if (job?.status === "succeeded" && job.declared_authority?.operation === "community" && job.artifacts?.length === 1) {
      setReviewId(job.job_id); setReviewDigest(job.artifacts[0].content_sha256);
    }
  }, [job]);
  const checkpoint = job?.checkpoints.find(value => value.checkpoint_id === "moltbook:state")?.payload;
  const phase = checkpoint?.phase;
  const approval = typeof checkpoint?.approval_id === "string" ? checkpoint.approval_id : null;
  function prepare() {
    if (!goal || !connection?.revision) return;
    const fields = operation === "create_post" ? { community: "introductions", title, content }
      : { post_id: post, content, ...(parent ? { parent_id: parent } : {}) };
    void act({ method: "POST", path: "/writes", body: { operation, fields, request_key: crypto.randomUUID(),
      goal_id: goal.id, goal_revision: goal.revision, expected_revision: connection.revision,
      community_job_id: reviewId, community_digest: reviewDigest, introductions_allowed: true, public_only: true } });
  }
  return <section aria-label="Moltbook approved public text" className="space-y-2">
    <p>Public text is limited to a reviewed introductions community. Review a fresh community read first. Each creation and manual verification requires a separate exact approval.</p>
    <label className="block">Completed community job <input aria-label="Reviewed Moltbook community job" value={reviewId} onChange={event => setReviewId(event.target.value)} /></label>
    <label className="block">Community artifact digest <input aria-label="Reviewed Moltbook community digest" value={reviewDigest} onChange={event => setReviewDigest(event.target.value)} /></label>
    <label className="block">Public text kind <select aria-label="Moltbook write kind" value={operation} onChange={event => setOperation(event.target.value)}><option value="create_post">Introduction post</option><option value="create_comment">Comment or reply</option></select></label>
    {operation === "create_post" ? <label className="block">Title <input aria-label="Moltbook draft title" maxLength={300} value={title} onChange={event => setTitle(event.target.value)} /></label>
      : <><label className="block">Target post ID <input aria-label="Moltbook target post" value={post} onChange={event => setPost(event.target.value)} /></label><label className="block">Parent comment ID (optional) <input aria-label="Moltbook parent comment" value={parent} onChange={event => setParent(event.target.value)} /></label></>}
    <label className="block">Exact public text <textarea aria-label="Moltbook draft text" maxLength={8192} value={content} onChange={event => setContent(event.target.value)} /></label>
    <label className="block"><input type="checkbox" checked={publicAcknowledged} onChange={event => setPublicAcknowledged(event.target.checked)} /> This is public personal, noncommercial text; the reviewed community permits introductions, and I will not redistribute others’ content.</label>
    <button disabled={busy || !!connection?.active_job_id || connection?.mode !== "active" || !goal || !publicAcknowledged || !content || !reviewId || !/^[a-f0-9]{64}$/.test(reviewDigest) || !connection.consent?.actions?.includes(operation)} onClick={prepare}>Prepare exact public text for approval</button>
    {job?.draft && <div><p>Canonical original draft for this job:</p><pre className="whitespace-pre-wrap break-all">{JSON.stringify(job.draft, null, 2)}</pre></div>}
    {job && approval && ["awaiting_create_approval", "awaiting_verify_approval"].includes(String(phase)) && <div>
      <p>{phase === "awaiting_verify_approval" ? "Approve the manual answer for the original content and challenge." : "Approve only the canonical original draft shown above."}</p>
      <button disabled={busy || !job.draft} onClick={() => void act({ method: "POST", path: `/jobs/${job.job_id}/approval`, body: { approval_id: approval, decision: "approved" } })}>Approve exact {phase === "awaiting_verify_approval" ? "verification answer" : "creation"}</button>
      <button disabled={busy} onClick={() => void act({ method: "POST", path: `/jobs/${job.job_id}/approval`, body: { approval_id: approval, decision: "denied" } })}>Deny exact approval</button>
      {job.manual_answer && <pre>{job.manual_answer}</pre>}
      <button disabled={busy || !job.lease || job.approval?.status !== "approved"} onClick={() => void act(originalExecution(job))}>Run approved original {phase === "awaiting_verify_approval" ? "verification" : "creation"}</button>
    </div>}
    {job && phase === "awaiting_manual_answer" && <div>
      <p>Untrusted platform challenge for original content {String(checkpoint?.content_id)}; expires {String(checkpoint?.challenge_expires_at)}. Solve it manually. Seraph does not solve challenges or bypass identity checks.</p>
      <pre className="whitespace-pre-wrap break-all">{String(checkpoint?.challenge_text ?? "")}</pre>
      <label className="block">Manual answer (two decimal places) <input aria-label="Moltbook manual verification answer" value={answer} onChange={event => setAnswer(event.target.value)} /></label>
      <button disabled={busy || !/^-?(?:0|[1-9][0-9]{0,26})\.[0-9]{2}$/.test(answer)} onClick={() => void act({ method: "POST", path: `/jobs/${job.job_id}/answer`, body: { answer, request_key: crypto.randomUUID() } })}>Retain manual answer for separate approval</button>
    </div>}
  </section>;
}
