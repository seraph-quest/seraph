import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { normalizeModelFabricSettings } from "../../lib/modelFabric";
import type { ModelFabricSettingsStatus } from "../../lib/modelFabric";
import { appEventBus } from "../../lib/appEventBus";
import { SETTINGS_OPEN_SECTION_EVENT } from "../../lib/settingsNavigation";
import { MemorySearchRow, ModelTab, ScreenshotAnalysisRow, VISION_CHECKS, memoryRowState, screenshotRowState } from "./ModelTab";
import type { ArtifactSettingsSource } from "./settingsData";

// #1280 PR 3: the Model tab's two rows under the Model card. Synthetic
// settings only; NEAR is never contacted.

const TEXT_OK = "I understand NEAR sees these requests as plain text: no end-to-end encryption, and Seraph does not verify NEAR's secure hardware.";
const SCREENSHOT_OK = "I agree to send my screenshots to NEAR for analysis. This is separate from the chat OK.";
const VISION_MODEL = "Qwen/Qwen3-VL-30B-A3B-Instruct";

type Raw = Record<string, unknown>;
function rawSlot(overrides: Raw = {}): Raw {
  return {
    model_id: VISION_MODEL, enabled: true, max_input_tokens: 16384, max_output_tokens: 2048, timeout_seconds: 30,
    temperature: null, catalog_ttl_hours: 24, consent_current: true, status: "ready", reason_code: null,
    catalog_receipt: { model_id: VISION_MODEL, outcome: "passed", checked_at_iso: "2026-11-18T10:00:00+00:00", reason_code: null, required_cost_microusd: 20_000 },
    catalog_receipt_current: true, tee_hosted_per_catalog: true, tee_hosting_label: "TEE-hosted per NEAR catalog",
    capability_check_timeout_seconds: 46, ...overrides,
  };
}
function raw({ vision = rawSlot(), embedding = null, index = null, near = {}, top = {} }: { vision?: Raw | null; embedding?: Raw | null; index?: Raw | null; near?: Raw; top?: Raw } = {}): Raw {
  return {
    schema_version: "seraph.model-fabric.settings.v1", status: "ready", error_code: null, configuration_status: "ready",
    egress_revision: 8, egress_revoked: false,
    near_tee: { configured: true, status: "ready", reason_code: null, key_present: true, credential_fingerprint: "553c52e01ac7",
      tee_verified: false, e2ee: false, slots: { text: null, vision, embedding }, embedding_index: index, ...near },
    ...top,
  };
}
const settingsOf = (value: Raw) => normalizeModelFabricSettings(value) as ModelFabricSettingsStatus;
const ON = { analysis_enabled: true, provider: "near_tee" };
const OFF = { analysis_enabled: false, provider: "" };

afterEach(() => { vi.unstubAllGlobals(); window.localStorage.clear(); });

function renderRow(settings: ModelFabricSettingsStatus | null, screen: typeof ON | null = ON, source: ArtifactSettingsSource = "full", extra: Raw = {}) {
  const props = {
    screen, source, settings, stale: false, savingScreen: false,
    onSave: vi.fn(async (_payload: Record<string, unknown>) => settingsOf(raw({ top: { egress_revision: 10 } }))),
    onSettings: vi.fn(), onScreenPatch: vi.fn(async (_patch: Record<string, unknown>) => true), onRefresh: vi.fn(async () => {}),
    ...extra,
  };
  return { ...render(<ScreenshotAnalysisRow {...props} />), props };
}
const row = (name: string) => screen.getByRole("region", { name });
const rowStatus = (name: string) => within(row(name)).getAllByRole("status")[0];

describe("screenshotRowState", () => {
  it.each([
    ["loading", null, ON, "full"],
    ["loading", raw(), ON, "fallback"],
    ["unreadable", raw({ top: { configuration_status: "degraded" } }), ON, "full"],
    ["off", raw(), OFF, "full"],
    ["off", raw(), { analysis_enabled: true, provider: "openrouter" }, "screen"],
    ["missing_key", raw({ near: { key_present: false } }), ON, "full"],
    ["needs_ok", raw({ vision: null }), ON, "full"],
    ["needs_ok", raw({ vision: rawSlot({ consent_current: false }) }), ON, "full"],
    ["needs_ok", raw({ top: { egress_revoked: true } }), ON, "full"],
    ["needs_test", raw({ vision: rawSlot({ catalog_receipt_current: false }) }), ON, "full"],
    ["needs_test", raw({ vision: rawSlot({ status: "blocked", reason_code: "proof_missing:vision" }) }), ON, "full"],
    ["blocked", raw({ vision: rawSlot({ status: "blocked", reason_code: "near_tee_model_not_ready" }) }), ON, "full"],
    ["on", raw(), ON, "full"],
  ] as const)("%s", (expected, value, screenSettings, source) => {
    expect(screenshotRowState(screenSettings, source, value ? settingsOf(value) : null)).toBe(expected);
  });
});

