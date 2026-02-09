---
sidebar_position: 2
---

# OpenClaw vs Seraph — Feature Gap Analysis

> **Date**: 2026-02-09 (updated)
> **OpenClaw version context**: v2026.1.x (145k+ GitHub stars, formerly Clawdbot/Moltbot)
> **Seraph branch**: `develop` (Phase 1 + 2 complete)

## Overview

**OpenClaw** is a self-hosted gateway connecting messaging platforms (WhatsApp, Telegram, Discord, etc.) to AI agents. It's headless — text-in/text-out with no visual UI.

**Seraph** is a self-contained web app with a retro 16-bit RPG village UI. A Phaser 3 canvas renders a tile-based village where an animated pixel-art avatar walks between tool stations while the user chats via an RPG-style dialog box. Persistent identity, long-term memory, hierarchical goals.

Different philosophies, but many of OpenClaw's features are worth adopting.

---

## What Seraph Has (Phase 0-2 Complete)

- Real-time chat with AI agent (WebSocket streaming with step/final/error/proactive/ambient types)
- Tool execution with visual feedback (animated RPG avatar walks to tool stations in village)
- **16 auto-discovered tools + MCP integrations**: web search, file I/O, template fill, soul view/update, goal CRUD, shell execute (snekbox sandbox), browser automation (Playwright), calendar (Google), email (Gmail), task management (Things3 via MCP)
- **Persistent sessions** — SQLite-backed, survive restarts, session list UI with switch/delete
- **Persistent memory** — Soul file (soul.md) + LanceDB vector store with sentence-transformer embeddings
- **Memory consolidation** — Background extraction of facts/preferences/decisions after each conversation
- **Hierarchical goal system** — Vision → Annual → Quarterly → Monthly → Weekly → Daily, with quest log UI
- **Onboarding flow** — Specialized agent for first-time users, skip/restart controls, welcome message
- **Plugin system** — Auto-discovery of tools from `src/tools/`, tool registry with village metadata
- **Sandboxed execution** — snekbox Docker sidecar for shell commands, Playwright for browser
- Phaser 3 village scene with 7 buildings, day/night cycle, idle wandering, 12 waypoints, speech bubbles
- Multi-model support via OpenRouter/LiteLLM
- Docker Compose dev environment (3 services: backend, frontend, sandbox)
- React 19 + Vite 6 + TypeScript + Tailwind + Zustand + Phaser 3 frontend

---

## Feature Gap Analysis

### Tier 1 — Remaining Critical Gaps

| # | Feature | OpenClaw | Seraph Status |
|---|---------|----------|---------------|
| 1 | **Model fallbacks** | Primary + fallback chain, per-agent model override, provider rotation | Single model via OpenRouter, no fallback |
| 2 | **Tool policy system** | Allow/deny lists per agent, profiles (minimal/coding/messaging/full), elevated mode | Onboarding agent has restricted tools, but no general policy system |
| 3 | **Context management** | Context pruning (off/adaptive/aggressive), session compaction/summarization | Unbounded history, no compaction |

### Tier 2 — Major Gaps

| # | Feature | OpenClaw | Seraph Status |
|---|---------|----------|---------------|
| 4 | **Proactive heartbeat** | Cron-like scheduled tasks (check email, RSS, summaries) | WS protocol scaffolding exists (proactive/ambient types), but no scheduler or reasoning engine yet (Phase 3) |
| 5 | **Multi-channel messaging** | WhatsApp, Telegram, Discord, Slack, Signal, iMessage, Mattermost, Google Chat | Web UI only |
| 6 | **Multi-agent routing** | Multiple isolated agents per gateway, deterministic routing | Single agent (+ onboarding agent) |
| 7 | **Subagents** | Spawnable child agents with concurrency limits, agent-level restrictions | None |
| 8 | **Note-taking / Knowledge base** | N/A (not an OpenClaw feature) | Planned in roadmap (Phase 2.4) but not implemented — no Obsidian/markdown vault integration |

### Tier 3 — Important Gaps (UX & operational)

