import { describe, expect, it, vi } from "vitest";
import { publicationKey, validatePublication, validatePublicationDiscovery } from "./repoPublication";
import { publicationFixture } from "./repoPublication.fixture";


describe("exact publication receipt", () => {
  it("accepts only a bounded page bound to the original repair and root", () => {
    const page = { repair_job_id: "repair-a", owner_principal_id: "owner-a", owner_session_id: "root-a", limit: 20, jobs: [publicationFixture()], next_offset: 20 };
    expect(validatePublicationDiscovery(page, "owner-a", "root-a", "repair-a").nextOffset).toBe(20);
    expect(() => validatePublicationDiscovery(page, "owner-a", "other-root", "repair-a")).toThrow();
    expect(() => validatePublicationDiscovery({ ...page, jobs: Array(21).fill(publicationFixture()) }, "owner-a", "root-a", "repair-a")).toThrow();
    expect(() => validatePublicationDiscovery({ ...page, next_offset: 2020 }, "owner-a", "root-a", "repair-a")).toThrow();
    expect(validatePublicationDiscovery({ ...page, next_offset: null, scan_limit_reached: true }, "owner-a", "root-a", "repair-a").scanLimitReached).toBe(true);
  });
  it("binds original owner, finite root, repair and all four current permissions", () => {
    const value = publicationFixture();
    expect(validatePublication(value, "owner-a", "root-a", "repair-a").approval_status).toBe("pending");
    for (const [owner, root, repair] of [["other", "root-a", "repair-a"], ["owner-a", "root-b", "repair-a"], ["owner-a", "root-a", "other"]]) expect(() => validatePublication(value, owner, root, repair)).toThrow();
    value.preview.required_permissions = value.preview.required_permissions.slice(1);
    expect(() => validatePublication(value, "owner-a", "root-a", "repair-a")).toThrow();
  });
  it("rejects incomplete tested input or false local isolation", () => {
    const value = publicationFixture();
    value.preview.tested_input.environment.available = false;
    expect(() => validatePublication(value, "owner-a", "root-a", "repair-a")).toThrow();
    value.preview.tested_input.environment.available = true; value.preview.local_posture.isolation_claim = "isolated";
    expect(() => validatePublication(value, "owner-a", "root-a", "repair-a")).toThrow();
  });
  it("creates a valid stable caller key even without randomUUID", () => {
    vi.stubGlobal("crypto", { getRandomValues: (bytes: Uint8Array) => bytes.fill(7) });
    expect(publicationKey()).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    vi.unstubAllGlobals();
  });
});