describe("ScreenshotAnalysisRow", () => {
  it("turns on only after both OKs, sends them once, and runs no paid check", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(raw({ top: { egress_revision: 11 } })), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const { props } = renderRow(settingsOf(raw({ vision: null })), OFF);
    expect(rowStatus("Screenshot analysis")).toHaveTextContent("Off.");
    fireEvent.click(screen.getByRole("button", { name: "Turn on" }));
    const allow = screen.getByRole("button", { name: "Allow and turn on" });
    const textBox = screen.getByLabelText(TEXT_OK);
    const shotBox = screen.getByLabelText(SCREENSHOT_OK);
    expect(textBox).not.toBeChecked();
    expect(shotBox).not.toBeChecked();
    expect(allow).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Screenshot model"), { target: { value: VISION_MODEL } });
    fireEvent.click(textBox);
    expect(allow).toBeDisabled();
    fireEvent.click(shotBox);
    expect(allow).toBeEnabled();
    fireEvent.click(allow);

    await waitFor(() => expect(props.onRefresh).toHaveBeenCalled());
    expect(props.onSave).toHaveBeenCalledTimes(1);
    const payload = props.onSave.mock.calls[0][0] as { expected_policy_revision: number; near_tee: Raw };
    expect(payload.expected_policy_revision).toBe(8);
    expect(payload.near_tee).not.toHaveProperty("api_key");
    expect((payload.near_tee.slots as Raw).vision).toMatchObject({
      model_id: VISION_MODEL, enabled: true, plaintext_provider_egress_acknowledged: true, screenshot_plaintext_egress_acknowledged: true,
    });
    expect(Object.keys(payload.near_tee.slots as Raw)).toEqual(["vision"]);
    expect(props.onScreenPatch).toHaveBeenCalledWith({ enabled: true, provider: "near_tee" });
    // Only the free catalog check, bound to the new revision; no capability check.
    const urls = fetchMock.mock.calls.map((call) => String((call as unknown[])[0]));
    expect(urls).toHaveLength(1);
    expect(urls[0]).toContain("/api/settings/model-fabric/near-tee/catalog-check");
    expect(JSON.parse(String(((fetchMock.mock.calls[0] as unknown[])[1] as RequestInit).body))).toEqual({ expected_policy_revision: 10 });
    // The ticks never outlive the save.
    expect(screen.queryByLabelText(TEXT_OK)).not.toBeInTheDocument();
  });

  it("keeps the analysis switch off when the slot save is refused", async () => {
    vi.stubGlobal("fetch", vi.fn());
    const { props } = renderRow(settingsOf(raw({ vision: rawSlot({ consent_current: false }) })), ON, "full", {
      onSave: vi.fn(async () => { throw new Error("Request failed: 409 · egress_revision_conflict"); }),
    });
    expect(rowStatus("Screenshot analysis")).toHaveTextContent("Paused. Screenshot analysis needs your OK.");
    fireEvent.click(screen.getByRole("button", { name: "Turn on" }));
    // A stored model needs no picker.
    expect(screen.queryByLabelText("Screenshot model")).not.toBeInTheDocument();
    fireEvent.click(screen.getByLabelText(TEXT_OK));
    fireEvent.click(screen.getByLabelText(SCREENSHOT_OK));
    fireEvent.click(screen.getByRole("button", { name: "Allow and turn on" }));
    expect(await screen.findByTestId("screenshot-row-failure")).toHaveTextContent("egress_revision_conflict");
    expect(props.onScreenPatch).not.toHaveBeenCalled();
    expect(vi.mocked(fetch)).not.toHaveBeenCalled();
  });

  it("re-enables analysis on a consented slot without asking again", async () => {
    vi.stubGlobal("fetch", vi.fn());
    const { props } = renderRow(settingsOf(raw()), OFF);
    fireEvent.click(screen.getByRole("button", { name: "Turn on" }));
    await waitFor(() => expect(props.onScreenPatch).toHaveBeenCalledWith({ enabled: true, provider: "near_tee" }));
    expect(props.onSave).not.toHaveBeenCalled();
    expect(vi.mocked(fetch)).not.toHaveBeenCalled();
  });

  it("shows the cost of the test before running it, and only on click", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ outcome: "passed", error_code: null }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    renderRow(settingsOf(raw({ vision: rawSlot({ status: "blocked", reason_code: "proof_missing:vision" }) })));
    expect(rowStatus("Screenshot analysis")).toHaveTextContent("One quick test left.");
    expect(screen.getByTestId("screenshot-row-cost")).toHaveTextContent(`It sends ${VISION_CHECKS.length} tiny paid requests to NEAR, about $0.08 in total.`);
    expect(fetchMock).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Run test" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(VISION_CHECKS.length));
    const capabilities = fetchMock.mock.calls.map((call) => JSON.parse(String(((call as unknown[])[1] as RequestInit).body)).capability);
    expect(capabilities).toEqual([...VISION_CHECKS]);
  });

  it("turns analysis off without touching consent", async () => {
    vi.stubGlobal("fetch", vi.fn());
    const { props } = renderRow(settingsOf(raw()));
    expect(rowStatus("Screenshot analysis")).toHaveTextContent("On.");
    fireEvent.click(screen.getByRole("button", { name: "Turn off" }));
    await waitFor(() => expect(props.onScreenPatch).toHaveBeenCalledWith({ enabled: false }));
    expect(props.onSave).not.toHaveBeenCalled();
  });

  it("does not read placeholder screen settings as truth", () => {
    renderRow(settingsOf(raw()), ON, "fallback");
    expect(rowStatus("Screenshot analysis")).toHaveTextContent("Checking screenshot analysis…");
    expect(within(row("Screenshot analysis")).queryByRole("button")).not.toBeInTheDocument();
  });
});

