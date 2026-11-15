import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { normalizeInferenceLane, normalizeModelFabricSettings, type ModelFabricSettingsStatus } from "../../lib/modelFabric";
import { InferenceLanePanel } from "./InferenceLanePanel";

const lane = normalizeInferenceLane({
  schema_version: "seraph.inference.lane.v1", spend_ceiling_microusd: 25_000, max_queued: 8, max_inflight: 1,
  max_outstanding_per_owner: 4, max_retries: 1, published: false, status: "migration_pending",
});

describe("InferenceLanePanel", () => {
  it("normalizes only the closed lane contract", () => {
    expect(lane?.max_queued).toBe(8);
    expect(normalizeInferenceLane({ ...lane, max_inflight: 2 })).toBeNull();
    expect(normalizeInferenceLane({ ...lane, schema_version: "other" })).toBeNull();
    expect(normalizeInferenceLane({ ...lane, spend_ceiling_microusd: 0 })).toBeNull();
  });

  it("publishes the provider-neutral lane with the current revision", async () => {
    const onSave = vi.fn(async () => ({ egress_revision: 5, inference_lane: { ...lane!, published: true } }) as unknown as ModelFabricSettingsStatus);
    render(<InferenceLanePanel lane={lane} stale={false} policyRevision={3} onSave={onSave} />);
    expect(screen.getByText("migration pending · save to publish")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Shared inference ceiling (USD)"), { target: { value: "0.04" } });
    fireEvent.click(screen.getByRole("button", { name: "Save shared lane" }));
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave).toHaveBeenCalledWith({ expected_policy_revision: 3, inference_lane: {
      spend_ceiling_microusd: 40_000, max_queued: 8, max_inflight: 1, max_outstanding_per_owner: 4, max_retries: 1 } });
    expect(await screen.findByRole("status")).toBeTruthy();
  });

  it("keeps controls usable when metadata is stale and refuses invalid bounds locally", async () => {
    const onSave = vi.fn();
    render(<InferenceLanePanel lane={lane} stale={true} policyRevision={3} onSave={onSave} />);
    expect(screen.getByText("stale retained")).toBeTruthy();
    const queued = screen.getByLabelText("Shared lane max queued") as HTMLInputElement;
    expect(queued.disabled).toBe(false);
    fireEvent.change(queued, { target: { value: "65" } });
    fireEvent.click(screen.getByRole("button", { name: "Save shared lane" }));
    expect((await screen.findByRole("alert")).textContent).toContain("Max queued");
    expect(onSave).not.toHaveBeenCalled();
  });

  it("surfaces an unreadable lane instead of 'not configured' and sends an explicit reserve review", async () => {
    const settings = normalizeModelFabricSettings({ schema_version: "seraph.model-fabric.settings.v1", status: "blocked",
      inference_lane: { status: "blocked", reason_code: "inference_lane_unreadable", published: false } });
    expect(settings?.inference_lane).toBeNull();
    expect(settings?.inference_lane_reason).toBe("inference_lane_unreadable");
    const onSave = vi.fn(async () => ({ egress_revision: 9 }) as unknown as ModelFabricSettingsStatus);
    render(<InferenceLanePanel lane={null} laneReason={settings?.inference_lane_reason} stale={false} policyRevision={7}
      ledgerCeilingMicrousd={25_000} onSave={onSave} />);
    expect(screen.getByText("blocked · inference lane unreadable")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Shared lane reserve review (USD)"), { target: { value: "0.0012" } });
    fireEvent.click(screen.getByRole("button", { name: "Save shared lane" }));
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave).toHaveBeenCalledWith({ expected_policy_revision: 7, inference_lane: { spend_ceiling_microusd: 25_000,
      max_queued: 64, max_inflight: 1, max_outstanding_per_owner: 16, max_retries: 2, request_reserve_review_microusd: 1200 } });
  });
});
