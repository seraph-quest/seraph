import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { InferenceAccountingPanel, spendStateLabel } from "./InferenceAccountingPanel";
import type { InferenceAccountingStatus } from "../../lib/modelFabric";
import { normalizeInferenceAccounting } from "../../lib/modelFabric";

const { apiFetch } = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("../../lib/api", () => ({ apiFetch }));
const settleControl = { action: "settle", endpoint: "/api/settings/model-fabric/accounting/settle", method: "POST",
  expected_revision: 3, job_id: "owned-job", operation_id: "remote:owned-job" };
const accounting: InferenceAccountingStatus = {
  status: "ready", period_id: "2026-10", spent_microusd: 3_200_000, unpriced_requests: 1, history_verified: true,
  operation_count: 1,
  operations: [{ operation_id: "remote:owned-job", job_id: "owned-job", owner_id: "operator:root:owner",
    runtime_path: "chat_agent", state: "unknown", revision: 3, period_id: "2026-09", actual_cost_microusd: null,
    recovery_reason: "provider_cost_readback_required", controls: [settleControl] }],
};

describe("InferenceAccountingPanel", () => {
  it.each([undefined, null])("keeps missing spend metadata usable (%s)", (missing) => {
    render(<InferenceAccountingPanel accounting={missing} stale onRefresh={vi.fn(async () => {})} />);
    expect(screen.getByText("Spend this month is not available.")).toBeInTheDocument();
    expect(screen.getByText("Refresh spend history")).toBeEnabled();
    expect(screen.queryByText("Record the charge")).not.toBeInTheDocument();
  });

  it("shows the month total, the unpriced count and where spending limits live", () => {
    render(<InferenceAccountingPanel accounting={accounting} stale={false} onRefresh={vi.fn(async () => {})} />);
    expect(screen.getByTestId("spend-total")).toHaveTextContent("October 2026: $3.20 spent. 1 request is not priced by NEAR yet (all months).");
    expect(screen.getByText(/Spending limits are set in your NEAR account\./)).toBeInTheDocument();
    expect(screen.queryByText(/blocked/i)).toBeNull();
    expect(screen.queryByText(/ceiling|remaining|reserved/i)).toBeNull();
  });

  it("says nothing was spent in a month without charges", () => {
    render(<InferenceAccountingPanel accounting={{ ...accounting, spent_microusd: 0, unpriced_requests: 0, operations: [] }}
      stale={false} onRefresh={vi.fn(async () => {})} />);
    expect(screen.getByTestId("spend-total")).toHaveTextContent("October 2026: nothing spent.");
  });

  it("reports an unreadable ledger as unavailable without a blocked banner", () => {
    render(<InferenceAccountingPanel accounting={{ ...accounting, status: "unavailable" }} stale={false} onRefresh={vi.fn(async () => {})} />);
    expect(screen.getByRole("status")).toHaveTextContent("Spend this month is not available.");
    expect(screen.queryByText(/Paid inference blocked/)).toBeNull();
  });

  it("labels each request priced, answered or failed without a price, in progress or not sent", () => {
    const shapes = [["settled", null], ["unknown", "provider_cost_readback_required"], ["unknown", "provider_call_failed"],
      ["unknown", "provider_outcome_unknown"], ["contact_started", null], ["reserved", null], ["released", null]] as const;
    const rows = shapes.map(([state, reason], index) => ({
      ...accounting.operations[0], operation_id: "op-" + index, job_id: "job-" + index, state, controls: [],
      recovery_reason: reason, actual_cost_microusd: state === "settled" ? 12_340 : null }));
    render(<InferenceAccountingPanel accounting={{ ...accounting, operations: rows }} stale={false} onRefresh={vi.fn(async () => {})} />);
    const text = screen.getAllByTestId("spend-row").map((row) => row.textContent ?? "");
    expect(text[0]).toContain("chat_agent · priced · $0.01 · month 2026-09");
    expect(text[1]).toContain("· answered, not priced yet ·");
    // A call that failed, or lost its outcome, after contact is never "not priced yet".
    expect(text[2]).toContain("· no answer, not priced ·");
    expect(text[3]).toContain("· no answer, not priced ·");
    expect(text[4]).toContain("· in progress ·");
    expect(text[5]).toContain("· not sent ·");
    expect(text[6]).toContain("· not sent ·");
    expect(text.slice(2, 5).join(" ")).not.toContain("not priced yet");
    expect(spendStateLabel("settled")).toBe("priced");
  });

  it("records a charge only for the exact advertised request then reads spend history back", async () => {
    apiFetch.mockReset();
    apiFetch.mockResolvedValueOnce({ ok: true }).mockResolvedValueOnce({ ok: true,
      json: async () => ({ ...accounting, spent_microusd: 3_200_009, unpriced_requests: 0, operations: [] }) });
    const refresh = vi.fn(async () => {});
    render(<InferenceAccountingPanel accounting={accounting} stale={false} onRefresh={refresh} />);
    fireEvent.click(screen.getByText("Record the charge"));
    expect(screen.getByText(/nothing waits on it/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Declared account charge"), { target: { value: "9" } });
    fireEvent.change(screen.getByLabelText("Settlement evidence SHA-256"), { target: { value: "e".repeat(64) } });
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByText("Save charge"));
    await waitFor(() => expect(refresh).toHaveBeenCalledOnce());
    const [url, options] = apiFetch.mock.calls[0];
    expect(url).toContain("/api/settings/model-fabric/accounting/settle");
    expect(JSON.parse(options.body)).toMatchObject({ operation_id: "remote:owned-job", job_id: "owned-job",
      expected_revision: 3, actual_cost_microusd: 9, evidence_digest: "e".repeat(64) });
    expect(apiFetch.mock.calls[1][0]).toContain("/api/settings/model-fabric/accounting");
    expect(screen.getByTestId("spend-total")).toHaveTextContent("October 2026: $3.20 spent.");
    expect(screen.getByTestId("spend-total")).not.toHaveTextContent("not priced");
  });

  it("refuses an incomplete charge without calling the backend", () => {
    apiFetch.mockReset();
    render(<InferenceAccountingPanel accounting={accounting} stale={false} onRefresh={vi.fn(async () => {})} />);
    fireEvent.click(screen.getByText("Record the charge"));
    fireEvent.change(screen.getByLabelText("Declared account charge"), { target: { value: "9" } });
    fireEvent.change(screen.getByLabelText("Settlement evidence SHA-256"), { target: { value: "not-a-digest" } });
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByText("Save charge"));
    expect(screen.getByRole("alert")).toHaveTextContent("evidence SHA-256");
    expect(apiFetch).not.toHaveBeenCalled();
  });

  it("retains stale history and disables the record control", () => {
    render(<InferenceAccountingPanel accounting={accounting} stale onRefresh={vi.fn(async () => {})} />);
    expect(screen.getByText(/Retained, stale/)).toBeInTheDocument();
    expect(screen.getByText("Record the charge")).toBeDisabled();
  });

  it("rejects a settlement control that names another operation or endpoint", () => {
    const value = normalizeInferenceAccounting({ ...accounting, operations: [{ ...accounting.operations[0],
      controls: [{ ...settleControl, operation_id: "foreign-operation" },
        { ...settleControl, endpoint: "https://foreign.invalid/settle" }] }] });
    expect(value?.operations[0].controls).toEqual([]);
  });

  it("ignores the retired limit fields an older backend may still send", () => {
    const value = normalizeInferenceAccounting({ ...accounting, ceiling_microusd: 1_000_000, remaining_microusd: 0,
      reserved_microusd: 5, unknown_microusd: 9, committed_microusd: 7, period_review: { endpoint: "/x" },
      unreviewed_overruns: [{ operation_id: "a" }], overrun_max_cost_microusd: 3, settings_revision: 4,
      operations: [{ ...accounting.operations[0], bound_microusd: 100 }] });
    expect(value).not.toBeNull();
    for (const key of ["ceiling_microusd", "remaining_microusd", "reserved_microusd", "unknown_microusd", "committed_microusd",
      "period_review", "unreviewed_overruns", "overrun_max_cost_microusd", "settings_revision"]) {
      expect(value).not.toHaveProperty(key);
    }
    expect(value?.spent_microusd).toBe(3_200_000);
    expect(value?.operations).toHaveLength(1);
    expect(Object.keys(value?.operations[0] ?? {})).not.toContain("bound_microusd");
  });
});
