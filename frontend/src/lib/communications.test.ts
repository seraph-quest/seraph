import { describe, expect, it } from "vitest";
import { parseCommunicationCleanup, parseCommunicationPlan } from "./communications";
export const replyRef = { source_id: "mail-one", capability_id: "work.mail-reply-draft.v1", source_revision: "message-revision",
  source_input_digest: "a".repeat(64), task_id: "original-source-task", attempt_id: "original-attempt", job_id: "original-source-job",
  artifact_path: "private/source.json", artifact_digest: "b".repeat(64), readback_id: "original-readback" };
export const privateWire = { task_id: "communication-task", no_learning: true, plan: { source_refs: [replyRef], reply_drafts: [
  { source_ref: replyRef, subject: "Reviewed reply", body: "<script>private inert body</script>", caveats: ["Review recipient."] }], meeting_preparations: [], reschedule_proposals: [], unresolved_questions: [] } };
describe("closed private communication wire", () => {
  it("accepts current exact source refs and the empty plan", () => {
    expect(parseCommunicationPlan(privateWire, privateWire.task_id)).toEqual(privateWire);
    expect(parseCommunicationPlan({ ...privateWire, plan: { ...privateWire.plan, source_refs: [], reply_drafts: [] } }, privateWire.task_id).plan.source_refs).toEqual([]);
  });
  it.each(["wrong-task", "extra-field", "learning", "substituted-source", "over-limit", "duplicate-ref"])("rejects %s before private rendering", kind => {
    const value = JSON.parse(JSON.stringify(privateWire));
    if (kind === "wrong-task") value.task_id = "other-task";
    if (kind === "extra-field") value.plan.effect_authority = true;
    if (kind === "learning") value.no_learning = false;
    if (kind === "substituted-source") value.plan.reply_drafts[0].source_ref.task_id = "other-task";
    if (kind === "over-limit") value.plan.reply_drafts = Array(6).fill(value.plan.reply_drafts[0]);
    if (kind === "duplicate-ref") value.plan.source_refs.push(value.plan.source_refs[0]);
    expect(() => parseCommunicationPlan(value, privateWire.task_id)).toThrow(/incomplete or changed/);
  });
});
describe("closed aggregate cleanup wire", () => {
  it.each([
    { status: "cleanup_verified", absent: true, no_learning: true },
    { status: "cleanup_unresolved", absent: false, no_learning: true },
  ])("accepts the exact %s absence receipt", value => expect(parseCommunicationCleanup(value)).toEqual(value));
  it.each([
    { status: "cleanup_verified", absent: false, no_learning: true },
    { status: "cleanup_unresolved", absent: true, no_learning: true },
    { status: "deleted", absent: true, no_learning: true },
    { status: "cleanup_verified", absent: "true", no_learning: true },
    { status: "cleanup_verified", absent: true, no_learning: false },
    { status: "cleanup_verified", absent: true, no_learning: true, task_id: "fabricated-echo" },
    { status: "cleanup_verified", no_learning: true },
  ])("rejects inconsistent, extra or missing cleanup evidence %s", value => expect(() => parseCommunicationCleanup(value)).toThrow(/unconfirmed/));
});
