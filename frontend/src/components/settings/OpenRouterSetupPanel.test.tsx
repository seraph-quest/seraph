import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  normalizeModelFabricSettings,
  retainModelFabricSettings,
  type ModelFabricSettingsStatus,
  type OpenRouterPurpose,
  type OpenRouterRouteStatus,
  type OpenRouterSetupStatus,
  type OpenRouterSetupValue,
} from "../../lib/modelFabric";
import { OpenRouterSetupPanel } from "./OpenRouterSetupPanel";

// Explicit synthetic fixture selections. No transport in these tests contacts a provider.
function route(slot: OpenRouterPurpose): OpenRouterRouteStatus {
  return {
    model_id: "fixture/" + slot, enabled: true,
    capabilities: slot === "vision" ? ["text", "vision", "structured_output"] : slot === "embedding" ? ["embedding"] : ["text", "structured_output"],
    allowed_upstreams: ["fixture-upstream"], temperature: 0.7, max_output_tokens: 4096,
    timeout_seconds: 120, zero_data_retention: slot !== "text", request_cost_bound_microusd: 500,
    status: "blocked", error_code: "capability_proof_missing", proof_expires_at: null,
  };
}
const setup: OpenRouterSetupStatus = {
  schema_version: "seraph.openrouter.setup.v2", profile_id: "openrouter",
  api_base: "https://openrouter.ai/api/v1", provider_kind: "openrouter",
  routes: { text: route("text"), vision: null, embedding: null },
  slot_statuses: {
    text: { status: "blocked", error_code: "capability_proof_missing", proof_expires_at: null },
    vision: { status: "configuration_required", error_code: "route_missing", proof_expires_at: null },
    embedding: { status: "configuration_required", error_code: "route_missing", proof_expires_at: null },
  },
  allow_fallbacks: false, require_parameters: true, data_collection: "deny", data_retention_policy: "deny",
  egress_class: "cloud_allowed_full", cloud_egress_acknowledged: true, spend_ceiling_microusd: 10_000,
  max_queued: 64, max_inflight: 1, max_outstanding_per_owner: 16, max_retries: 2,
  credential_ref: "vault:openrouter_api_key", credential_fingerprint: null, credential_configured: false,
  status: "configuration_required", error_code: "credential_missing", provider_calls: "manual_canary_only",
};
function savedSettings(nextSetup = setup, revision = 9): ModelFabricSettingsStatus {
  return {
    schema_version: "seraph.model-fabric.settings.v1", status: "configured",
    openrouter_setup: nextSetup, egress_revision: revision, egress_revoked: false,
  } as ModelFabricSettingsStatus;
}
function change(label: string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}
function enable(slot: "vision" | "embedding") {
  fireEvent.click(screen.getByLabelText("Enable OpenRouter " + slot + " route"));
  change("OpenRouter " + slot + " model ID", "fixture/" + slot);
  change("OpenRouter " + slot + " upstream allow-list", "fixture-upstream");
  change("OpenRouter " + slot + " request cost bound", "500");
  fireEvent.click(screen.getByLabelText("Enable OpenRouter " + slot + " zero data retention"));
}
function save() {
  fireEvent.click(screen.getByRole("button", { name: "Save OpenRouter setup" }));
}
function request(mock: ReturnType<typeof vi.fn>): { expected_policy_revision: number; openrouter_setup: OpenRouterSetupValue } {
  return mock.mock.calls[0][0];
}
afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});
describe("OpenRouterSetupPanel", () => {
  it("keeps audio absent in legacy saves and upgrades only on explicit optional-slot selection", async () => {
    const onSave = vi.fn(async () => savedSettings());
    render(<OpenRouterSetupPanel setup={setup} stale={false} policyRevision={7} onSave={onSave} />);
    expect(screen.queryByLabelText("OpenRouter audio model ID")).not.toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Configure optional OpenRouter audio slot"));
    expect(screen.getByLabelText("OpenRouter audio model ID")).toHaveValue("");
    expect(screen.getByLabelText("Enable OpenRouter audio route")).not.toBeChecked();
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(request(onSave).openrouter_setup).toMatchObject({ schema_version: "seraph.openrouter.setup.v3", routes: { audio: null } });
    expect(request(onSave).openrouter_setup).not.toHaveProperty("audio_egress_acknowledged");
  });

  it("requires independent audio consent and preserves the blocked server proof status", async () => {
    const v3: OpenRouterSetupStatus = { ...setup, schema_version: "seraph.openrouter.setup.v3", routes: { ...setup.routes, audio: null }, slot_statuses: { ...setup.slot_statuses, audio: { status: "blocked", error_code: "audio_documentary_acquisition_unavailable", proof_expires_at: null } } };
    const onSave = vi.fn(async () => savedSettings(v3));
    render(<OpenRouterSetupPanel setup={v3} stale={false} policyRevision={7} onSave={onSave} />);
    expect(screen.getByTestId("openrouter-audio-status")).toHaveTextContent("audio_documentary_acquisition_unavailable");
    fireEvent.click(screen.getByLabelText("Enable OpenRouter audio route"));
    change("OpenRouter audio model ID", "fixture/audio");
    change("OpenRouter audio upstream allow-list", "fixture/audio");
    change("OpenRouter audio request cost bound", "500");
    save();
    expect(await screen.findByRole("alert")).toHaveTextContent("Acknowledge audio egress");
    expect(onSave).not.toHaveBeenCalled();
    fireEvent.click(screen.getByLabelText("Acknowledge OpenRouter audio egress"));
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(request(onSave).openrouter_setup).toMatchObject({ schema_version: "seraph.openrouter.setup.v3", audio_egress_acknowledged: true, routes: { audio: { capabilities: ["text", "audio_input"], timeout_seconds: 60, zero_data_retention: true } } });
  });

  it("has three independent absent slots without inventing a model or upstream", () => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    render(<OpenRouterSetupPanel setup={null} stale={false} policyRevision={7} onSave={vi.fn()} />);
    for (const slot of ["text", "vision", "embedding"]) {
      expect(screen.getByLabelText("OpenRouter " + slot + " model ID")).toHaveValue("");
      expect(screen.getByLabelText("OpenRouter " + slot + " upstream allow-list")).toHaveValue("");
      expect(screen.getByLabelText("Enable OpenRouter " + slot + " route")).not.toBeChecked();
      expect(screen.getByTestId("openrouter-" + slot + "-status")).toHaveTextContent("configuration required");
    }
    expect(fetch).not.toHaveBeenCalled();
  });

  it("saves exact v2 values/current revision and keeps absent purposes independent and blank key omitted", async () => {
    const onSave = vi.fn(async () => savedSettings());
    render(<OpenRouterSetupPanel setup={setup} stale={false} policyRevision={7} onSave={onSave} />);
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    const payload = request(onSave);
    expect(payload.expected_policy_revision).toBe(7);
    expect(Object.keys(payload).sort()).toEqual(["expected_policy_revision", "openrouter_setup"]);
    expect(payload.openrouter_setup).toMatchObject({
      schema_version: "seraph.openrouter.setup.v2", routes: { vision: null, embedding: null },
      allow_fallbacks: false, require_parameters: true, data_collection: "deny", data_retention_policy: "deny",
      cloud_egress_acknowledged: true, spend_ceiling_microusd: 10_000, max_inflight: 1,
    });
    expect(Object.keys(payload.openrouter_setup.routes.text!).sort()).toEqual([
      "model_id", "enabled", "capabilities", "allowed_upstreams", "temperature", "max_output_tokens",
      "timeout_seconds", "zero_data_retention", "request_cost_bound_microusd",
    ].sort());
    for (const field of ["api_key", "credential_ref", "credential_fingerprint", "status", "proof_expires_at", "purpose_consents", "model_ids"]) {
      expect(payload.openrouter_setup).not.toHaveProperty(field);
    }
    expect(payload.openrouter_setup).not.toHaveProperty("vision_egress_acknowledged");
    expect(payload.openrouter_setup).not.toHaveProperty("embedding_egress_acknowledged");
  });

  it("accepts the outer revision when an initially empty setup finishes loading", async () => {
    const onSave = vi.fn(async () => savedSettings());
    const view = render(<OpenRouterSetupPanel setup={undefined} stale={false} onSave={onSave} />);
    view.rerender(<OpenRouterSetupPanel setup={null} stale={false} policyRevision={7} onSave={onSave} />);
    fireEvent.click(screen.getByLabelText("Enable OpenRouter text route"));
    change("OpenRouter text model ID", "fixture/text");
    change("OpenRouter text upstream allow-list", "fixture-upstream");
    change("OpenRouter text request cost bound", "500");
    change("OpenRouter spend ceiling", "10000");
    fireEvent.click(screen.getByLabelText("Acknowledge OpenRouter cloud egress"));
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(request(onSave).expected_policy_revision).toBe(7);
  });

  it("requires separate fresh purpose acknowledgments and sends independent route selections", async () => {
    const onSave = vi.fn(async () => savedSettings());
    render(<OpenRouterSetupPanel setup={setup} stale={false} policyRevision={7} onSave={onSave} />);
    enable("vision");
    enable("embedding");
    expect(screen.getByLabelText("Acknowledge OpenRouter vision egress")).not.toBeChecked();
    expect(screen.getByLabelText("Acknowledge OpenRouter embedding egress")).not.toBeChecked();
    save();
    await screen.findByText("Acknowledge vision egress for the changed route before saving.");
    expect(onSave).not.toHaveBeenCalled();
    fireEvent.click(screen.getByLabelText("Acknowledge OpenRouter vision egress"));
    save();
    await screen.findByText("Acknowledge embedding egress for the changed route before saving.");
    expect(onSave).not.toHaveBeenCalled();
    fireEvent.click(screen.getByLabelText("Acknowledge OpenRouter embedding egress"));
    change("OpenRouter vision model ID", "fixture/vision-changed");
    expect(screen.getByLabelText("Acknowledge OpenRouter vision egress")).not.toBeChecked();
    expect(screen.getByLabelText("Acknowledge OpenRouter embedding egress")).toBeChecked();
    fireEvent.click(screen.getByLabelText("Acknowledge OpenRouter vision egress"));
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    const value = request(onSave).openrouter_setup;
    expect(value.vision_egress_acknowledged).toBe(true);
    expect(value.embedding_egress_acknowledged).toBe(true);
    expect(value.routes.text!.model_id).toBe("fixture/text");
    expect(value.routes.vision!.model_id).toBe("fixture/vision-changed");
    expect(value.routes.embedding!.model_id).toBe("fixture/embedding");
    expect(value.routes.embedding!.capabilities).toEqual(["embedding"]);
  });

  it("keeps unchanged sensitive routes without prechecked/repeated acknowledgments and retains disabled selections", async () => {
    const existing = { ...setup, routes: { text: route("text"), vision: route("vision"), embedding: route("embedding") } };
    const onSave = vi.fn(async () => savedSettings(existing));
    render(<OpenRouterSetupPanel setup={existing} stale={false} policyRevision={7} onSave={onSave} />);
    expect(screen.queryByLabelText("Acknowledge OpenRouter vision egress")).not.toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Enable OpenRouter embedding route"));
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(request(onSave).openrouter_setup).not.toHaveProperty("vision_egress_acknowledged");
    expect(request(onSave).openrouter_setup).not.toHaveProperty("embedding_egress_acknowledged");
    expect(request(onSave).openrouter_setup.routes.embedding).toMatchObject({ enabled: false, model_id: "fixture/embedding" });
    expect(request(onSave).openrouter_setup.routes.text!.enabled).toBe(true);
  });

  it("preserves last-known status, editable independent controls and dirty drafts through partial metadata", async () => {
    const onSave = vi.fn(async () => savedSettings());
    const view = render(<OpenRouterSetupPanel setup={setup} stale={false} policyRevision={7} onSave={onSave} />);
    change("OpenRouter text model ID", "fixture/edited");
    view.rerender(<OpenRouterSetupPanel setup={null} stale={true} onSave={onSave} />);
    expect(screen.getByLabelText("OpenRouter text model ID")).toHaveValue("fixture/edited");
    expect(screen.getByLabelText("OpenRouter vision model ID")).toBeEnabled();
    expect(screen.getByLabelText("OpenRouter spend ceiling")).toHaveValue(10000);
    expect(screen.getByTestId("openrouter-text-status")).toHaveTextContent("capability_proof_missing");
    expect(screen.getByText("stale retained")).toBeInTheDocument();
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(request(onSave).expected_policy_revision).toBe(7);
    expect(request(onSave).openrouter_setup.routes.text!.model_id).toBe("fixture/edited");
  });

  it("projects server status/reasons/proof expiry for ready, blocked and null purposes separately", () => {
    const current = {
      ...setup, slot_statuses: {
        text: { status: "ready" as const, error_code: null, proof_expires_at: "2026-10-05T20:00:00+00:00" },
        vision: { status: "blocked" as const, error_code: "provider_policy_revoked", proof_expires_at: null },
        embedding: setup.slot_statuses.embedding,
      },
    };
    const normalized = normalizeModelFabricSettings(savedSettings(current));
    expect(normalized?.openrouter_setup?.slot_statuses.text.proof_expires_at).toBe("2026-10-05T20:00:00+00:00");
    render(<OpenRouterSetupPanel setup={normalized?.openrouter_setup ?? null} stale={false} policyRevision={7} onSave={vi.fn()} />);
    expect(screen.getByTestId("openrouter-text-status")).toHaveTextContent("ready · proof expires 2026-10-05T20:00:00+00:00");
    expect(screen.getByTestId("openrouter-vision-status")).toHaveTextContent("blocked · provider_policy_revoked");
    expect(screen.getByTestId("openrouter-embedding-status")).toHaveTextContent("route_missing");
  });

  it("keeps the draft after 409, refreshes only the existing GET, then explicitly saves the new revision", async () => {
    const onSave = vi.fn().mockRejectedValueOnce(new Error("Request failed: 409")).mockResolvedValue(savedSettings());
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      void input; void init;
      return { ok: true, status: 200, json: async () => savedSettings(setup, 11) };
    });
    vi.stubGlobal("fetch", fetch);
    render(<OpenRouterSetupPanel setup={setup} stale={false} policyRevision={7} onSave={onSave} />);
    change("OpenRouter text model ID", "fixture/retained");
    save();
    expect(await screen.findByRole("alert")).toHaveTextContent("409");
    expect(screen.getByLabelText("OpenRouter text model ID")).toHaveValue("fixture/retained");
    fireEvent.click(screen.getByRole("button", { name: "Refresh current OpenRouter settings" }));
    expect(await screen.findByRole("status")).toHaveTextContent("edits retained");
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0][0]).toMatch(/\/api\/settings\/model-fabric$/);
    expect(fetch.mock.calls[0][1]).toMatchObject({ credentials: "include" });
    expect(onSave).toHaveBeenCalledTimes(1);
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(2));
    expect(onSave.mock.calls[1][0].expected_policy_revision).toBe(11);
    expect(onSave.mock.calls[1][0].openrouter_setup.routes.text.model_id).toBe("fixture/retained");
  });

  it("blocks saves without an exact revision and keeps controls usable after a failed refresh", async () => {
    const onSave = vi.fn();
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("metadata unavailable"); }));
    render(<OpenRouterSetupPanel setup={setup} stale={false} onSave={onSave} />);
    save();
    expect(await screen.findByRole("alert")).toHaveTextContent("policy revision");
    fireEvent.click(screen.getByRole("button", { name: "Refresh current OpenRouter settings" }));
    expect(await screen.findByRole("status")).toHaveTextContent("metadata unavailable");
    expect(screen.getByLabelText("OpenRouter text model ID")).toHaveValue("fixture/text");
    expect(screen.getByLabelText("OpenRouter text model ID")).toBeEnabled();
    expect(onSave).not.toHaveBeenCalled();
  });

  it.each([
    ["OpenRouter text model ID", "", "select one qualified"],
    ["OpenRouter text upstream allow-list", "", "explicit upstream"],
    ["OpenRouter text request cost bound", "10001", "request cost bound"],
    ["OpenRouter text temperature", "", "temperature"],
    ["OpenRouter max queued", "65", "Max queued"],
    ["OpenRouter max retries", "3", "Max retries"],
  ])("rejects invalid bound or incomplete selection %s before saving", async (label, value, message) => {
    const onSave = vi.fn();
    render(<OpenRouterSetupPanel setup={setup} stale={false} policyRevision={7} onSave={onSave} />);
    change(label, value);
    save();
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(onSave).not.toHaveBeenCalled();
  });

  it("requires ZDR for newly enabled purposes without affecting text", async () => {
    const onSave = vi.fn();
    render(<OpenRouterSetupPanel setup={setup} stale={false} policyRevision={7} onSave={onSave} />);
    enable("vision");
    fireEvent.click(screen.getByLabelText("Enable OpenRouter vision zero data retention"));
    save();
    expect(await screen.findByRole("alert")).toHaveTextContent("vision: zero data retention is required");
    expect(screen.getByLabelText("Enable OpenRouter text route")).toBeChecked();
    expect(onSave).not.toHaveBeenCalled();
  });

  it("requires fresh cloud/purpose review after revocation", () => {
    const current = { ...setup, routes: { ...setup.routes, vision: route("vision") } };
    render(<OpenRouterSetupPanel setup={current} stale={false} policyRevision={7} policyRevoked onSave={vi.fn()} />);
    expect(screen.getByLabelText("Acknowledge OpenRouter cloud egress")).not.toBeChecked();
    expect(screen.getByLabelText("Acknowledge OpenRouter vision egress")).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Review and re-grant OpenRouter egress" })).toBeEnabled();
  });

  it("sends a write-only key, clears it on success, and retains only fingerprint metadata", async () => {
    const sentinel = "sk-ui-synthetic-secret";
    const configured = { ...setup, credential_configured: true, credential_fingerprint: "0123456789ab" };
    const onSave = vi.fn(async () => savedSettings(configured));
    render(<OpenRouterSetupPanel setup={setup} stale={false} policyRevision={7} onSave={onSave} />);
    change("OpenRouter API key (write-only)", sentinel);
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(request(onSave).openrouter_setup.api_key).toBe(sentinel);
    await waitFor(() => expect(screen.getByLabelText("OpenRouter API key (write-only)")).toHaveValue(""));
    expect(screen.getByText(/fingerprint 0123456789ab/)).toBeInTheDocument();
    expect(screen.queryByText(sentinel)).not.toBeInTheDocument();
    const poisoned = {
      ...savedSettings(configured), api_key: sentinel,
      openrouter_setup: { ...configured, api_key: sentinel, purpose_consents: { vision: 7 },
        routes: { ...configured.routes, text: { ...route("text"), api_key: sentinel } } },
    };
    const normalized = normalizeModelFabricSettings(poisoned)!;
    retainModelFabricSettings(poisoned as ModelFabricSettingsStatus);
    expect(JSON.stringify(normalized)).not.toContain(sentinel);
    expect(JSON.stringify(normalized)).not.toContain("purpose_consents");
    expect(JSON.stringify(window.localStorage)).not.toContain(sentinel);
    expect(normalized.openrouter_setup!.routes.text!.model_id).toBe("fixture/text");
  });
});
