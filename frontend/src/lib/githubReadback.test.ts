import { describe, expect, it } from "vitest";
import { githubReadbackRequest } from "./githubReadback";

describe("legacy GitHub exact readback request", () => {
  it("requires fresh explicit acknowledgment and canonical revision", () => {
    expect(() => githubReadbackRequest(false, 3, 7)).toThrow();
    for (const revision of [0, -1, "3", NaN, Infinity]) expect(() => githubReadbackRequest(true, revision, 7)).toThrow();
    expect(githubReadbackRequest(true, 3, 7)).toEqual({ acknowledged_readback: true, expected_connection_revision: 3, remote_id: 7 });
    expect(githubReadbackRequest(true, 4)).toEqual({ acknowledged_readback: true, expected_connection_revision: 4 });
  });
});
