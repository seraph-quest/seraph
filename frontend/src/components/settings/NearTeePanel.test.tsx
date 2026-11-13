import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { NEAR_TEE_DISCLOSURE, normalizeModelFabricSettings } from "../../lib/modelFabric";
import type { ModelFabricSettingsStatus, NearTeeSetupStatus, NearTeeSlotStatus } from "../../lib/modelFabric";
import { NearTeePanel } from "./NearTeePanel";

const slot: NearTeeSlotStatus = {
  model_id: "Qwen/Qwen3.8-27B", enabled: true, max_input_tokens: 8192, max_output_tokens: 1024,
  timeout_seconds: 20, temperature: null, request_cost_bound_microusd: 20_000, catalog_ttl_hours: 24,
  profile_id: "near_tee.text", consent_current: true, status: "blocked",
  reason_code: "near_tee_catalog_receipt_missing", catalog_receipt: null, catalog_receipt_current: false,
  tee_hosted_per_catalog: false, tee_hosting_label: null, capability_check_timeout_seconds: 36,
};
function setup(text: NearTeeSlotStatus | null = slot): NearTeeSetupStatus {
  return { configured: true, status: "blocked", reason_code: null, key_present: true, credential_fingerprint: "abcdefabcdef",
    allowlist: { text: [] }, disclosure: NEAR_TEE_DISCLOSURE, tee_verified: false, e2ee: false, slots: { text } };
}
function saved(revision = 7): ModelFabricSettingsStatus {
  return { schema_version: "seraph.model-fabric.settings.v1", status: "blocked", near_tee: setup(),
    egress_revision: revision, egress_revoked: false } as ModelFabricSettingsStatus;
}
const save = () => fireEvent.click(screen.getByRole("button", { name: "Save NEAR TEE settings" }));
afterEach(() => { vi.unstubAllGlobals(); window.localStorage.clear(); });

it("starts disabled with the honest trust disclosure and makes no provider request", () => {
  const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
  render(<NearTeePanel setup={null} stale={false} policyRevision={3} laneCeilingMicrousd={1_000_000} onSave={vi.fn()} />);
  expect(screen.getByLabelText("Enable NEAR TEE text slot")).not.toBeChecked();
  expect(screen.getByText(NEAR_TEE_DISCLOSURE)).toBeInTheDocument();
  expect(screen.queryByText(/verified TEE|confidential/i)).toBeNull();
  expect(screen.getByRole("button", { name: "Run catalog check" })).toBeDisabled();
  expect(fetch).not.toHaveBeenCalled();
});

it("requires an allowlisted model and explicit plaintext acknowledgement before saving", async () => {
  const onSave = vi.fn(async (_payload: Record<string, unknown>) => saved(5));
  render(<NearTeePanel setup={null} stale={false} policyRevision={3} laneCeilingMicrousd={1_000_000} onSave={onSave} />);
  fireEvent.click(screen.getByLabelText("Enable NEAR TEE text slot"));
  save(); expect(await screen.findByRole("alert")).toHaveTextContent("allowlist");
  fireEvent.change(screen.getByLabelText("NEAR TEE text model"), { target: { value: "Qwen/Qwen3.8-27B" } });
  fireEvent.change(screen.getByLabelText("NEAR TEE API key"), { target: { value: "synthetic-near-tee-secret" } });
  save(); expect(await screen.findByRole("alert")).toHaveTextContent("Acknowledge");
  expect(onSave).not.toHaveBeenCalled();
  fireEvent.click(screen.getByLabelText("Acknowledge NEAR TEE plaintext access")); save();
  await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
  expect(onSave.mock.calls[0][0]).toEqual({ expected_policy_revision: 3, near_tee: {
    schema_version: "seraph.near.tee.setup.v1", api_base: "https://cloud-api.near.ai/v1", api_key: "synthetic-near-tee-secret",
    slots: { text: { model_id: "Qwen/Qwen3.8-27B", enabled: true, max_input_tokens: 8192, max_output_tokens: 1024,
      timeout_seconds: 30, request_cost_bound_microusd: 20_000, catalog_ttl_hours: 24, plaintext_provider_egress_acknowledged: true } },
  } });
  await waitFor(() => expect(screen.getByLabelText("NEAR TEE API key")).toHaveValue(""));
  expect(window.localStorage.length).toBe(0);
});

