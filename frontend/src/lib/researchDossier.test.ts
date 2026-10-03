import { beforeEach, expect, it, vi } from "vitest";
import { apiFetch } from "./api";
import { newResearchCreation, readResearchPending, researchStorageKey, retainResearchPending, submitResearchCreation } from "./researchDossier";
import type { ResearchInput } from "./researchDossier";
vi.mock("./api", () => ({ apiFetch: vi.fn() }));
const key = researchStorageKey("operator:one", "session-one", "create");
const input: ResearchInput = { schema_version: 1, question: "What does this evidence say?", perspectives: [{ instruction: "Attribute evidence", source_slots: [0] }],
  sources: [{ kind: "public_https_text", url: "https://example.com/source.txt", first_line: 1, last_line: 2 }], source_egress_acknowledged: true, no_learning: true };
beforeEach(() => { vi.restoreAllMocks(); vi.mocked(apiFetch).mockReset(); window.sessionStorage.clear(); });
it("retains the returned artifact binding before the task POST and reconciles an uncertain response", async () => {
  const taskBodies: string[] = [];
  vi.mocked(apiFetch).mockImplementation(async (url, options) => {
    const retained = readResearchPending(key);
    expect(retained?.kind).toBe("create");
    if (String(url).endsWith("input-artifacts")) return new Response(JSON.stringify({ artifact_id: "original-artifact", typed_input_digest: "a".repeat(64), capability_id: "work.research-dossier.v1", goal_id: "goal-one", goal_revision: 2 }));
    if (retained?.kind === "create") expect(retained.artifact_id).toBe("original-artifact");
    taskBodies.push(String(options?.body));
    if (taskBodies.length === 1) throw new Error("uncertain task response");
    return new Response(JSON.stringify({ task: { task_id: "original-task", input_artifact_id: "original-artifact", capability_id: "work.research-dossier.v1", goal_id: "goal-one", goal_revision: 2 } }));
  });
  const pending = newResearchCreation("goal-one", 2, "Finite research", input);
  await expect(submitResearchCreation(key, pending)).rejects.toThrow("uncertain task response");
  const restored = readResearchPending(key)!;
  await expect(submitResearchCreation(key, restored)).resolves.toMatchObject({ task_id: "original-task" });
  expect(taskBodies[1]).toBe(taskBodies[0]);
  expect(vi.mocked(apiFetch).mock.calls.filter(([url]) => String(url).endsWith("input-artifacts"))).toHaveLength(1);
});
it("rejects oversized input, unknown actions and mismatched owner-session task storage", () => {
  expect(() => newResearchCreation("goal-one", 1, "Finite", { ...input, question: "a".repeat(2049) })).toThrow();
  window.sessionStorage.setItem(key, JSON.stringify({ kind: "shell", command: "anything" }));
  expect(() => readResearchPending(key)).toThrow();
  expect(() => retainResearchPending(researchStorageKey("operator:one", "session-two", "other-task"), {
    kind: "control", task_id: "original-task", action: "recover", body: { expected_revision: 1, idempotency_key: "original-control" },
  })).toThrow();
});
