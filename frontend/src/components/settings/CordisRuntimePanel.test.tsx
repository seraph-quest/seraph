import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { apiFetch } from "../../lib/api";
import { normalizeChatRuntime, normalizeCordisRuntime } from "../../lib/cordisRuntime";
import { CordisRuntimePanel } from "./CordisRuntimePanel";
vi.mock("../../lib/api", () => ({ apiFetch: vi.fn() }));
const snapshot = { state: "blocked", reason: "node_unsupported", profile_id: "core.cpu", cordis_version: "4.0.0-rc.10", node_version: "22.11.0", composition_digest: null, package_digest: null, plugins: [{ id: "authority", state: "blocked", reason: "host_unavailable" }], cleanup: { state: "not_started", process_reaped: false, resources_remaining: null, cordis_disposal: "not_started" } };
const response = (v: unknown, status = 200) => new Response(JSON.stringify(v), { status });
beforeEach(() => { vi.mocked(apiFetch).mockReset(); });
it.each(["node_unsupported", "node_missing", "runtime_build_stale", "runtime_package_invalid_or_missing"])("shows actionable host block %s without inferring agent migration", async reason => {
  vi.mocked(apiFetch).mockResolvedValue(response({ cordis_runtime: { ...snapshot, reason } }));
  render(<CordisRuntimePanel />);
  expect(await screen.findByText(`blocked · ${reason}`)).toBeInTheDocument();
  expect(screen.getByText(/Restore the reviewed host build/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /activate|restart/i })).toBeNull();
});
it("names the exact operator fix the backend reports for a blocked host", async () => {
  const recovery = "Install Node 22.12+ or 24.x and set CORDIS_NODE_PATH to it (or put it first on PATH), then run ./manage.sh -e dev cordis build and restart with ./manage.sh -e dev local up.";
  vi.mocked(apiFetch).mockResolvedValue(response({ cordis_runtime: { ...snapshot, recovery } }));
  render(<CordisRuntimePanel />);
  expect(await screen.findByText(`Fix: ${recovery}`)).toBeInTheDocument();
});
it("preserves last confirmed state through failed metadata refresh", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response({ cordis_runtime: snapshot })).mockResolvedValueOnce(response({}, 503));
  render(<CordisRuntimePanel />); await screen.findByText("blocked · node_unsupported");
  fireEvent.click(screen.getByRole("button", { name: "Refresh Cordis host" }));
  expect(await screen.findByText(/Host readiness unknown · stale metadata/)).toBeInTheDocument();
  expect(screen.getByText(/Artifact settings remain usable/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Refresh Cordis host" })).toBeEnabled();
});
it("shows current verified readiness, then removes every active ready label after refresh failure", async () => {
  const ready = { ...snapshot, state: "ready", reason: null, readiness: { state: "verified", checked_at: 1700000000000 }, plugins: [{ id: "authority", state: "ready", reason: null }] };
  vi.mocked(apiFetch).mockResolvedValueOnce(response({ cordis_runtime: ready })).mockRejectedValueOnce(Error("timeout"));
  render(<CordisRuntimePanel />);
  await screen.findByText("ready");
  fireEvent.click(screen.getByRole("button", { name: "Refresh Cordis host" }));
  await screen.findByText("Host readiness unknown · stale metadata");
  expect(screen.queryByText("ready")).toBeNull();
  expect(screen.getByText("authority: unknown · stale metadata")).toBeInTheDocument();
  expect(screen.getByText(/Readiness: unknown · last verified: 2023-11-14/)).toBeInTheDocument();
});
it("treats legacy cached ready snapshots as unknown instead of current readiness", () => {
  const parsed = normalizeCordisRuntime({ ...snapshot, state: "ready", reason: null, plugins: [{ id: "authority", state: "ready", reason: null }] });
  expect(parsed?.state).toBe("blocked"); expect(parsed?.readiness.state).toBe("unknown");
  expect(parsed?.plugins[0].state).toBe("blocked");
});
it("strips unexpected sensitive fields from its retained public projection", async () => {
  const unsafe = { ...snapshot, api_key: "sensitive-token", pid: 123, nonce: "sensitive-nonce", environment: { SECRET: "value" }, plugins: [{ ...snapshot.plugins[0], stderr: "sensitive-stderr" }] };
  const parsed = normalizeCordisRuntime(unsafe);
  expect(JSON.stringify(parsed)).not.toMatch(/sensitive|environment|stderr|nonce|api_key|pid/);
  vi.mocked(apiFetch).mockResolvedValue(response({ cordis_runtime: unsafe }));
  render(<CordisRuntimePanel />); await screen.findByText("blocked · node_unsupported");
  expect(document.body.textContent).not.toMatch(/sensitive/);
});
it("reports missing host metadata as unavailable rather than ready", async () => {
  vi.mocked(apiFetch).mockResolvedValue(response({ model_fabric: {} }));
  render(<CordisRuntimePanel />);
  await waitFor(() => expect(screen.getByText("Host state unavailable")).toBeInTheDocument());
  expect(screen.queryByText("ready")).toBeNull();
});
it("names the actual chat agent loop, route and owner epoch from the server's owner row", async () => {
  const chat_runtime = { loop: "cordis.agent-loop.v1", route: "chat_agent", owner_epoch: 2, reason: null, recovery: null };
  vi.mocked(apiFetch).mockResolvedValue(response({ cordis_runtime: snapshot, chat_runtime }));
  render(<CordisRuntimePanel />);
  expect(await screen.findByText("Chat: Cordis agent loop (cordis.agent-loop.v1) · route chat_agent · owner epoch 2")).toBeInTheDocument();
  expect(screen.queryByText(/Chat fix:/)).toBeNull();
});
it("shows a blocked chat loop with its exact fix and never a legacy fallback", async () => {
  const recovery = "Run ./manage.sh -e dev cordis build, then restart with ./manage.sh -e dev local up.";
  const chat_runtime = { loop: "blocked", route: "chat_agent", owner_epoch: 2, reason: "cordis_runtime_build_stale", recovery };
  vi.mocked(apiFetch).mockResolvedValue(response({ cordis_runtime: snapshot, chat_runtime }));
  render(<CordisRuntimePanel />);
  expect(await screen.findByText("Chat blocked · cordis_runtime_build_stale")).toBeInTheDocument();
  expect(screen.getByText(`Chat fix: ${recovery}`)).toBeInTheDocument();
  expect(screen.queryByText(/legacy agent loop/)).toBeNull();
});
it("drops a malformed chat runtime status instead of guessing a loop", () => {
  expect(normalizeChatRuntime({ loop: "blocked", route: "chat_agent", owner_epoch: 2, reason: null, recovery: null })).toBeNull();
  expect(normalizeChatRuntime({ loop: "smolagents", route: "chat_agent", owner_epoch: 1, reason: null, recovery: null })).toBeNull();
  expect(normalizeChatRuntime({ loop: "legacy.smolagents", route: "chat_agent", owner_epoch: null, reason: null, recovery: null, secret: "x" }))
    .toEqual({ loop: "legacy.smolagents", route: "chat_agent", owner_epoch: null, reason: null, recovery: null });
});
