import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { NEAR_TEE_DISCLOSURE, normalizeModelFabricSettings } from "../../lib/modelFabric";
import type { ModelFabricSettingsStatus, NearTeeEmbeddingIndexStatus, NearTeeSetupStatus, NearTeeSlotStatus } from "../../lib/modelFabric";
import { NearTeeEmbeddingSection } from "./NearTeeEmbeddingSection";
import { NearTeePanel } from "./NearTeePanel";

const slot: NearTeeSlotStatus = {
  model_id: "Qwen/Qwen3-Embedding-0.6B", enabled: true, max_input_tokens: 8192, max_output_tokens: 1, max_batch_size: 16,
  timeout_seconds: 20, temperature: null, request_cost_bound_microusd: 5_000, catalog_ttl_hours: 24,
  profile_id: "near_tee.embedding", consent_current: true, status: "blocked",
  reason_code: "near_tee_embedding_evidence_required", catalog_receipt: null, catalog_receipt_current: true,
  tee_hosted_per_catalog: true, tee_hosting_label: null, capability_check_timeout_seconds: 36,
  check_capabilities: ["embedding", "health", "latency_ms"], evidence_required: true,
};
const lexical: NearTeeEmbeddingIndexStatus = { slot_reason_code: "near_tee_embedding_evidence_required", namespace: null,
  model: "Qwen/Qwen3-Embedding-0.6B", measured_dimension: null, index_complete: false, recall_mode: "lexical",
  notice: "Semantic recall is unavailable: memory recall uses lexical search until the NEAR embedding re-index completes.",
  max_items: 10_000, job: null };
function setup(embedding: NearTeeSlotStatus | null = slot, index: NearTeeEmbeddingIndexStatus | null = lexical): NearTeeSetupStatus {
  return { configured: true, status: "blocked", reason_code: null, key_present: true, credential_fingerprint: "abcdefabcdef",
    allowlist: { text: [] }, disclosure: NEAR_TEE_DISCLOSURE, tee_verified: false, e2ee: false,
    slots: { text: null, embedding }, embedding_index: index };
}
function saved(revision = 7): ModelFabricSettingsStatus {
  return { schema_version: "seraph.model-fabric.settings.v1", status: "blocked", near_tee: setup(),
    egress_revision: revision, egress_revoked: false } as ModelFabricSettingsStatus;
}
afterEach(() => { vi.unstubAllGlobals(); });

