import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { normalizeModelFabricSettings } from "../../lib/modelFabric";
import type { ModelFabricSettingsStatus } from "../../lib/modelFabric";
import { ModelCard, modelCardState } from "./ModelCard";
import type { ChatRouteReadiness } from "./ModelCard";

const CONSENT = "I understand NEAR sees my messages as plain text: no end-to-end encryption, and Seraph does not verify NEAR's secure hardware.";
const MODEL = "deepseek-ai/DeepSeek-V4.1-Flash-1017";

type Raw = Record<string, unknown>;
function rawSlot(overrides: Raw = {}): Raw {
  return {
    model_id: MODEL, enabled: true, max_input_tokens: 8192, max_output_tokens: 4096, timeout_seconds: 30.0,
    temperature: null, request_cost_bound_microusd: 20_000, catalog_ttl_hours: 24, profile_id: "near_tee.text",
    consent_current: true, status: "ready", reason_code: null, catalog_receipt: { model_id: MODEL, outcome: "passed",
      checked_at_iso: "2026-11-18T10:00:00+00:00", reason_code: null }, catalog_receipt_current: true,
    tee_hosted_per_catalog: true, tee_hosting_label: "TEE-hosted per NEAR catalog", capability_check_timeout_seconds: 46,
    chat_capabilities: { streaming: "ready", tool_use: "ready" }, structured_output_capability: "ready",
    ...overrides,
  };
}
function raw({ slot = rawSlot(), near = {}, top = {}, accounting = {}, lane = {} }: { slot?: Raw | null; near?: Raw; top?: Raw; accounting?: Raw; lane?: Raw | null } = {}): Raw {
  return {
    schema_version: "seraph.model-fabric.settings.v1", status: "ready", error_code: null, configuration_status: "ready",
    egress_revision: 8, egress_revoked: false,
    near_tee: { configured: true, status: "ready", reason_code: null, key_present: true, credential_fingerprint: "553c52e01ac7",
      tee_verified: false, e2ee: false, slots: { text: slot }, ...near },
    inference_lane: lane === null ? null : { schema_version: "seraph.inference.lane.v1", spend_ceiling_microusd: 1_000_000_000,
      max_queued: 64, max_inflight: 1, max_outstanding_per_owner: 16, max_retries: 2, request_reserve_review_microusd: null,
      published: true, status: "published", ...lane },
    inference_accounting: { status: "ready", ceiling_microusd: 1_000_000_000, committed_microusd: 5_249, unknown_microusd: 0,
      remaining_microusd: 999_934_751, overrun_max_cost_microusd: 0, unreviewed_overruns: [], operations: [], ...accounting },
    ...top,
  };
}
const settingsOf = (value: Raw) => normalizeModelFabricSettings(value) as ModelFabricSettingsStatus;
const READY: ChatRouteReadiness = { inference_ready: true, reasons: [] };
const BLOCKED = (...reasons: string[]): ChatRouteReadiness => ({ inference_ready: false, reasons });

/** The state you are in today (2026-11-18): consent withdrawn by the revoke, receipt stale. */
const revokedRaw = () => raw({
  top: { egress_revoked: true, inference_block_reason: "provider_policy_revoked" },
  near: { status: "blocked", reason_code: "provider_policy_revoked" },
  slot: rawSlot({ consent_current: false, status: "blocked", reason_code: "provider_policy_revoked", catalog_receipt_current: false,
    chat_capabilities: {}, structured_output_capability: null }),
});
const afterCatalogCheck = (overrides: Raw = {}) => raw({ top: { egress_revision: 11 },
  slot: rawSlot({ status: "configuration_required", reason_code: null, ...overrides }) });

function json(body: unknown, status = 200) { return new Response(JSON.stringify(body), { status }); }
function renderCard(settings: ModelFabricSettingsStatus | null, route: ChatRouteReadiness | null, extra: Partial<Parameters<typeof ModelCard>[0]> = {}) {
  const props = {
    settings, route, stale: false,
    onSave: vi.fn(async (_payload: Record<string, unknown>) => settingsOf(raw({ top: { egress_revision: 10 } }))),
    onSettings: vi.fn(), onRefresh: vi.fn(async () => {}), onOpenDiagnostics: vi.fn(), ...extra,
  };
  return { ...render(<ModelCard {...props} />), props };
}
const card = () => screen.getByRole("region", { name: "Model card" });
const statusLine = () => within(card()).getAllByRole("status")[0];

