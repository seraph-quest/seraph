import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ModelFabricSettingsStatus, OpenRouterSetupStatus } from "../../lib/modelFabric";
import { OpenRouterSetupPanel } from "./OpenRouterSetupPanel";

const setup: OpenRouterSetupStatus = {
  schema_version: "seraph.openrouter.setup.v1",
  profile_id: "openrouter",
  api_base: "https://openrouter.ai/api/v1",
  provider_kind: "openrouter",
  model_ids: ["anthropic/claude-sonnet-4"],
  capabilities: ["text", "structured_output"],
  temperature: 0.7,
  max_output_tokens: 4096,
  timeout_seconds: 120,
  allowed_upstreams: ["anthropic"],
  allow_fallbacks: false,
  require_parameters: true,
  data_collection: "deny",
  data_retention_policy: "deny",
  zero_data_retention: false,
  egress_class: "cloud_allowed_full",
  cloud_egress_acknowledged: true,
  spend_ceiling_microusd: 10_000,
  max_queued: 64,
  max_inflight: 1,
  max_outstanding_per_owner: 16,
  max_retries: 2,
  credential_ref: "vault:openrouter_api_key",
  credential_fingerprint: null,
  credential_configured: false,
  status: "configuration_required",
  error_code: "credential_missing",
  provider_calls: "manual_canary_only",
};

function savedSettings(nextSetup: OpenRouterSetupStatus): ModelFabricSettingsStatus {
  return { openrouter_setup: nextSetup } as ModelFabricSettingsStatus;
}

describe("OpenRouterSetupPanel", () => {
  it("uses the current OpenRouter target when no setup is persisted", () => {
    render(<OpenRouterSetupPanel setup={null} stale={false} onSave={vi.fn(async () => savedSettings(setup))} />);

    expect(screen.getByLabelText("OpenRouter model IDs")).toHaveValue("z-ai/glm-5.3-flash");
    expect(screen.getByLabelText("OpenRouter upstream allow-list")).toHaveValue("z-ai");
  });

  it("saves policy controls without sending a blank key", async () => {
    const onSave = vi.fn(async (payload: Record<string, unknown>) => {
      void payload;
      return savedSettings(setup);
    });
    render(<OpenRouterSetupPanel setup={setup} stale={false} onSave={onSave} />);

    fireEvent.click(screen.getByRole("button", { name: "Save OpenRouter setup" }));

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    const request = onSave.mock.calls[0][0] as { openrouter: Record<string, unknown> };
    expect(request.openrouter).toMatchObject({
      model_ids: ["anthropic/claude-sonnet-4"],
      capabilities: ["text", "structured_output"],
      allowed_upstreams: ["anthropic"],
      allow_fallbacks: false,
      require_parameters: true,
      data_collection: "deny",
      data_retention_policy: "deny",
      egress_class: "cloud_allowed_full",
      cloud_egress_acknowledged: true,
      spend_ceiling_microusd: 10_000,
    });
    expect(request.openrouter).not.toHaveProperty("api_key");
  });

  it("accepts a write-only key and clears it after a redacted save response", async () => {
    const sentinel = "sk-ui-write-only-sentinel";
    const configured = { ...setup, credential_configured: true, credential_fingerprint: "0123456789ab", status: "configured_unverified" };
    const onSave = vi.fn(async (payload: Record<string, unknown>) => {
      void payload;
      return savedSettings(configured);
    });
    render(<OpenRouterSetupPanel setup={setup} stale={false} onSave={onSave} />);

    fireEvent.change(screen.getByLabelText("OpenRouter API key (write-only)"), { target: { value: sentinel } });
    fireEvent.click(screen.getByRole("button", { name: "Save OpenRouter setup" }));

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    const request = onSave.mock.calls[0][0] as { openrouter: Record<string, unknown> };
    expect(request.openrouter.api_key).toBe(sentinel);
    await waitFor(() => expect(screen.getByLabelText("OpenRouter API key (write-only)")).toHaveValue(""));
    expect(screen.queryByText(sentinel)).not.toBeInTheDocument();
  });
});
