import { describe, expect, it } from "vitest";
import wire from "./__fixtures__/channel-output-wire-r6.json";
import { decodeChannelOutputReview } from "./channelCapture";

// Captured from the actual authenticated C1/native physical output test,
// channel-origin-r5.xml: original producer, terminal readback, then API GET.
describe("actual channel output wire", () => {
  it("binds the original Task, attempt, Root and exact native artifact", () => {
    const decoded = decodeChannelOutputReview(wire, wire.owner_session_id);
    expect(decoded.taskId).toBe(wire.task_id);
    expect(decoded.attemptId).toBe(wire.attempt_id);
    expect(decoded.workflowRunId).toBe(wire.workflow_run_id);
    expect(decoded.reference.artifact_id).toBe(wire.reference.artifact_id);
    expect(decoded.reference.content_sha256).toBe(wire.reference.content_sha256);
  });
  it.each([
    { ...wire, no_learning: false }, { ...wire, owner_session_id: "foreign-root" },
    { ...wire, task_revision: 0 }, { ...wire, approve: true },
    { ...wire, reference: { ...wire.reference, exists: false } },
    { ...wire, reference: { ...wire.reference, file_path: "../../private" } },
    { ...wire, reference: { ...wire.reference, content_sha256: "0".repeat(64) } },
  ])("rejects changed scope, effect authority or unverified artifact", changed => {
    expect(() => decodeChannelOutputReview(changed, wire.owner_session_id)).toThrow();
  });
});
