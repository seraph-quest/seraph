import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { RuntimeOwnerBanner } from "./RuntimeOwnerBanner";

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