describe("Memory search row", () => {
  const gated = { enabled: false, consent_current: false, status: "blocked", reason_code: "near_tee_embedding_evidence_required", evidence_required: true, model_id: "Qwen/Qwen3-Embedding-0.6B", max_output_tokens: 1, max_batch_size: 16 };

  it.each([
    ["loading", null],
    // A near_tee block the cockpit cannot read (here: it claims TEE verification).
    ["unreadable", raw({ near: { tee_verified: true } })],
    ["no_model", raw()],
    ["no_model", raw({ embedding: rawSlot(gated) })],
    ["not_indexed", raw({ embedding: rawSlot({ model_id: "Qwen/Qwen3-Embedding-0.6B" }), index: { recall_mode: "lexical", index_complete: false, slot_reason_code: null } })],
    ["on", raw({ index: { recall_mode: "semantic", index_complete: true, slot_reason_code: null } })],
  ] as const)("%s", (expected, value) => {
    expect(memoryRowState(value ? settingsOf(value) : null)).toBe(expected);
  });

  it("says plainly that there is no embedding model, with no control that pretends to fix it", () => {
    const opened = vi.fn();
    appEventBus.on(SETTINGS_OPEN_SECTION_EVENT, opened);
    try {
      render(<MemorySearchRow settings={settingsOf(raw({ embedding: rawSlot(gated), index: { recall_mode: "lexical", slot_reason_code: "near_tee_embedding_evidence_required" } }))} />);
      expect(rowStatus("Memory search")).toHaveTextContent("Off. There is no embedding model to use right now.");
      expect(row("Memory search")).toHaveTextContent("Memory search matches words, not meaning.");
      expect(row("Memory search")).toHaveTextContent("There is nothing to turn on yet.");
      const buttons = within(row("Memory search")).getAllByRole("button");
      expect(buttons.map((button) => button.textContent)).toEqual(["Details"]);
      expect(within(row("Memory search")).queryByRole("checkbox")).not.toBeInTheDocument();
      fireEvent.click(buttons[0]);
      expect(opened).toHaveBeenCalledWith({ section: "near-slots" });
    } finally {
      appEventBus.off(SETTINGS_OPEN_SECTION_EVENT, opened);
    }
  });
});

describe("ModelTab", () => {
  it("shows the card and two rows, and none of the raw output that moved to Diagnostics", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/settings/model-fabric")) return new Response(JSON.stringify(raw()), { status: 200 });
      if (url.includes("/api/runtime/status")) return new Response(JSON.stringify({ cordis_runtime: { state: "blocked" }, effective_runtime: { inference_ready: false } }), { status: 200 });
      return new Response("{}", { status: 503 });
    }));
    await act(async () => { render(<ModelTab />); });
    expect(screen.getByRole("region", { name: "Model card" })).toBeInTheDocument();
    expect(row("Screenshot analysis")).toBeInTheDocument();
    expect(row("Memory search")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Budget" })).not.toBeInTheDocument();
    for (const moved of ["Advanced and diagnostics", "Model fabric", "Screenshot Folder", "End-of-day reports", "Cordis runtime host"]) {
      expect(screen.queryByText(moved)).not.toBeInTheDocument();
    }
    expect(screen.queryByTestId("settings-advanced")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("OpenRouter text model ID")).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "NEAR TEE settings" })).not.toBeInTheDocument();
  });
});