| # | Feature | OpenClaw | Seraph Status |
|---|---------|----------|---------------|
| 9 | **Streaming/chunking** | Block streaming with configurable chunk size, human-like delay | Raw WebSocket step streaming |
| 10 | **Media support** | Send/receive images, audio, documents bidirectionally | Text only |
| 11 | **TTS** | ElevenLabs/OpenAI providers, auto/inbound/tagged modes | None |
| 12 | **Voice transcription** | Inbound voice note transcription hook | None |
| 13 | **Message queuing** | Steer/followup/collect/interrupt modes, debouncing for rapid messages | No queue, one-at-a-time |
| 14 | **Security audit CLI** | `openclaw security audit --deep`, permission hardening, log redaction | None |
| 15 | **User auth/identity** | DM pairing, allowlists, identity links across channels, access groups | Anonymous singleton user, no auth |
| 16 | **Configuration UI** | Web control dashboard, config editing via chat | No settings UI |
| 17 | **Remote access** | SSH, Tailscale, mDNS discovery | Localhost only |
| 18 | **Structured logging** | Redaction, pretty/compact/json styles, per-file output | Basic console logging |

### Tier 4 — Nice-to-Have

| # | Feature | OpenClaw |
|---|---------|----------|
| 19 | Mobile nodes (iOS/Android with Canvas) |
| 20 | macOS menubar companion app |
| 21 | Group chat mention gating & policies |
| 22 | Config includes with deep merge (10 levels) |
| 23 | Response prefix templates (`{model}`, `{identity.name}`) |
| 24 | Ack reactions (emoji confirmations) |
| 25 | Custom chat commands (`/command` in chat) |

---

## Previously Identified Gaps — Now Resolved

These were gaps in the original analysis that have since been implemented:

| Feature | Original Gap | Resolution |
|---------|-------------|------------|
| **Persistent memory** | In-memory only, lost on restart | Soul file + LanceDB vector store (Phase 1) |
| **Session persistence** | In-memory dict, no persistence | SQLite-backed sessions with full history (Phase 1) |
| **Sandboxed execution** | No sandboxing | snekbox Docker sidecar (Phase 2) |
| **Browser automation** | DuckDuckGo text search only | Playwright with headless Chromium (Phase 2) |
| **Shell command execution** | No shell tool | snekbox-based sandboxed execution (Phase 2) |
| **Plugin/skill system** | 4 hardcoded tools | Auto-discovery from `src/tools/` (16 native tools) + MCP integrations (Phase 2) |

---

## Recommended Roadmap

### Phase 1 — Foundation (make the agent robust) — DONE

1. ~~**Persistent sessions + chat history** — SQLite, survive restarts~~
2. ~~**Persistent memory system** — Agent "soul" / long-term recall across sessions~~
3. **Context management** — Compaction/summarization for long conversations
4. **Model fallback chain** — Primary + fallback models, graceful degradation

### Phase 2 — Capability Expansion — DONE

5. ~~**Shell execution tool** — With sandboxing/allowlists~~
6. ~~**Browser automation tool** — Playwright-based, huge capability unlock~~
7. **Media support** — Image send/receive in chat
8. ~~**Plugin/skill system** — User-installable tools without backend code changes~~

### Phase 3 — Operational Maturity

9. **Tool policies** — Allow/deny per session or user
10. **Security sandboxing** — Docker-based tool execution (partially done via snekbox)
11. **Settings UI** — In-app configuration panel
12. **User auth** — Basic identity + session isolation

### Phase 4 — Distribution & Polish

13. **Telegram/Discord bot** — Alternative frontends leveraging existing backend
14. **Scheduled/proactive tasks** — Heartbeat system for autonomous workflows
15. **TTS** — Fits the RPG theme (avatar "speaking" with voice)
16. **Structured logging** — Redaction, multiple output formats

---

## Seraph's Unique Advantage

OpenClaw is headless. Seraph's **visual RPG experience** has no equivalent:

- Phaser 3 village scene with 7 buildings mapped to tool categories
- Animated pixel-art avatar walking between tool stations
- Day/night cycle based on system time
- Idle wandering between 12 waypoints
- Speech bubbles with step content
- Quest log UI with hierarchical goals and domain progress
- Persistent identity and onboarding that builds a relationship
- CRT scanline/vignette retro effects

---

## Sources

- [OpenClaw Official Site](https://openclaw.ai/)
- [OpenClaw Docs — Home](https://docs.openclaw.ai/)
- [OpenClaw Docs — Features](https://docs.openclaw.ai/concepts/features)
- [OpenClaw Docs — Multi-Agent](https://docs.openclaw.ai/concepts/multi-agent)
- [OpenClaw Docs — Configuration](https://docs.openclaw.ai/gateway/configuration)
- [OpenClaw Docs — Security](https://docs.openclaw.ai/gateway/security)
- [OpenClaw Guide 2026 — Gyld Blog](https://gyld.ai/blog/openclaw-open-source-ai-agent-guide-2026)
