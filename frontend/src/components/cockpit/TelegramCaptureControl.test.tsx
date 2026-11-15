import { StrictMode } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { GoalInfo } from "../../types";
import { TelegramCaptureControl } from "./TelegramCaptureControl";

const goal: GoalInfo = { id: "goal-1", title: "Current Goal", revision: 2, owner_session_id: "root-1", status: "active", parent_id: null, path: "goal-1", level: "objective", description: null, domain: "work", start_date: null, due_date: null, sort_order: 0 };
afterEach(() => { vi.unstubAllGlobals(); });

it("requires an actual current pairing and saves default-disabled zero-cost capture independently of inference consent", async () => {
  const fetch = vi.fn().mockResolvedValueOnce({ ok: true, json: async () => ({ state_revision: 7, pairing_state: "active", owner_principal_id: "owner-1", operator_session_id: "root-1", live_transport: false }) })
    .mockResolvedValueOnce({ ok: true, json: async () => ({ state_revision: 8 }) });
  vi.stubGlobal("fetch", fetch);
  render(<StrictMode><TelegramCaptureControl ownerPrincipalId="owner-1" ownerSessionId="root-1" goals={[goal]} /></StrictMode>);
  expect(fetch).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "Save Telegram capture selection" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Refresh current Telegram pairing" }));
  await screen.findByText(/Live Telegram delivery is unverified/);
  fireEvent.change(screen.getByLabelText("Telegram capture Goal"), { target: { value: goal.id } });
  fireEvent.click(screen.getByRole("button", { name: "Save Telegram capture selection" }));
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
  const body = JSON.parse(fetch.mock.calls[1][1].body);
  expect(body).toMatchObject({ expected_revision: 7, enabled: false, goal_id: "goal-1", goal_revision: 2, inference_egress_acknowledged: false, limits: { max_inference_calls: 0, max_cost_microusd: 0, wall_seconds: 900 } });
  expect(body.document_acquisition).toBeNull();
  expect(Object.keys(body).sort()).toEqual(["document_acquisition", "enabled", "expected_revision", "goal_id", "goal_revision", "inference_egress_acknowledged", "limits", "requested_output"]);
});

it("separately opts into one future private document and clears that selection on Goal revision change", async () => {
  const fetch = vi.fn().mockResolvedValueOnce({ ok: true, json: async () => ({ state_revision: 7, pairing_state: "active", owner_principal_id: "owner-1", operator_session_id: "root-1", live_transport: false }) })
    .mockResolvedValueOnce({ ok: true, json: async () => ({ state_revision: 8 }) });
  vi.stubGlobal("fetch", fetch);
  const view = render(<TelegramCaptureControl ownerPrincipalId="owner-1" ownerSessionId="root-1" goals={[goal]} />);
  const acquisition = screen.getByRole("checkbox", { name: /Acquire one future original/ });
  expect(acquisition).not.toBeChecked();
  expect(acquisition).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Refresh current Telegram pairing" }));
  await screen.findByText(/Live Telegram delivery is unverified/);
  fireEvent.change(screen.getByLabelText("Telegram capture Goal"), { target: { value: goal.id } });
  fireEvent.click(screen.getByRole("checkbox", { name: "Enable Task capture for this selected Goal" }));
  fireEvent.click(acquisition);
  fireEvent.click(screen.getByRole("button", { name: "Save Telegram capture selection" }));
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
  expect(JSON.parse(fetch.mock.calls[1][1].body)).toMatchObject({ document_acquisition: {
    action: "acquire_one_original_task_document", max_sources: 1, source_cap_bytes: 16777216,
    docx_cap_bytes: 10485760, formats: ["pdf", "docx", "xlsx", "csv"], no_learning: true },
    inference_egress_acknowledged: false, limits: { max_inference_calls: 0, max_cost_microusd: 0 } });
  view.rerender(<TelegramCaptureControl ownerPrincipalId="owner-1" ownerSessionId="root-1" goals={[{ ...goal, revision: 3 }]} />);
  expect(acquisition).not.toBeChecked();
});

it("aborts and discards a late pairing response after original Root changes", async () => {
  let resolve!: (value: unknown) => void;
  const fetch = vi.fn((_url: RequestInfo | URL, _init?: RequestInit) => new Promise(done => { resolve = done; }));
  vi.stubGlobal("fetch", fetch);
  const view = render(<TelegramCaptureControl ownerPrincipalId="owner-1" ownerSessionId="root-1" goals={[goal]} />);
  fireEvent.click(screen.getByRole("button", { name: "Refresh current Telegram pairing" }));
  const signal = fetch.mock.calls[0][1]?.signal;
  view.rerender(<TelegramCaptureControl ownerPrincipalId="owner-1" ownerSessionId="root-2" goals={[]} />);
  expect(signal?.aborted).toBe(true);
  resolve({ ok: true, json: async () => ({ state_revision: 7, pairing_state: "active", owner_principal_id: "owner-1", operator_session_id: "root-1", live_transport: true }) });
  await waitFor(() => expect(screen.getByRole("button", { name: "Save Telegram capture selection" })).toBeDisabled());
  expect(screen.queryByText(/Current pairing loaded/)).not.toBeInTheDocument();
});
