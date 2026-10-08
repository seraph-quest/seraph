import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { NEAR_TEXT_API_BASE, NEAR_TEXT_DISCLOSURE, NEAR_TEXT_MODEL, NEAR_TEXT_PROFILE, normalizeModelFabricSettings, retainModelFabricSettings } from "../../lib/modelFabric";
import type { ModelFabricSettingsStatus, NearTextSetupInput, NearTextSetupStatus } from "../../lib/modelFabric";
import { NearTextPanel } from "./NearTextPanel";

const setup: NearTextSetupStatus = {
  schema_version: "seraph.near.text.v1", enabled: true, profile_id: NEAR_TEXT_PROFILE,
  model_id: NEAR_TEXT_MODEL, api_base: NEAR_TEXT_API_BASE, max_output_tokens: 512,
  timeout_seconds: 45, request_cost_bound_microusd: 1000, spend_ceiling_microusd: 10_000,
  credential_ref: "vault:near_text_api_key", credential_fingerprint: "a".repeat(64),
  plaintext_egress_consent_revision: 7, key_present: true, consent_current: true,
  status: "configured", reason_code: null, tls_transport: true, tee_verified: false,
  e2ee: false, provider_plaintext_disclosure: NEAR_TEXT_DISCLOSURE,
};
function saved(near = setup, revision = 9): ModelFabricSettingsStatus {
  return { schema_version: "seraph.model-fabric.settings.v1", status: "configured", near_text: near,
    egress_revision: revision, egress_revoked: false } as ModelFabricSettingsStatus;
}
const change = (label: string, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } });
const save = () => fireEvent.click(screen.getByRole("button", { name: "Save NEAR text settings" }));
function request(mock: ReturnType<typeof vi.fn>): { expected_policy_revision: number; near_text: NearTextSetupInput } { return mock.mock.calls[0][0]; }
afterEach(() => { vi.unstubAllGlobals(); window.localStorage.clear(); });

it("starts disabled without implicit funding, credentials or provider requests", () => {
  const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
  render(<NearTextPanel setup={null} stale={false} policyRevision={7} onSave={vi.fn()} />);
  expect(screen.getByLabelText("Enable NEAR text")).not.toBeChecked();
  expect(screen.getByLabelText("NEAR shared deployment ceiling USD")).toHaveValue("");
  expect(screen.getByText(/NEAR receives the question in plaintext over HTTPS/)).toBeInTheDocument();
  expect(fetch).not.toHaveBeenCalled();
});

it("requires literal plaintext acknowledgment and sends only exact Near fields at the current revision", async () => {
  const onSave = vi.fn(async () => saved());
  render(<NearTextPanel setup={null} stale={false} policyRevision={7} onSave={onSave} />);
  fireEvent.click(screen.getByLabelText("Enable NEAR text"));
  change("NEAR shared deployment ceiling USD", "0.01"); change("NEAR API key", "synthetic-near-secret");
  save(); expect(await screen.findByRole("alert")).toHaveTextContent("Acknowledge");
  expect(onSave).not.toHaveBeenCalled();
  fireEvent.click(screen.getByLabelText("Acknowledge NEAR plaintext access")); save();
  await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
  expect(request(onSave)).toEqual({ expected_policy_revision: 7, near_text: {
    schema_version: "seraph.near.text.v1", enabled: true, profile_id: NEAR_TEXT_PROFILE,
    model_id: NEAR_TEXT_MODEL, api_base: NEAR_TEXT_API_BASE, max_output_tokens: 1024,
    timeout_seconds: 45, request_cost_bound_microusd: 1000, spend_ceiling_microusd: 10_000,
    plaintext_provider_egress_acknowledged: true, api_key: "synthetic-near-secret",
  } });
  await waitFor(() => expect(screen.getByLabelText("NEAR API key")).toHaveValue(""));
  expect(window.localStorage.length).toBe(0);
});

