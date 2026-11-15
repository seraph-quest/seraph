/// <reference types="vitest" />
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

declare const process: { env: Record<string, string | undefined> };

const apiProxyTarget = process.env.VITE_API_PROXY_TARGET ?? "http://127.0.0.1:8004";
const websocketProxyTarget = apiProxyTarget.replace(/^http/, "ws");
// The dev server serves the cockpit and can read local files over its
// WebSocket, so it binds loopback unless a caller opts into a wider bind
// explicitly (docker-dev sets SERAPH_DEV_SERVER_HOST=0.0.0.0 inside its
// container). The name avoids the VITE_ prefix so it never reaches client code.
const devServerHost = process.env.SERAPH_DEV_SERVER_HOST?.trim() || "127.0.0.1";

export default defineConfig({
  plugins: [react()],
  server: {
    host: devServerHost,
    port: 5173,
    proxy: {
      // xfwd forwards the real client address in X-Forwarded-For. The backend
      // trusts proxy headers only from loopback, so a LAN client reaching an
      // exposed dev server is never mistaken for a loopback operator (local
      // no-login mode refuses any non-loopback forwarded hop; #1161).
      "/api": {
        target: apiProxyTarget,
        changeOrigin: true,
        xfwd: true,
      },
      "/ws": {
        target: websocketProxyTarget,
        ws: true,
        xfwd: true,
      },
    },
    watch: {
      usePolling: true,
    },
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    testTimeout: 20000,
  },
});