it("carries unchanged consent and blocks saving without a published lane", async () => {
  const onSave = vi.fn(async (_payload: Record<string, unknown>) => saved());
  const { rerender } = render(<NearTeePanel setup={setup()} stale={false} policyRevision={5} laneCeilingMicrousd={null} onSave={onSave} />);
  expect(screen.queryByLabelText("Acknowledge NEAR TEE plaintext access")).toBeNull();
  save(); expect(await screen.findByRole("alert")).toHaveTextContent("shared inference lane");
  rerender(<NearTeePanel setup={setup()} stale={false} policyRevision={5} laneCeilingMicrousd={1_000_000} onSave={onSave} />);
  save(); await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
  expect(onSave.mock.calls[0][0]).toMatchObject({ expected_policy_revision: 5,
    near_tee: { slots: { text: { plaintext_provider_egress_acknowledged: false } } } });
  expect(onSave.mock.calls[0][0].near_tee as Record<string, unknown>).not.toHaveProperty("api_key");
});

it("runs the explicit catalog check at the current revision and shows the receipt status", async () => {
  const response = { ...saved(5), near_tee: setup({ ...slot, status: "configuration_required", reason_code: "proof_missing:health",
    catalog_receipt_current: true, tee_hosted_per_catalog: true,
    tee_hosting_label: "TEE-hosted per NEAR catalog as of 2026-11-13T00:00:00+00:00; NEAR gateway receives plaintext; attestation not verified by Seraph; no end-to-end encryption",
    catalog_receipt: { model_id: "Qwen/Qwen3.8-27B", checked_at_iso: "2026-11-13T00:00:00+00:00", expires_at: null,
      outcome: "passed", reason_code: null, required_cost_microusd: 1711 } }) };
  const fetch = vi.fn(async () => new Response(JSON.stringify({ ...response, tee_verified: undefined }), { status: 200 }));
  vi.stubGlobal("fetch", fetch);
  const refreshed = vi.fn();
  render(<NearTeePanel setup={setup()} stale={false} policyRevision={5} laneCeilingMicrousd={1_000_000} onSave={vi.fn()} onRefreshed={refreshed} />);
  fireEvent.click(screen.getByRole("button", { name: "Run catalog check" }));
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
  const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
  expect(url).toContain("/api/settings/model-fabric/near-tee/catalog-check");
  expect(JSON.parse(String(init.body))).toEqual({ expected_policy_revision: 5 });
  expect(await screen.findByRole("status")).toHaveTextContent("not a capability check");
  expect(refreshed).toHaveBeenCalledTimes(1);
});

it("normalizes NEAR TEE status fail-closed and never keeps credential values", () => {
  const next = normalizeModelFabricSettings({ schema_version: "seraph.model-fabric.settings.v1", status: "blocked",
    near_tee: { ...setup(), api_key: "leaked", allowlist: { text: ["Qwen/Qwen3.8-27B", "anthropic/claude"] } } });
  expect(next?.near_tee?.allowlist.text).toEqual(["Qwen/Qwen3.8-27B"]);
  expect(JSON.stringify(next)).not.toContain("leaked");
  const verifiedClaim = normalizeModelFabricSettings({ schema_version: "seraph.model-fabric.settings.v1", status: "blocked",
    near_tee: { ...setup(), tee_verified: true } });
  expect(verifiedClaim?.near_tee).toBeNull();
  expect(verifiedClaim?.near_tee_metadata_unavailable).toBe(true);
});
