import { act, fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { SettingsPanel } from "./SettingsPanel";
import { appEventBus } from "../lib/appEventBus";
import { useChatStore } from "../stores/chatStore";

const auth = vi.hoisted(() => ({ session: { principal_id: "owner", session_id: "root", idle_expires_at: "2099-01-01T00:00:00Z", absolute_expires_at: "2099-01-02T00:00:00Z" } }));
vi.mock("./auth/OperatorAuthGate", () => ({ useOptionalOperatorAuth: () => auth }));
vi.mock("./settings/ArtifactStoragePanel", () => ({ ArtifactStoragePanel: () => <div aria-label="Inference accounting">Owning accounting inspector</div> }));
beforeEach(() => {
  useChatStore.setState({ settingsPanelOpen: false });
  auth.session = { principal_id: "owner", session_id: "root", idle_expires_at: "2099-01-01T00:00:00Z", absolute_expires_at: "2099-01-02T00:00:00Z" };
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) }));
});
afterEach(() => vi.unstubAllGlobals());

it("opens the existing accounting section from a closed mount and resets another open section", async () => {
  render(<SettingsPanel />);
  act(() => appEventBus.emit("settings:inspect-accounting", { principalId: "owner", sessionId: "root" }));
  expect(await screen.findByLabelText("Inference accounting")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "General" }));
  expect(screen.queryByLabelText("Inference accounting")).not.toBeInTheDocument();
  act(() => appEventBus.emit("settings:inspect-accounting", { principalId: "owner", sessionId: "root" }));
  expect(screen.getByLabelText("Inference accounting")).toBeInTheDocument();
});

it.each(["wrong-owner", "stale-root", "expired", "invalid-expiry"])("ignores %s inspection without opening Settings", (condition) => {
  if (condition === "expired") auth.session.idle_expires_at = "2000-01-01T00:00:00Z";
  if (condition === "invalid-expiry") auth.session.absolute_expires_at = "invalid";
  render(<SettingsPanel />);
  act(() => appEventBus.emit("settings:inspect-accounting", { principalId: condition === "wrong-owner" ? "other" : "owner", sessionId: condition === "stale-root" ? "old-root" : "root" }));
  expect(useChatStore.getState().settingsPanelOpen).toBe(false);
});