it("carries only unchanged current consent, omits blank keys and preserves the shared OpenRouter budget", async () => {
  const onSave = vi.fn(async () => saved());
  render(<NearTextPanel setup={setup} stale={false} policyRevision={7} sharedCeilingMicrousd={10_000} sharedCeilingLocked onSave={onSave} />);
  expect(screen.queryByLabelText("Acknowledge NEAR plaintext access")).toBeNull();
  expect(screen.getByLabelText("NEAR shared deployment ceiling USD")).toHaveAttribute("readonly");
  save(); await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
  expect(request(onSave)).toMatchObject({ expected_policy_revision: 7, near_text: { plaintext_provider_egress_acknowledged: false, spend_ceiling_microusd: 10_000 } });
  expect(request(onSave)).not.toHaveProperty("openrouter_setup");
  expect(request(onSave).near_text).not.toHaveProperty("api_key");
  expect(request(onSave).near_text).not.toHaveProperty("credential_fingerprint");
});

it("invalidates acknowledgment after an enabled route change and permits disabling without a new grant", async () => {
  const onSave = vi.fn(async () => saved({ ...setup, enabled: false, consent_current: false, status: "disabled" }));
  render(<NearTextPanel setup={setup} stale={false} policyRevision={7} onSave={onSave} />);
  change("NEAR maximum answer tokens", "500");
  fireEvent.click(screen.getByLabelText("Acknowledge NEAR plaintext access"));
  change("NEAR request timeout", "40");
  expect(screen.getByLabelText("Acknowledge NEAR plaintext access")).not.toBeChecked();
  fireEvent.click(screen.getByLabelText("Enable NEAR text")); save();
  await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
  expect(request(onSave).near_text).toMatchObject({ enabled: false, plaintext_provider_egress_acknowledged: false });
});

it.each([["NEAR per-request reserve USD", "0.010001", "cannot exceed"], ["NEAR per-request reserve USD", "1e-3", "six decimal"], ["NEAR maximum answer tokens", "1025", "1024"], ["NEAR request timeout", "46", "45"]])("rejects %s outside the frozen limits", async (label, value, error) => {
  const onSave = vi.fn(); render(<NearTextPanel setup={setup} stale={false} policyRevision={7} onSave={onSave} />);
  change(label, value); fireEvent.click(screen.getByLabelText("Enable NEAR text")); save();
  expect(await screen.findByRole("alert")).toHaveTextContent(error); expect(onSave).not.toHaveBeenCalled();
});

it("retains drafts on stale metadata and 409, refreshes GET without replaying a save", async () => {
  const onSave = vi.fn().mockRejectedValue(new Error("Request failed: 409"));
  const fetch = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify(saved(setup, 11)))); vi.stubGlobal("fetch", fetch);
  const view = render(<NearTextPanel setup={setup} stale={false} policyRevision={7} onSave={onSave} />);
  change("NEAR maximum answer tokens", "400");
  view.rerender(<NearTextPanel setup={null} stale policyRevision={7} onSave={onSave} />);
  expect(screen.getByText("stale retained")).toBeInTheDocument();
  expect(screen.getByLabelText("NEAR maximum answer tokens")).toHaveValue(400);
  fireEvent.click(screen.getByLabelText("Acknowledge NEAR plaintext access")); save();
  expect(await screen.findByRole("alert")).toHaveTextContent("409");
  fireEvent.click(screen.getByRole("button", { name: "Refresh current NEAR settings" }));
  expect(await screen.findByRole("status")).toHaveTextContent("edits retained");
  expect(fetch.mock.calls[0][0]).toMatch(/\/api\/settings\/model-fabric$/);
  expect(fetch.mock.calls[0][1]).toMatchObject({ credentials: "include" });
  expect(onSave).toHaveBeenCalledTimes(1);
  expect(screen.getByLabelText("NEAR maximum answer tokens")).toHaveValue(400);
});

it("rejects false transport claims and strips accidental GET secrets before metadata retention", () => {
  const normalized = normalizeModelFabricSettings({ ...saved(), near_text: { ...setup, api_key: "must-not-retain" } });
  expect(normalized?.near_text).toEqual(setup); retainModelFabricSettings(normalized!);
  expect(JSON.stringify(window.localStorage)).not.toContain("must-not-retain");
  const unsafe = normalizeModelFabricSettings({ ...saved(), near_text: { ...setup, tee_verified: true } });
  expect(unsafe?.near_text).toBeNull(); expect(unsafe?.near_text_metadata_unavailable).toBe(true);
});
