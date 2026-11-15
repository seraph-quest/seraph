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
    catalog_receipt: { model_id: "Qwen/Qwen3.8-27B", checked_at_iso: "2026-11-13T00:00:00+00:00",
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

it("saves only the vision slot after both plaintext and separate screenshot consent", async () => {
  const onSave = vi.fn(async (_payload: Record<string, unknown>) => saved(7));
  render(<NearTeePanel setup={setup()} stale={false} policyRevision={5} laneCeilingMicrousd={1_000_000} onSave={onSave} />);
  expect(screen.getByText(/Screenshots are sent to the NEAR gateway in plaintext/)).toBeInTheDocument();
  const options = Array.from((screen.getByLabelText("NEAR TEE vision model") as HTMLSelectElement).options).map(option => option.value);
  expect(options).toEqual(["", "z-ai/glm-5.3-flash", "Qwen/Qwen3.8-27B", "Qwen/Qwen3-VL-30B-A3B-Instruct", "deepseek-ai/DeepSeek-V4.1-Flash-1017"]);
  expect(options).not.toContain("Qwen/Qwen3.6-35B-A3B-FP8");
  fireEvent.click(screen.getByLabelText("Enable NEAR TEE vision slot"));
  fireEvent.change(screen.getByLabelText("NEAR TEE vision model"), { target: { value: "Qwen/Qwen3-VL-30B-A3B-Instruct" } });
  const saveVision = () => fireEvent.click(screen.getByRole("button", { name: "Save NEAR TEE vision slot" }));
  saveVision(); expect(await screen.findByRole("alert")).toHaveTextContent("plaintext");
  fireEvent.click(screen.getByLabelText("Acknowledge NEAR TEE vision plaintext access"));
  saveVision(); expect(await screen.findByRole("alert")).toHaveTextContent("screenshots are sent to NEAR");
  expect(onSave).not.toHaveBeenCalled();
  fireEvent.click(screen.getByLabelText("Acknowledge screenshots sent to NEAR in plaintext"));
  saveVision();
  await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
  expect(onSave.mock.calls[0][0]).toEqual({ expected_policy_revision: 5, near_tee: {
    schema_version: "seraph.near.tee.setup.v1", api_base: "https://cloud-api.near.ai/v1",
    slots: { vision: { model_id: "Qwen/Qwen3-VL-30B-A3B-Instruct", enabled: true, max_input_tokens: 16384,
      max_output_tokens: 2048, timeout_seconds: 30, request_cost_bound_microusd: 20_000, catalog_ttl_hours: 24,
      plaintext_provider_egress_acknowledged: true, screenshot_plaintext_egress_acknowledged: true } },
  } });
});

it("runs the vision capability check for vision mechanics and stays usable without metadata", async () => {
  const visionSlot: NearTeeSlotStatus = { ...slot, model_id: "Qwen/Qwen3-VL-30B-A3B-Instruct", profile_id: "near_tee.vision",
    catalog_receipt_current: true, reason_code: "proof_missing:health",
    catalog_receipt: { model_id: "Qwen/Qwen3-VL-30B-A3B-Instruct", checked_at_iso: "2026-11-13T00:00:00+00:00",
      outcome: "passed", reason_code: null, required_cost_microusd: 3584, structured_outputs: true } };
  const fetch = vi.fn(async () => new Response(JSON.stringify({ outcome: "passed" }), { status: 200 }));
  vi.stubGlobal("fetch", fetch);
  const { unmount } = render(<NearTeePanel setup={{ ...setup(), slots: { text: slot, vision: visionSlot } }} stale={false}
    policyRevision={5} laneCeilingMicrousd={1_000_000} onSave={vi.fn()} />);
  expect(screen.getByText(/JSON schema requests/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Run vision capability check" }));
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(4));
  const bodies = (fetch.mock.calls as unknown as [string, RequestInit][]).map(([, init]) => JSON.parse(String(init.body)));
  expect(bodies.map(body => body.capability)).toEqual(["vision", "structured_output", "health", "latency_ms"]);
  expect(bodies.every(body => body.profile_id === "near_tee.vision")).toBe(true);
  unmount();
  // Metadata failure: the panel still renders both editable slots.
  render(<NearTeePanel setup={null} stale={false} policyRevision={5} laneCeilingMicrousd={1_000_000} onSave={vi.fn()} />);
  expect(screen.getByLabelText("Enable NEAR TEE vision slot")).not.toBeChecked();
  expect(screen.getByLabelText("NEAR TEE vision model")).toBeEnabled();
});

it("caps the vision timeout at the job budget and withdraws screenshot consent explicitly", async () => {
  const visionSlot: NearTeeSlotStatus = { ...slot, model_id: "Qwen/Qwen3-VL-30B-A3B-Instruct", profile_id: "near_tee.vision",
    max_input_tokens: 16384, max_output_tokens: 2048 };
  const onSave = vi.fn(async (_payload: Record<string, unknown>) => ({ ...saved(9),
    near_tee: { ...setup(), slots: { text: slot, vision: visionSlot } } }) as ModelFabricSettingsStatus);
  render(<NearTeePanel setup={{ ...setup(), vision_max_timeout_seconds: 94, slots: { text: slot, vision: visionSlot } }}
    stale={false} policyRevision={7} laneCeilingMicrousd={1_000_000} onSave={onSave} />);
  // Unchanged consented slot: a save omits the screenshot field (consent carries).
  fireEvent.click(screen.getByRole("button", { name: "Save NEAR TEE vision slot" }));
  await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
  const carried = (onSave.mock.calls[0][0].near_tee as { slots: { vision: Record<string, unknown> } }).slots.vision;
  expect(carried).not.toHaveProperty("screenshot_plaintext_egress_acknowledged");
  fireEvent.click(screen.getByRole("button", { name: "Withdraw screenshot consent" }));
  await waitFor(() => expect(onSave).toHaveBeenCalledTimes(2));
  const withdrawn = (onSave.mock.calls[1][0].near_tee as { slots: { vision: Record<string, unknown> } }).slots.vision;
  expect(withdrawn.screenshot_plaintext_egress_acknowledged).toBe(false);
  fireEvent.change(screen.getByLabelText("NEAR TEE vision request timeout"), { target: { value: "100" } });
  fireEvent.click(screen.getByLabelText("Acknowledge NEAR TEE vision plaintext access"));
  fireEvent.click(screen.getByLabelText("Acknowledge screenshots sent to NEAR in plaintext"));
  fireEvent.click(screen.getByRole("button", { name: "Save NEAR TEE vision slot" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("between 1 and 94");
  expect(onSave).toHaveBeenCalledTimes(2);
});

it("shows inferred readiness, reasoning guidance and runs the text structured output check (#1167)", async () => {
  const deepseek = "deepseek-ai/DeepSeek-V4.1-Flash-1017";
  const raw = { schema_version: "seraph.model-fabric.settings.v1", status: "blocked", egress_revision: 5, egress_revoked: false,
    near_tee: { ...setup(), reasoning_recommended_max_output_tokens: 4096, slots: { text: { ...slot, model_id: deepseek,
      status: "ready", reason_code: null, catalog_receipt_current: true, tee_hosted_per_catalog: true,
      structured_output_basis: null, structured_output_capability: "proof_missing:structured_output",
      catalog_receipt: { model_id: deepseek, checked_at_iso: "2026-11-14T00:00:00+00:00", outcome: "passed",
        reason_code: null, required_cost_microusd: 7373, structured_outputs: false, is_ready: null,
        readiness_basis: "absent_flag_attested", features: ["reasoning", "tools"],
        input_price_usd_per_million: "0.300000000", output_price_usd_per_million: "1.200000000", image_price_usd: null } } } } };
  const next = normalizeModelFabricSettings(raw);
  const text = next?.near_tee?.slots.text;
  expect(text?.catalog_receipt?.readiness_basis).toBe("absent_flag_attested");
  expect(text?.catalog_receipt?.is_ready).toBeNull();
  expect(text?.structured_output_capability).toBe("proof_missing:structured_output");
  expect(next?.near_tee?.reasoning_recommended_max_output_tokens).toBe(4096);
  const fetch = vi.fn(async () => new Response(JSON.stringify({ outcome: "passed" }), { status: 200 }));
  vi.stubGlobal("fetch", fetch);
  render(<NearTeePanel setup={next?.near_tee} stale={false} policyRevision={5} laneCeilingMicrousd={1_000_000} onSave={vi.fn()} />);
  expect(screen.getByText(/Readiness inferred: NEAR's catalog omits isReady/)).toBeInTheDocument();
  expect(screen.getByText(/unavailable until a structured output check passes/)).toBeInTheDocument();
  expect(screen.getByText(/Use at least 4096/)).toBeInTheDocument();
  // 8192 x $0.30/M + 4096 x $1.20/M = $0.007373 (ceil), shown against the current reserve.
  expect(screen.getByText(/needs a per-request reserve of at least \$0\.007373 .*reserve now \$0\.02\)/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Run structured output check" }));
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
  const [, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
  expect(JSON.parse(String(init.body))).toMatchObject({ profile_id: "near_tee.text", capability: "structured_output" });
  expect(JSON.parse(String(init.body))).not.toHaveProperty("structured_mode");
  expect(await screen.findByRole("status")).toHaveTextContent("Structured output check passed");
});

it("establishes the vision JSON-schema mode only through an explicit check (#1167)", async () => {
  const deepseek = "deepseek-ai/DeepSeek-V4.1-Flash-1017";
  const receipt = { model_id: deepseek, checked_at_iso: "2026-11-14T00:00:00+00:00", outcome: "passed",
    reason_code: null, required_cost_microusd: 9831, structured_outputs: false, readiness_basis: "absent_flag_attested",
    features: ["reasoning", "tools"] };
  const visionSlot: NearTeeSlotStatus = { ...slot, model_id: deepseek, profile_id: "near_tee.vision", max_output_tokens: 4096,
    catalog_receipt_current: true, status: "ready", reason_code: null, structured_output_basis: null, catalog_receipt: receipt };
  const fetch = vi.fn(async () => new Response(JSON.stringify({ outcome: "passed" }), { status: 200 }));
  vi.stubGlobal("fetch", fetch);
  const { rerender } = render(<NearTeePanel setup={{ ...setup(), slots: { text: slot, vision: visionSlot } }} stale={false}
    policyRevision={5} laneCeilingMicrousd={1_000_000} onSave={vi.fn()} />);
  expect(screen.getByText(/no current JSON-schema check: prompt-JSON requests/)).toBeInTheDocument();
  expect(screen.queryByText(/Use at least/)).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Run JSON-schema structured output check" }));
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
  const [, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
  expect(JSON.parse(String(init.body))).toMatchObject({ profile_id: "near_tee.vision", capability: "structured_output",
    structured_mode: "json_schema" });
  rerender(<NearTeePanel setup={{ ...setup(), slots: { text: slot, vision: { ...visionSlot, structured_output_basis: "capability_proof" } } }}
    stale={false} policyRevision={5} laneCeilingMicrousd={1_000_000} onSave={vi.fn()} />);
  expect(screen.getByText(/a current capability check established JSON schema requests/)).toBeInTheDocument();
  // With JSON schema established by a proof, the explicit way back is a prompt-JSON check.
  expect(screen.queryByRole("button", { name: "Run JSON-schema structured output check" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Run prompt-JSON check" }));
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
  const [, again] = fetch.mock.calls[1] as unknown as [string, RequestInit];
  expect(JSON.parse(String(again.body))).toMatchObject({ capability: "structured_output", structured_mode: "prompt_json" });
});

it("explains that a failed JSON-schema check withdraws JSON schema requests (#1167)", async () => {
  const deepseek = "deepseek-ai/DeepSeek-V4.1-Flash-1017";
  const visionSlot: NearTeeSlotStatus = { ...slot, model_id: deepseek, profile_id: "near_tee.vision", status: "ready", reason_code: null,
    catalog_receipt_current: true, structured_output_basis: null, catalog_receipt: { model_id: deepseek, checked_at_iso: null,
      outcome: "passed", reason_code: null, required_cost_microusd: 9831, structured_outputs: false } };
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ outcome: "failed", error_code: "canary_shape_invalid" }), { status: 200 })));
  render(<NearTeePanel setup={{ ...setup(), slots: { text: slot, vision: visionSlot } }} stale={false}
    policyRevision={5} laneCeilingMicrousd={1_000_000} onSave={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "Run JSON-schema structured output check" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/JSON schema requests are withdrawn; the vision slot stays blocked until a prompt-JSON check passes/);
});

it("shows the receipt basis, last confirmation and a non-blocking catalog warning, never an expiry (#1178)", () => {
  const raw = { ...saved(5), near_tee: setup({ ...slot, status: "ready", reason_code: null,
    catalog_receipt_current: true, tee_hosted_per_catalog: true,
    tee_hosting_label: "TEE-hosted per NEAR catalog as of 2026-11-14T06:00:00+00:00; NEAR gateway receives plaintext; attestation not verified by Seraph; no end-to-end encryption",
    catalog_receipt: { model_id: "Qwen/Qwen3.8-27B", checked_at_iso: "2026-11-10T00:00:00+00:00", basis: "setup_check",
      outcome: "passed", reason_code: null, required_cost_microusd: 1711 },
    catalog_last_confirmed_at: "2026-11-14T06:00:00+00:00", catalog_last_confirmed_basis: "background_read",
    catalog_warning: { reason_code: "near_tee_catalog_not_ready", since: "2026-11-14T12:00:00+00:00",
      message: "NEAR catalog reports Qwen/Qwen3.8-27B not ready since 2026-11-14T12:00:00+00:00. Requests are not blocked; the per-request model echo and provider errors still apply." },
    catalog_background_read: { last_attempt_at: "2026-11-14T12:00:00+00:00", last_attempt_outcome: "read", last_attempt_error: null } }) };
  const normalized = normalizeModelFabricSettings(raw);
  render(<NearTeePanel setup={normalized?.near_tee} stale={false} policyRevision={5} laneCeilingMicrousd={1_000_000} onSave={vi.fn()} />);
  expect(screen.getByText(/Catalog basis: setup check at 2026-11-10T00:00:00\+00:00 · last confirmed 2026-11-14T06:00:00\+00:00 \(background read\)/)).toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent("NEAR catalog reports Qwen/Qwen3.8-27B not ready since 2026-11-14T12:00:00+00:00");
  expect(screen.getByLabelText("NEAR TEE catalog refresh hours")).toHaveValue(24);
  expect(screen.queryByText(/expires|lifetime/i)).toBeNull();
  expect(screen.queryByText(/^Blocked:/)).toBeNull();
});
