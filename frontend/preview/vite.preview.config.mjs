// Offline cockpit preview (#1172). Development aid only; not used by the
// managed app, tests or production builds.
//
//   npx vite --config preview/vite.preview.config.mjs
//
// Serves the cockpit on http://127.0.0.1:3011 with a read-only fixture API.
// It never proxies to a backend: GET /api/* is answered from
// preview/fixtures/*.json (redacted captures), every other method is refused,
// and an injected guard blocks browser requests to any other origin. The chat
// WebSocket is replaced by a local fake that returns a labelled canned reply;
// no model is called.
//
// Fixture provenance: runtime_status, observer_state, observer_continuity,
// auth_session, approvals_pending, settings_*, sessions, tools, user_profile,
// capabilities_source-watches and audit_events are GET captures from a local develop backend (2026-11-14) with
// identifiers, secrets, free text, home paths and LAN addresses redacted.
// operator_continuation (the Home snapshot fixture from src/lib), goals_*,
// and work-board_* are synthetic and labelled as
// preview data. observer_state.data_quality_reason ("unscoped_fallback") was
// added by hand to show the #1172 backend field. Fixtures do not have to
// agree with each other.
import { readFileSync, existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const here = dirname(fileURLToPath(import.meta.url));
const fixturesDir = join(here, "fixtures");
const missing = new Set();

function fixtureName(pathname) {
  return pathname.replace(/^\/api\//, "").replace(/\/+$/, "").replace(/[^A-Za-z0-9_.-]+/g, "_") + ".json";
}

function previewMockApi() {
  return {
    name: "seraph-preview-mock-api",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const url = new URL(req.url ?? "/", "http://preview.local");
        if (!url.pathname.startsWith("/api/")) return next();
        res.setHeader("Content-Type", "application/json");
        if (req.method !== "GET") {
          res.statusCode = 403;
          res.end(JSON.stringify({ detail: "preview_mock_read_only" }));
          return;
        }
        const file = join(fixturesDir, fixtureName(url.pathname));
        if (existsSync(file)) {
          res.statusCode = 200;
          res.end(readFileSync(file));
          return;
        }
        if (!missing.has(url.pathname)) {
          missing.add(url.pathname);
          server.config.logger.warn(`[preview-mock] no fixture for GET ${url.pathname}`);
        }
        res.statusCode = 404;
        res.end(JSON.stringify({ detail: "preview_fixture_missing" }));
      });
    },
    transformIndexHtml(html) {
      // Offline preview: drop the Google Fonts stylesheet so the page makes no
      // third-party request; the cockpit falls back to local fonts.
      return {
        html: html.replace(/<link[^>]*fonts\.googleapis\.com[^>]*>/g, ""),
        tags: [{ tag: "script", injectTo: "head-prepend", children: PREVIEW_GUARD }],
      };
    },
  };
}

const PREVIEW_GUARD = `
(() => {
  const realFetch = window.fetch.bind(window);
  window.fetch = (input, init) => {
    const href = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const target = new URL(href, window.location.href);
    if (target.origin !== window.location.origin) {
      return Promise.reject(new TypeError("preview guard: cross-origin request blocked " + target.origin));
    }
    return realFetch(input, init);
  };
  class PreviewSocket extends EventTarget {
    static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3;
    constructor(url) {
      super();
      this.url = String(url); this.readyState = 0;
      this.onopen = null; this.onmessage = null; this.onclose = null; this.onerror = null;
      setTimeout(() => { this.readyState = 1; this.onopen?.(new Event("open")); }, 120);
    }
    send(raw) {
      let data = {}; try { data = JSON.parse(raw); } catch {}
      const reply = (payload) => setTimeout(() => this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(payload) })), 600);
      if (data.type === "ping") return reply({ type: "pong" });
      if (data.type === "message" || data.type === "resume_message") {
        return reply({ type: "final", session_id: data.session_id ?? "preview-session", content: "[Preview mock] Canned reply. No backend or model was contacted." });
      }
    }
    close() { this.readyState = 3; this.onclose?.({ code: 1000, reason: "" }); }
  }
  window.WebSocket = PreviewSocket;
})();
`;

export default defineConfig({
  root: resolve(here, ".."),
  plugins: [react(), previewMockApi()],
  define: {
    "import.meta.env.VITE_API_URL": JSON.stringify("/api"),
    "import.meta.env.VITE_WS_URL": JSON.stringify("/ws/chat"),
  },
  server: {
    host: "127.0.0.1",
    port: 3011,
    strictPort: true,
    proxy: {},
  },
});