afterEach(() => { vi.unstubAllGlobals(); window.localStorage.clear(); });

describe("modelCardState (appendix B)", () => {
  it.each([
    ["unreadable", raw({ top: { configuration_status: "degraded" } }), READY],
    ["unreadable", raw({ top: { error_code: "provider_policy_continuity_unavailable" } }), READY],
    ["missing_key", raw({ near: { key_present: false } }), READY],
    ["missing_key", raw(), BLOCKED("near_tee_credential_required")],
    ["no_budget", raw({ lane: null }), READY],
    ["no_budget", raw({ lane: { published: false } }), READY],
    ["no_budget", raw(), BLOCKED("near_tee_request_reserve_exceeds_lane_ceiling")],
    ["no_budget", raw({ lane: null, near: { key_present: false } }), READY],
    ["not_set_up", raw({ slot: null }), READY],
    ["not_set_up", raw({ slot: rawSlot({ enabled: false }) }), READY],
    ["needs_ok", revokedRaw(), BLOCKED("provider_policy_revoked", "near_tee_catalog_receipt_stale")],
    ["needs_ok", raw({ slot: rawSlot({ consent_current: false }) }), READY],
    ["overrun", raw({ accounting: { unreviewed_overruns: [{ operation_id: "op", job_id: "job", bound_microusd: 20_000, actual_cost_microusd: 30_000 }] } }), READY],
    ["overrun", raw(), BLOCKED("provider_charge_exceeded_reservation")],
    ["budget_used", raw({ accounting: { remaining_microusd: 10_000 } }), READY],
    ["needs_test", raw({ slot: rawSlot({ catalog_receipt_current: false, chat_capabilities: {} }) }), READY],
    ["needs_test", raw({ slot: rawSlot({ reason_code: "proof_missing:health" }) }), BLOCKED("proof_missing:health")],
    ["needs_test", raw({ slot: rawSlot({ chat_capabilities: { streaming: "proof_missing:streaming", tool_use: "ready" } }) }), READY],
    ["ready", raw(), READY],
    ["blocked", raw(), BLOCKED("near_tee_slot_timeout_exceeds_chat_turn")],
    // The route alone can say ready while the slot is blocked by accounting or proof metadata.
    ["blocked", raw({ slot: rawSlot({ status: "blocked", reason_code: "accounting_period_review_required" }) }), READY],
    ["blocked", raw({ slot: rawSlot({ status: "blocked", reason_code: "inference_accounting_unavailable" }) }), READY],
    ["blocked", raw({ slot: rawSlot({ status: "blocked", reason_code: "proof_metadata_unavailable" }) }), READY],
    ["blocked", raw({ slot: rawSlot({ status: "blocked", reason_code: "near_tee_credential_unavailable" }) }), READY],
    ["new_month", raw({ accounting: { status: "blocked", reason_code: "accounting_period_review_required", revision: 483, period_id: "2026-12",
      period_review: { endpoint: "/api/settings/model-fabric/accounting/period", method: "POST", period_id: "2026-12", expected_revision: 483 } },
      slot: rawSlot({ status: "blocked", reason_code: "accounting_period_review_required" }) }), READY],
    // An open overrun refuses every NEAR save, so it shows before the consent step.
    ["overrun", raw({ top: { egress_revoked: true }, slot: rawSlot({ consent_current: false }),
      accounting: { unreviewed_overruns: [{ operation_id: "op", job_id: "job", bound_microusd: 20_000, actual_cost_microusd: 30_000 }] } }), BLOCKED("provider_policy_revoked")],
    ["route_unknown", raw(), null],
  ] as Array<[string, Raw, ChatRouteReadiness | null]>)("%s", (expected, value, route) => {
    expect(modelCardState(settingsOf(value), route)).toBe(expected);
  });

  it("shows NEAR down only from this session's failed check, and a missing structured check never blocks chat", () => {
    expect(modelCardState(settingsOf(raw()), READY, true)).toBe("near_down");
    expect(modelCardState(settingsOf(raw({ slot: rawSlot({ structured_output_capability: "proof_missing:structured_output" }) })), READY)).toBe("ready");
    expect(modelCardState(null, null)).toBe("loading");
  });
});