it("shows the evidence block and lexical recall, and keeps checks and re-index disabled without contact", () => {
  const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
  render(<NearTeeEmbeddingSection setup={setup()} stale={false} revision={5} laneCeilingMicrousd={1_000_000} onSave={vi.fn()} />);
  expect(screen.getByText(/source evidence shows the embeddings and billing endpoints honour x-no-aliasing/)).toBeInTheDocument();
  expect(screen.getByText(/no readiness flag, so the catalog check reports it not ready/)).toBeInTheDocument();
  expect(screen.getByRole("note")).toHaveTextContent("Recall: lexical. Semantic recall is unavailable");
  expect(screen.getByRole("button", { name: "Run embedding capability check" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Start re-index" })).toBeDisabled();
  expect(screen.queryByText(/verified TEE|confidential/i)).toBeNull();
  expect(fetch).not.toHaveBeenCalled();
});

it("saves only the embedding slot with a fixed one-token output and explicit memory-egress consent", async () => {
  const onSave = vi.fn(async (_payload: Record<string, unknown>) => saved(5));
  render(<NearTeeEmbeddingSection setup={setup(null, null)} stale={false} revision={3} laneCeilingMicrousd={1_000_000} onSave={onSave} />);
  fireEvent.click(screen.getByLabelText("Enable NEAR TEE embedding slot"));
  fireEvent.click(screen.getByRole("button", { name: "Save embedding slot" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("allowlist");
  fireEvent.change(screen.getByLabelText("NEAR TEE embedding model"), { target: { value: "Qwen/Qwen3-Embedding-0.6B" } });
  fireEvent.click(screen.getByRole("button", { name: "Save embedding slot" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("plaintext");
  expect(onSave).not.toHaveBeenCalled();
  fireEvent.click(screen.getByLabelText("Acknowledge NEAR TEE memory plaintext access"));
  fireEvent.click(screen.getByRole("button", { name: "Save embedding slot" }));
  await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
  expect(onSave.mock.calls[0][0]).toEqual({ expected_policy_revision: 3, near_tee: {
    schema_version: "seraph.near.tee.setup.v1", api_base: "https://cloud-api.near.ai/v1",
    slots: { embedding: { model_id: "Qwen/Qwen3-Embedding-0.6B", enabled: true, max_input_tokens: 8192, max_output_tokens: 1,
      max_batch_size: 16, timeout_seconds: 30, request_cost_bound_microusd: 5_000, catalog_ttl_hours: 24,
      plaintext_provider_egress_acknowledged: true } } } });
});

it("starts a consented, budgeted re-index and shows progress and cancel", async () => {
  const ready = { ...slot, status: "ready" as const, reason_code: null, evidence_required: false };
  const running: NearTeeEmbeddingIndexStatus = { ...lexical, slot_reason_code: null, measured_dimension: 1024,
    job: { job_id: "near-reindex:abc", status: "running", reason_code: null, live: true, planned_items: 40, indexed_items: 16,
      skipped_existing: 4, oversize_items: 0, batches: 1, spent_microusd: 1000, budget_microusd: 100_000 } };
  const fetch = vi.fn(async (url: string) => new Response(JSON.stringify(url.endsWith("/model-fabric") ? saved() : lexical), { status: 200 }));
  vi.stubGlobal("fetch", fetch);
  const { rerender } = render(<NearTeeEmbeddingSection setup={setup(ready, lexical)} stale={false} revision={5} laneCeilingMicrousd={1_000_000} onSave={vi.fn()} />);
  fireEvent.change(screen.getByLabelText("Re-index budget USD"), { target: { value: "0.1" } });
  fireEvent.change(screen.getByLabelText("Re-index maximum memories"), { target: { value: "40" } });
  fireEvent.click(screen.getByRole("button", { name: "Start re-index" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Acknowledge");
  expect(fetch).not.toHaveBeenCalled();
  fireEvent.click(screen.getByLabelText("Acknowledge re-index memory egress"));
  fireEvent.click(screen.getByRole("button", { name: "Start re-index" }));
  await waitFor(() => expect(fetch).toHaveBeenCalled());
  const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
  expect(url).toContain("/api/settings/model-fabric/near-tee/embedding-reindex");
  const body = JSON.parse(String(init.body));
  expect(body).toMatchObject({ expected_policy_revision: 5, budget_microusd: 100_000, max_items: 40, memory_egress_acknowledged: true });
  expect(body.request_uuid).toMatch(/^[0-9a-f-]{36}$/);
  rerender(<NearTeeEmbeddingSection setup={setup(ready, running)} stale={false} revision={5} laneCeilingMicrousd={1_000_000} onSave={vi.fn()} />);
  expect(screen.getByLabelText("Re-index progress")).toHaveTextContent("16 of 40 memories indexed, 4 already current, spent $0.001 of $0.1");
  expect(screen.getByRole("button", { name: "Resume re-index" })).toBeDisabled();
  fetch.mockClear();
  fireEvent.click(screen.getByRole("button", { name: "Cancel re-index" }));
  await waitFor(() => expect(fetch).toHaveBeenCalled());
  const [cancelUrl, cancelInit] = fetch.mock.calls[0] as unknown as [string, RequestInit];
  expect(cancelUrl).toContain("/embedding-reindex/cancel");
  expect(JSON.parse(String(cancelInit.body))).toEqual({ job_id: "near-reindex:abc" });
});

it("normalizes the embedding index fail-closed to lexical", () => {
  const next = normalizeModelFabricSettings({ schema_version: "seraph.model-fabric.settings.v1", status: "blocked",
    near_tee: { ...setup(), allowlist: { text: [], embedding: ["Qwen/Qwen3-Embedding-0.6B", "openai/text-embedding-3-small"] },
      embedding_index: { ...lexical, recall_mode: "semantic", index_complete: false } } });
  expect(next?.near_tee?.allowlist.embedding).toEqual(["Qwen/Qwen3-Embedding-0.6B"]);
  expect(next?.near_tee?.embedding_index?.recall_mode).toBe("lexical");
  expect(next?.near_tee?.slots.embedding?.evidence_required).toBe(true);
});

it("reports catalog-check outcomes per slot", async () => {
  const text: NearTeeSlotStatus = { ...slot, model_id: "Qwen/Qwen3.8-27B", profile_id: "near_tee.text", max_output_tokens: 1024,
    max_batch_size: null, status: "configuration_required", reason_code: "proof_missing:health", evidence_required: false,
    catalog_receipt: { model_id: "Qwen/Qwen3.8-27B", checked_at_iso: "2026-11-14T00:00:00+00:00", expires_at: null,
      outcome: "passed", reason_code: null, required_cost_microusd: 1711 } };
  const embedding: NearTeeSlotStatus = { ...slot, catalog_receipt: { model_id: "Qwen/Qwen3-Embedding-0.6B",
    checked_at_iso: "2026-11-14T00:00:00+00:00", expires_at: null, outcome: "failed",
    reason_code: "near_tee_catalog_not_ready", required_cost_microusd: 82 } };
  const response = { ...saved(5), near_tee: { ...setup(embedding), slots: { text, embedding } } };
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(response), { status: 200 })));
  render(<NearTeePanel setup={response.near_tee} stale={false} policyRevision={5} laneCeilingMicrousd={1_000_000} onSave={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "Run catalog check" }));
  await waitFor(() => expect(screen.getByText(/text slot: catalog check passed/)).toBeInTheDocument());
  expect(screen.getByText(/embedding slot: catalog check failed \(near tee catalog not ready\)/)).toBeInTheDocument();
});
