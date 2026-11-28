import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { RuntimeOwnerBanner } from "./RuntimeOwnerBanner";
import { normalizeLifecycleRuntime } from "../../lib/cordisRuntime";

it("shows a blocked guardian loop and non-ready domains with their fixes", () => {
  render(<RuntimeOwnerBanner guardian={{ loop: "blocked", owner_epoch: 2, reason: "cordis_host_unavailable", recovery: "Start the Cordis runtime." }}
    domains={[{ domain: "seraph.guardian.v1", owner_kind: "cordis", epoch: 2, state: "draining", reason: "owner_draining", recovery: "Finish the owner transition." },
      { domain: "seraph.tasks.v1", owner_kind: "legacy", epoch: 1, state: "ready", reason: null, recovery: null }]} />);
  const banner = screen.getByRole("status", { name: "Runtime owners needing action" });
  expect(banner).toHaveTextContent("Scheduled guardian work blocked · cordis_host_unavailable · fix: Start the Cordis runtime.");
  expect(banner).toHaveTextContent("seraph.guardian.v1: draining · owner_draining · fix: Finish the owner transition.");
  expect(banner).not.toHaveTextContent("seraph.tasks.v1");
});

it("renders nothing when every owner is ready", () => {
  const { container } = render(<RuntimeOwnerBanner guardian={{ loop: "cordis.guardian.v1", owner_epoch: 2, reason: null, recovery: null }}
    domains={[{ domain: "seraph.tasks.v1", owner_kind: "legacy", epoch: 1, state: "ready", reason: null, recovery: null }]} />);
  expect(container).toBeEmptyDOMElement();
});

const lifecycleRow = (overrides: Record<string, unknown>) => normalizeLifecycleRuntime({
  domain: "seraph.scheduler.v1", state: "active", reason: null, recovery: null, owner: "legacy", owner_epoch: 1,
  blocked_jobs: [], ...overrides });

it("shows a scheduler or dispatch lifecycle that is not active, never a disabled scheduler (#1025)", () => {
  const blocked = lifecycleRow({ domain: "seraph.dispatch.v1", state: "blocked", reason: "cordis_host_unavailable",
    recovery: "Start the Cordis runtime.", owner: "cordis", owner_epoch: 3 });
  const disabled = lifecycleRow({ state: "disabled", reason: "scheduler_disabled", recovery: "Enable it." });
  expect(blocked).not.toBeNull(); expect(disabled).not.toBeNull();
  render(<RuntimeOwnerBanner guardian={null} domains={[]} lifecycle={[blocked!, disabled!]} />);
  const banner = screen.getByRole("status", { name: "Runtime owners needing action" });
  expect(banner).toHaveTextContent("Work Board dispatch: blocked · cordis_host_unavailable · cordis owner epoch 3 · fix: Start the Cordis runtime.");
  expect(banner).not.toHaveTextContent("Scheduled work");
});

it("renders nothing for active or disabled lifecycle domains and rejects an unknown state", () => {
  const { container } = render(<RuntimeOwnerBanner guardian={null} domains={[]}
    lifecycle={[lifecycleRow({})!, lifecycleRow({ domain: "seraph.dispatch.v1", state: "disabled", reason: "scheduler_disabled" })!]} />);
  expect(container).toBeEmptyDOMElement();
  expect(lifecycleRow({ state: "running" })).toBeNull();
  expect(lifecycleRow({ domain: "seraph.tasks.v1" })).toBeNull();
});