describe("Model card", () => {
  it("needs your OK: the tick is never pre-ticked, the button waits for it, and the cost shows before the click", () => {
    const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
    renderCard(settingsOf(revokedRaw()), BLOCKED("provider_policy_revoked"));
    expect(statusLine()).toHaveTextContent("Chat is paused. It needs your OK.");
    expect(card()).toHaveTextContent("DeepSeek V4.1 Flash, hosted by NEAR");
    const tick = screen.getByRole("checkbox", { name: CONSENT });
    expect(tick).not.toBeChecked();
    const allow = screen.getByRole("button", { name: "Allow and turn on chat" });
    expect(allow).toBeDisabled();
    expect(screen.getByTestId("model-card-cost")).toHaveTextContent("up to 6 tiny paid requests, at most $0.12 in total");
    fireEvent.click(allow);
    fireEvent.click(tick);
    expect(allow).toBeEnabled();
    fireEvent.click(tick);
    expect(allow).toBeDisabled();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("allow and turn on chat: saves the OK with the stored slot and no key, checks the catalog, then runs only the missing checks", async () => {
    const calls: Array<{ url: string; body: Record<string, unknown> }> = [];
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      calls.push({ url, body: JSON.parse(String(init?.body ?? "{}")) });
      if (url.includes("catalog-check")) return json(afterCatalogCheck({ reason_code: "proof_missing:health",
        chat_capabilities: { streaming: "proof_missing:streaming", tool_use: "ready" }, structured_output_capability: "ready" }));
      return json({ outcome: "passed", error_code: null });
    });
    vi.stubGlobal("fetch", fetch);
    const onSave = vi.fn(async (_payload: Record<string, unknown>) => settingsOf(raw({ top: { egress_revision: 10 } })));
    const { props } = renderCard(settingsOf(revokedRaw()), BLOCKED("provider_policy_revoked"), { onSave });
    fireEvent.click(screen.getByRole("checkbox", { name: CONSENT }));
    fireEvent.click(screen.getByRole("button", { name: "Allow and turn on chat" }));
    expect(statusLine()).toHaveTextContent("Turning chat on…");
    await waitFor(() => expect(props.onRefresh).toHaveBeenCalled());

    expect(onSave).toHaveBeenCalledTimes(1);
    const payload = onSave.mock.calls[0][0] as { expected_policy_revision: number; near_tee: Record<string, unknown> };
    expect(payload.expected_policy_revision).toBe(8);
    expect(payload.near_tee).not.toHaveProperty("api_key");
    expect(payload.near_tee.slots).toEqual({ text: { model_id: MODEL, enabled: true, max_input_tokens: 8192, max_output_tokens: 4096,
      timeout_seconds: 30, request_cost_bound_microusd: 20_000, catalog_ttl_hours: 24, plaintext_provider_egress_acknowledged: true } });
    // The catalog check runs at the revision the save returned, before any paid check.
    expect(calls[0].url).toContain("/api/settings/model-fabric/near-tee/catalog-check");
    expect(calls[0].body).toEqual({ expected_policy_revision: 10 });
    expect(calls.slice(1).map(call => call.body.capability)).toEqual(["health", "latency_ms", "text", "streaming"]);
    expect(calls.slice(1).every(call => call.url.includes("/api/settings/model-fabric/canary"))).toBe(true);
    expect(props.onSettings).toHaveBeenCalled();
    // The tick resets after the save.
    expect(screen.getByRole("checkbox", { name: CONSENT })).not.toBeChecked();
  });

  it("never runs more paid checks than the cost line showed", async () => {
    const fetch = vi.fn(async (input: RequestInfo | URL) => String(input).includes("catalog-check")
      ? json(afterCatalogCheck({ reason_code: "proof_missing:health", chat_capabilities: { streaming: "proof_missing:streaming", tool_use: "proof_missing:tool_use" },
        structured_output_capability: "proof_missing:structured_output" }))
      : json({ outcome: "passed", error_code: null }));
    vi.stubGlobal("fetch", fetch);
    // Needs a test with a current receipt: exactly one check is missing and shown.
    const settings = settingsOf(raw({ slot: rawSlot({ reason_code: null, chat_capabilities: { streaming: "proof_missing:streaming", tool_use: "ready" } }) }));
    const { props } = renderCard(settings, READY);
    expect(statusLine()).toHaveTextContent("One quick test left.");
    expect(screen.getByTestId("model-card-cost")).toHaveTextContent("It sends 1 tiny paid request, at most $0.02 in total.");
    fireEvent.click(screen.getByRole("button", { name: "Run test" }));
    await waitFor(() => expect(props.onRefresh).toHaveBeenCalled());
    // The catalog check found six missing; none ran because only one was shown.
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("model-card-failure")).toHaveTextContent("6 paid checks are needed; 1 were shown.");
  });

  it("a failing structured output check does not stop chat; a failing chat check stops the rest", async () => {
    const order: string[] = [];
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes("catalog-check")) return json(afterCatalogCheck({ reason_code: "proof_missing:health",
        chat_capabilities: { streaming: "proof_missing:streaming", tool_use: "proof_missing:tool_use" }, structured_output_capability: "proof_missing:structured_output" }));
      const capability = JSON.parse(String(init?.body)).capability as string;
      order.push(capability);
      return json(capability === "structured_output" ? { outcome: "failed", error_code: "near_tee_structured_output_invalid" } : { outcome: "passed", error_code: null });
    });
    vi.stubGlobal("fetch", fetch);
    const { props } = renderCard(settingsOf(raw({ slot: rawSlot({ catalog_receipt_current: false, chat_capabilities: {} }) })), READY);
    expect(screen.getByTestId("model-card-cost")).toHaveTextContent("checks NEAR's catalog for free");
    fireEvent.click(screen.getByRole("button", { name: "Run test" }));
    await waitFor(() => expect(props.onRefresh).toHaveBeenCalled());
    expect(order).toEqual(["health", "latency_ms", "text", "streaming", "tool_use", "structured_output"]);
    expect(screen.queryByTestId("model-card-failure")).toBeNull();
    expect(card()).toHaveTextContent("structured_output: failed (near_tee_structured_output_invalid)");
  });

  it("shows NEAR down after an unanswered catalog check and keeps settings unchanged", async () => {
    const fetch = vi.fn(async () => json({ detail: "upstream" }, 502)); vi.stubGlobal("fetch", fetch);
    const onSave = vi.fn();
    const { props } = renderCard(settingsOf(raw({ slot: rawSlot({ catalog_receipt_current: false, chat_capabilities: {} }) })), READY, { onSave });
    fireEvent.click(screen.getByRole("button", { name: "Run test" }));
    await waitFor(() => expect(props.onRefresh).toHaveBeenCalled());
    await waitFor(() => expect(statusLine()).toHaveTextContent("NEAR isn't answering right now."));
    expect(card()).toHaveTextContent("Your settings are fine. Nothing was changed.");
    expect(screen.getByRole("button", { name: "Try again" })).toBeEnabled();
    expect(onSave).not.toHaveBeenCalled();
  });

  it("missing key: one key field, saved as a key-only change, then cleared", async () => {
    const onSave = vi.fn(async (_payload: Record<string, unknown>) => settingsOf(raw({ slot: rawSlot({ consent_current: false }) })));
    const { props } = renderCard(settingsOf(raw({ near: { key_present: false } })), BLOCKED("near_tee_credential_required"), { onSave });
    expect(statusLine()).toHaveTextContent("Chat is off. Seraph needs a NEAR API key.");
    expect(card()).toHaveTextContent("Paste your NEAR key. It is stored locally and never shown again.");
    fireEvent.change(screen.getByLabelText("NEAR API key"), { target: { value: "synthetic-near-key" } });
    fireEvent.click(screen.getByRole("button", { name: "Save key" }));
    await waitFor(() => expect(props.onRefresh).toHaveBeenCalled());
    expect(onSave.mock.calls[0][0]).toEqual({ expected_policy_revision: 8, near_tee: { schema_version: "seraph.near.tee.setup.v1",
      api_base: "https://cloud-api.near.ai/v1", slots: {}, api_key: "synthetic-near-key" } });
    expect(window.localStorage.length).toBe(0);
  });

  it("only the missing-key state shows a key field", () => {
    renderCard(settingsOf(revokedRaw()), BLOCKED("provider_policy_revoked"));
    expect(screen.queryByLabelText("NEAR API key")).toBeNull();
  });

  it("not set up: model picker and tick, and turning on sends the chosen model with the tick", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(afterCatalogCheck())));
    const onSave = vi.fn(async (_payload: Record<string, unknown>) => settingsOf(raw({ top: { egress_revision: 10 } })));
    const { props } = renderCard(settingsOf(raw({ slot: null })), BLOCKED("near_tee_slot_unconfigured"), { onSave });
    expect(statusLine()).toHaveTextContent("Chat is off. Pick a model to turn it on.");
    const turnOn = screen.getByRole("button", { name: "Turn on chat" });
    fireEvent.change(screen.getByLabelText("Chat model"), { target: { value: "Qwen/Qwen3.8-27B" } });
    expect(turnOn).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox", { name: CONSENT }));
    fireEvent.click(turnOn);
    await waitFor(() => expect(props.onRefresh).toHaveBeenCalled());
    expect(onSave.mock.calls[0][0]).toMatchObject({ near_tee: { slots: { text: { model_id: "Qwen/Qwen3.8-27B", enabled: true,
      plaintext_provider_egress_acknowledged: true } } } });
  });

  it("no budget, budget used up and a request that cost too much each save only the lane", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(afterCatalogCheck())));
    const onSave = vi.fn(async (_payload: Record<string, unknown>) => settingsOf(raw()));
    const first = renderCard(settingsOf(raw({ lane: null })), BLOCKED("inference_lane_required"), { onSave });
    expect(statusLine()).toHaveTextContent("Chat is off. Set a monthly budget first.");
    fireEvent.change(screen.getByLabelText("Monthly budget in dollars"), { target: { value: "25" } });
    fireEvent.click(screen.getByRole("button", { name: "Save budget" }));
    await waitFor(() => expect(first.props.onRefresh).toHaveBeenCalled());
    expect(onSave.mock.calls[0][0]).toMatchObject({ expected_policy_revision: 8, inference_lane: { spend_ceiling_microusd: 25_000_000, max_inflight: 1 } });
    expect(onSave.mock.calls[0][0]).not.toHaveProperty("near_tee");
    first.unmount();

    const used = renderCard(settingsOf(raw({ lane: { spend_ceiling_microusd: 1_000_000 }, accounting: { ceiling_microusd: 1_000_000, remaining_microusd: 0 } })), READY, { onSave });
    expect(statusLine()).toHaveTextContent("Chat is paused. This month's budget is used up.");
    expect(card()).toHaveTextContent("$1 of $1 spent.");
    fireEvent.change(screen.getByLabelText("New monthly budget in dollars"), { target: { value: "0.5" } });
    fireEvent.click(screen.getByRole("button", { name: "Raise budget" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Enter more than $1.");
    used.unmount();

    const overrun = renderCard(settingsOf(raw({ accounting: { overrun_max_cost_microusd: 30_000,
      unreviewed_overruns: [{ operation_id: "op", job_id: "job", bound_microusd: 20_000, actual_cost_microusd: 30_000 }] } })), READY, { onSave });
    expect(statusLine()).toHaveTextContent("Chat is paused. One request cost more than its limit.");
    expect(card()).toHaveTextContent("It cost $0.03. The limit was $0.02.");
    fireEvent.click(screen.getByRole("button", { name: "Accept and continue" }));
    await waitFor(() => expect(overrun.props.onRefresh).toHaveBeenCalled());
    expect(onSave.mock.calls[onSave.mock.calls.length - 1][0]).toMatchObject({ inference_lane: { spend_ceiling_microusd: 1_000_000_000, request_reserve_review_microusd: 30_000 } });
  });

  it("ready shows the plain line; change model asks for the OK again", () => {
    renderCard(settingsOf(raw()), READY);
    expect(statusLine()).toHaveTextContent("Chat is on.");
    expect(card()).toHaveTextContent("DeepSeek V4.1 Flash, hosted by NEAR. $0.01 of $1,000 used this month.");
    fireEvent.click(screen.getByRole("button", { name: "Change model" }));
    expect(screen.queryByRole("checkbox", { name: CONSENT })).toBeNull();
    fireEvent.change(screen.getByLabelText("Chat model"), { target: { value: "Qwen/Qwen3.8-27B" } });
    expect(screen.getByRole("checkbox", { name: CONSENT })).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Allow and switch model" })).toBeDisabled();
  });

  it("settings unreadable opens Diagnostics and sends nothing", () => {
    const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
    const { props } = renderCard(settingsOf(raw({ top: { configuration_status: "degraded" } })), READY);
    expect(statusLine()).toHaveTextContent("Seraph can't read its model settings.");
    expect(card()).toHaveTextContent("Nothing was sent to NEAR.");
    fireEvent.click(screen.getByRole("button", { name: "Open Diagnostics" }));
    expect(props.onOpenDiagnostics).toHaveBeenCalled();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("keeps technical detail under a closed Details fold", () => {
    renderCard(settingsOf(revokedRaw()), BLOCKED("provider_policy_revoked", "near_tee_catalog_receipt_stale"));
    const details = card().querySelector("details");
    expect(details).not.toBeNull();
    expect(details?.open).toBe(false);
    expect(within(details as HTMLElement).getByText(/provider_policy_revoked, near_tee_catalog_receipt_stale/)).toBeInTheDocument();
    expect(within(details as HTMLElement).getByRole("button", { name: "Turn off chat" })).toBeInTheDocument();
  });

  it("stale settings disable every action", () => {
    renderCard(settingsOf(revokedRaw()), BLOCKED("provider_policy_revoked"), { stale: true });
    expect(screen.getByRole("checkbox", { name: CONSENT })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Allow and turn on chat" })).toBeDisabled();
  });

  it("new month: one explicit click posts the existing period review and nothing else", async () => {
    const calls: Array<{ url: string; body: unknown }> = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), body: JSON.parse(String(init?.body ?? "{}")) });
      return json({ status: "ready" });
    }));
    const onSave = vi.fn();
    const settings = settingsOf(raw({ accounting: { status: "blocked", reason_code: "accounting_period_review_required", revision: 483, period_id: "2026-12",
      period_review: { endpoint: "/api/settings/model-fabric/accounting/period", method: "POST", period_id: "2026-12", expected_revision: 483 } },
      slot: rawSlot({ status: "blocked", reason_code: "accounting_period_review_required" }) }));
    const { props } = renderCard(settings, BLOCKED("accounting_period_review_required"), { onSave });
    expect(statusLine()).toHaveTextContent("Chat is paused. A new month started. Confirm last month's spending to continue.");
    expect(calls).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "Confirm and continue" }));
    expect(statusLine()).toHaveTextContent("Confirming the month…");
    await waitFor(() => expect(props.onRefresh).toHaveBeenCalled());
    expect(calls).toEqual([{ url: expect.stringContaining("/api/settings/model-fabric/accounting/period"), body: { period_id: "2026-12", expected_revision: 483 } }]);
    expect(onSave).not.toHaveBeenCalled();
  });

  it("a refused optional structured output check does not mark NEAR down", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes("catalog-check")) return json(afterCatalogCheck({ chat_capabilities: { streaming: "proof_missing:streaming", tool_use: "ready" },
        structured_output_capability: "proof_missing:structured_output" }));
      return JSON.parse(String(init?.body)).capability === "structured_output" ? json({ detail: "upstream" }, 502) : json({ outcome: "passed", error_code: null });
    }));
    const { props } = renderCard(settingsOf(raw({ slot: rawSlot({ catalog_receipt_current: false, chat_capabilities: {} }) })), READY);
    fireEvent.click(screen.getByRole("button", { name: "Run test" }));
    expect(statusLine()).toHaveTextContent("Running test…");
    await waitFor(() => expect(props.onRefresh).toHaveBeenCalled());
    await waitFor(() => expect(statusLine()).not.toHaveTextContent("Running test…"));
    expect(statusLine()).not.toHaveTextContent("NEAR isn't answering");
    expect(card()).toHaveTextContent("structured_output: refused (upstream)");
  });

  it("NEAR down after a saved OK says the change was saved", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ detail: "upstream" }, 503)));
    const onSave = vi.fn(async (_payload: Record<string, unknown>) => settingsOf(raw({ top: { egress_revision: 10 } })));
    let current = settingsOf(revokedRaw());
    const view = renderCard(current, BLOCKED("provider_policy_revoked"), { onSave });
    // After the refresh the backend reports the OK as current; the catalog check still failed.
    (view.props.onRefresh as ReturnType<typeof vi.fn>).mockImplementation(async () => { current = settingsOf(raw({ top: { egress_revision: 10 }, slot: rawSlot({ catalog_receipt_current: false, chat_capabilities: {} }) })); });
    fireEvent.click(screen.getByRole("checkbox", { name: CONSENT }));
    fireEvent.click(screen.getByRole("button", { name: "Allow and turn on chat" }));
    await waitFor(() => expect(view.props.onRefresh).toHaveBeenCalled());
    view.rerender(<ModelCard {...view.props} settings={current} route={READY} />);
    await waitFor(() => expect(statusLine()).toHaveTextContent("NEAR isn't answering right now."));
    expect(card()).toHaveTextContent("Your change was saved, but NEAR didn't answer the check that follows it.");
    expect(card()).not.toHaveTextContent("Nothing was changed.");
  });

  it("can't check the route: a neutral line and Try again re-reads", async () => {
    const { props } = renderCard(settingsOf(raw()), null);
    expect(statusLine()).toHaveTextContent("Can't check chat status right now.");
    expect(card()).not.toHaveTextContent("Chat is blocked.");
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(props.onRefresh).toHaveBeenCalled());
  });

  it("says plainly when the budget is already at the maximum, and when it is below one request's limit", () => {
    const max = renderCard(settingsOf(raw({ accounting: { remaining_microusd: 0 } })), READY);
    expect(card()).toHaveTextContent("$1,000 a month is the most Seraph allows, so chat resumes next month.");
    expect(screen.queryByRole("button", { name: "Raise budget" })).toBeNull();
    max.unmount();
    renderCard(settingsOf(raw({ lane: { spend_ceiling_microusd: 10_000 } })), BLOCKED("near_tee_request_reserve_exceeds_lane_ceiling"));
    expect(statusLine()).toHaveTextContent("Chat is off. The monthly budget is smaller than one request's limit ($0.02).");
  });

  it("switching model sends the stored slot with the new model and a fresh tick", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(afterCatalogCheck())));
    const onSave = vi.fn(async (_payload: Record<string, unknown>) => settingsOf(raw({ top: { egress_revision: 10 } })));
    const { props } = renderCard(settingsOf(raw()), READY, { onSave });
    fireEvent.click(screen.getByRole("button", { name: "Change model" }));
    fireEvent.change(screen.getByLabelText("Chat model"), { target: { value: "Qwen/Qwen3.8-27B" } });
    fireEvent.click(screen.getByRole("checkbox", { name: CONSENT }));
    fireEvent.click(screen.getByRole("button", { name: "Allow and switch model" }));
    expect(statusLine()).toHaveTextContent("Switching model…");
    await waitFor(() => expect(props.onRefresh).toHaveBeenCalled());
    expect(onSave.mock.calls[0][0]).toEqual({ expected_policy_revision: 8, near_tee: { schema_version: "seraph.near.tee.setup.v1",
      api_base: "https://cloud-api.near.ai/v1", slots: { text: { model_id: "Qwen/Qwen3.8-27B", enabled: true, max_input_tokens: 8192,
        max_output_tokens: 4096, timeout_seconds: 30, request_cost_bound_microusd: 20_000, catalog_ttl_hours: 24,
        plaintext_provider_egress_acknowledged: true } } } });
  });

  it("turn off chat sends the stored slot disabled, with no acknowledgement and no key", async () => {
    const onSave = vi.fn(async (_payload: Record<string, unknown>) => settingsOf(raw({ slot: rawSlot({ enabled: false }) })));
    const { props } = renderCard(settingsOf(raw()), READY, { onSave });
    fireEvent.click(screen.getByRole("button", { name: "Turn off chat" }));
    expect(statusLine()).toHaveTextContent("Turning chat off…");
    await waitFor(() => expect(props.onRefresh).toHaveBeenCalled());
    expect(onSave.mock.calls[0][0]).toEqual({ expected_policy_revision: 8, near_tee: { schema_version: "seraph.near.tee.setup.v1",
      api_base: "https://cloud-api.near.ai/v1", slots: { text: { model_id: MODEL, enabled: false, max_input_tokens: 8192,
        max_output_tokens: 4096, timeout_seconds: 30, request_cost_bound_microusd: 20_000, catalog_ttl_hours: 24,
        plaintext_provider_egress_acknowledged: false } } } });
  });
});
