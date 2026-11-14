// @vitest-environment node
import { describe, expect, it } from "vitest";

import config from "./vite.config";

// The backend trusts X-Forwarded-For only from loopback. The dev proxy must
// forward the real client so a LAN client reaching an exposed dev server is
// not mistaken for a loopback operator in local no-login mode (#1161).
describe("dev server proxy", () => {
  it("forwards the client address on the API and WebSocket proxies", () => {
    const proxy = config.server?.proxy as Record<string, { xfwd?: boolean; ws?: boolean }>;
    expect(proxy["/api"].xfwd).toBe(true);
    expect(proxy["/ws"].xfwd).toBe(true);
    expect(proxy["/ws"].ws).toBe(true);
  });
});
