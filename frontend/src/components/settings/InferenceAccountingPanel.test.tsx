import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { InferenceAccountingPanel } from "./InferenceAccountingPanel";
import type { InferenceAccountingStatus } from "../../lib/modelFabric";
import { normalizeInferenceAccounting } from "../../lib/modelFabric";

const { apiFetch } = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("../../lib/api", () => ({ apiFetch }));
const accounting: InferenceAccountingStatus = {
  status: "ready", period_id: "2026-10", settings_revision: 2,
  committed_microusd: 7, reserved_microusd: 0, unknown_microusd: 100, remaining_microusd: 893,
  operations: [{ operation_id: "remote:owned-job", job_id: "owned-job", owner_id: "operator:root:owner",
    runtime_path: "chat_agent", state: "unknown", revision: 3, bound_microusd: 100, period_id: "2026-09",
    recovery_reason: "provider_cost_readback_required", controls: [{ action: "settle",
      endpoint: "/api/settings/model-fabric/accounting/settle", method: "POST", expected_revision: 3,
      job_id: "owned-job", operation_id: "remote:owned-job" }] }],
};

describe("InferenceAccountingPanel", () => {
  it.each([undefined, null])("keeps missing accounting metadata usable (%s)", (missing) => {
    render(<InferenceAccountingPanel accounting={missing} stale onRefresh={vi.fn(async () => {})} />);
    expect(screen.getByText(/Paid inference blocked/)).toBeInTheDocument();
    expect(screen.getByText("Refresh accounting")).toBeEnabled();
    expect(screen.queryByText("Reconcile exact cost operation")).not.toBeInTheDocument();
  });

  it("settles only the exact advertised operation then reads accounting back", async () => {
    apiFetch.mockReset();
    apiFetch.mockResolvedValueOnce({ ok: true }).mockResolvedValueOnce({ ok: true,
      json: async () => ({ ...accounting, committed_microusd: 16, unknown_microusd: 0,
        remaining_microusd: 984, operations: [] }) });
    const refresh = vi.fn(async () => {});
    render(<InferenceAccountingPanel accounting={accounting} stale={false} onRefresh={refresh} />);
    fireEvent.click(screen.getByText("Reconcile exact cost operation"));
    expect(screen.getByText(/remains externally unverified/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Declared account charge"), { target: { value: "9" } });
    fireEvent.change(screen.getByLabelText("Settlement evidence SHA-256"), { target: { value: "e".repeat(64) } });
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByText("Record declared settlement"));
    await waitFor(() => expect(refresh).toHaveBeenCalledOnce());
    const [url, options] = apiFetch.mock.calls[0];
    expect(url).toContain("/api/settings/model-fabric/accounting/settle");
    expect(JSON.parse(options.body)).toMatchObject({ operation_id: "remote:owned-job", job_id: "owned-job",
      expected_revision: 3, actual_cost_microusd: 9, evidence_digest: "e".repeat(64) });
    expect(apiFetch.mock.calls[1][0]).toContain("/api/settings/model-fabric/accounting");
    expect(screen.getByText(/remaining 984 micro USD/)).toBeInTheDocument();
  });

  it("retains stale totals and disables recovery controls", () => {
    render(<InferenceAccountingPanel accounting={accounting} stale onRefresh={vi.fn(async () => {})} />);
    expect(screen.getByText(/retained, stale/)).toBeInTheDocument();
    expect(screen.getByText("Reconcile exact cost operation")).toBeDisabled();
  });

  it("rejects a settlement control that names another operation or endpoint", () => {
    const value = normalizeInferenceAccounting({ ...accounting, operations: [{ ...accounting.operations[0],
      controls: [{ ...accounting.operations[0].controls![0], operation_id: "foreign-operation" },
        { ...accounting.operations[0].controls![0], endpoint: "https://foreign.invalid/settle" }] }] });
    expect(value?.operations[0].controls).toEqual([]);
  });

  it("reviews only the exact advertised observed month and reads retained accounting back", async () => {
    apiFetch.mockReset();
    const blocked = { ...accounting, status: "blocked" as const, accounting_continuity_verified: true,
      reason_code: "accounting_period_review_required", revision: 5, period_high_water: "2026-10",
      period_review: { endpoint: "/api/settings/model-fabric/accounting/period", method: "POST" as const,
        period_id: "2026-10", expected_revision: 5, scope: "deployment_accounting" as const } };
    apiFetch.mockResolvedValueOnce({ ok: true }).mockResolvedValueOnce({ ok: true,
      json: async () => ({ ...blocked, status: "ready", period_review: null }) });
    const refresh = vi.fn(async () => {});
    render(<InferenceAccountingPanel accounting={blocked} stale={false} onRefresh={refresh} />);
    expect(screen.getByText("Acknowledge observed UTC month")).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByText("Acknowledge observed UTC month"));
    await waitFor(() => expect(refresh).toHaveBeenCalledOnce());
    expect(apiFetch.mock.calls[0][0]).toContain(blocked.period_review.endpoint);
    expect(JSON.parse(apiFetch.mock.calls[0][1].body)).toEqual({ period_id: "2026-10", expected_revision: 5 });
    expect(apiFetch.mock.calls[1][0]).toContain("/api/settings/model-fabric/accounting");
  });

  it("rejects period controls naming a foreign period or revision", () => {
    const control = { endpoint: "/api/settings/model-fabric/accounting/period", method: "POST",
      period_id: "2026-10", expected_revision: 5, scope: "deployment_accounting" };
    expect(normalizeInferenceAccounting({ ...accounting, revision: 5,
      period_review: { ...control, period_id: "2027-01" } })?.period_review).toBeNull();
    expect(normalizeInferenceAccounting({ ...accounting, revision: 5,
      period_review: { ...control, expected_revision: 4 } })?.period_review).toBeNull();
    expect(normalizeInferenceAccounting({ ...accounting, revision: Infinity,
      period_review: { ...control, expected_revision: Infinity } })?.period_review).toBeNull();
  });
});
