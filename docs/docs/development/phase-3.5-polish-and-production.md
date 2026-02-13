---
sidebar_position: 4
---

# Phase 3.5 — Polish & Production Readiness

**Goal**: Complete unfinished Phase 3 UI work, fix backend gaps, harden infrastructure, and eliminate adoption barriers — making what exists production-quality before expanding in Phase 4.

**Status**: Planned

**Context**: The REPORT.md analysis (2026-02-12) identified several gaps between Phases 3 (complete) and 4 (planned) — items that are neither new features nor security concerns, but quality, robustness, and developer experience improvements needed to ship a solid product.

---

## 3.5.1 Goal Management UI

Backend has full CRUD APIs (`POST /api/goals`, `PATCH /api/goals/{id}`, `DELETE /api/goals/{id}`, `GET /api/goals/tree`, `GET /api/goals/dashboard`), but users can only create goals through chat.

**Files**:
```
frontend/src/components/quest/
  GoalForm.tsx             # Create/edit goal modal (title, level, parent, domain, notes)
  GoalActions.tsx          # Inline edit/delete/complete buttons on GoalTree nodes
  QuestSearch.tsx          # Search bar + filter/sort controls for QuestPanel
```

**Design**:
- **GoalForm**: Modal triggered by "+" button in QuestPanel header
  - Fields: title, level (daily → life_vision), domain (productivity/performance/health/influence/growth), parent goal (dropdown from tree), notes
  - Edit mode: same form pre-filled, triggered by pencil icon on GoalTree nodes
  - Calls `POST /api/goals` (create) or `PATCH /api/goals/{id}` (edit)
- **GoalActions**: Inline action buttons on GoalTree nodes
  - Complete (check icon) → `PATCH /api/goals/{id}` with `status: "completed"`
  - Edit (pencil icon) → opens GoalForm in edit mode
  - Delete (trash icon, with confirmation) → `DELETE /api/goals/{id}`
- **QuestSearch**: Search/filter/sort for quest panel
  - Text search across goal titles
  - Filter by: level, domain, status (active/completed/stalled)
  - Sort by: created date, due date, progress percentage

**RPG styling**: Pixel-border modal matching existing DialogFrame aesthetic. Form inputs with retro styling consistent with Settings panel.

---

## 3.5.2 Interruption Mode UI

Phase 3.9 documented this but it was never implemented. The backend API exists at `GET/PUT /api/settings/interruption-mode`.

**Files**:
```
frontend/src/components/settings/
  InterruptionModeToggle.tsx   # Three-state toggle (Focus / Balanced / Active)
```

**Design**:
- Three-state toggle component in Settings panel
- Visual descriptions for each mode:
  - **Focus**: Shield icon — "Only scheduled briefings. Zero interruptions."
  - **Balanced**: Scale icon — "Smart timing. 3-5 touchpoints per day." (default)
  - **Active**: Lightning icon — "Real-time coaching. Proactive nudges."
- Calls `PUT /api/settings/interruption-mode` on change
- Current mode indicator visible in HudButtons area (small icon next to ambient dot)
- Seraph can suggest mode changes via proactive messages (already supported in backend)

---

## 3.5.3 Avatar State Reflection

Phase 3.8 documented this but avatar still just wanders. The `ambient` WebSocket messages already flow to the frontend — the Phaser-level visual changes are missing.

**Files**:
```
frontend/src/game/objects/AgentSprite.ts   # Add ambient state handling
frontend/src/game/scenes/VillageScene.ts   # Wire EventBus ambient events to sprite
```

**Design**:
- AgentSprite receives ambient state from EventBus `"agent-ambient-state"` events
- State-specific idle behaviors:
  - `has_insight`: Seraph sits at bench, writing animation, subtle glow particles
  - `goal_behind`: Seraph paces slowly, darker tint overlay
  - `on_track`: Seraph meditates at well, bright aura
  - `waiting`: Seraph waves occasionally from center
- Click Seraph in `has_insight` state → opens chat with queued insights
- Wandering behavior suspended while in ambient state (resumes on `idle`)

---

## 3.5.4 Calendar Scan Job

The `calendar_scan` job is registered in the scheduler but the implementation appears incomplete — the calendar source needs actual Google Calendar API integration.

**Files**:
```
backend/src/scheduler/jobs/calendar_scan.py    # Complete implementation
backend/src/observer/sources/calendar_source.py # Google Calendar API polling
```

**Design**:
- Poll Google Calendar API every 15 minutes (already scheduled)
- Cache events in `CurrentContext.upcoming_events`
- Detect state transitions: meeting starting → `in_meeting`, meeting ending → `transitioning`
- Requires Google Calendar OAuth token (setup in Settings or `.env`)
- Graceful degradation: if no calendar configured, job runs but returns empty

---

## 3.5.5 Token-Aware Context Window

Session history is fixed at 50 messages. Long conversations lose important context as older messages are truncated without summarization.

**Files**:
```
backend/src/agent/context_window.py    # Adaptive token-based windowing
```

**Design**:
- Replace fixed 50-message window with token-counting approach
- Use `tiktoken` (or model-specific tokenizer) to count message tokens
- Target budget: 80% of model context window for history (remaining for system prompt + tools)
- When budget exceeded:
  1. Keep first 2 messages (system context) + last 20 messages (recent context)
  2. Summarize middle messages into a "conversation so far" block using cheap model
  3. Cache summaries to avoid re-computing
- Configurable via settings: `context_window_strategy` = `fixed` (current) | `adaptive` (new)

---

## 3.5.6 Agent Execution Timeout

No timeout handling for agent execution — can hang indefinitely on slow API calls or tool execution.

**Files**:
```
backend/src/agent/timeout.py           # Timeout wrapper for agent runs
```

**Design**:
- Wrap agent execution in `asyncio.wait_for()` with configurable timeout
- Default timeout: 120 seconds for chat responses, 60 seconds for strategist ticks
- On timeout: return graceful error message to user ("I'm taking too long — let me try a simpler approach")
- Log timeout events for monitoring
- Per-tool timeout support: `shell_execute` gets 30s, `browse_webpage` gets 60s, others 15s

---

## 3.5.7 Tauri Desktop App

The Docker requirement is the biggest barrier to adoption. OpenClaw runs with `npx openclaw`. Seraph requires Docker Compose + daemon setup. A thorough Tauri analysis exists at `docs/architecture/tauri-analysis.md`.

**Files**:
```
desktop/
  src-tauri/
    src/main.rs              # Tauri entry point, backend process management
    tauri.conf.json          # App config, window settings, permissions
    Cargo.toml
  src/                       # Frontend (reuse existing React app)
    main.tsx
  package.json
```

**Design**:
- Tauri v2 wraps the existing React frontend in a native window
- Sidecar process: bundle the Python backend as a PyInstaller binary
- Bundle the daemon: no separate daemon process needed — Tauri's Rust layer handles window polling directly via macOS APIs
- System tray: always-on presence with menu (open village, interruption mode toggle, quit)
- Native notifications: replace WebSocket-only delivery with OS-level notifications
- Auto-updater: Tauri's built-in update mechanism
- Single `.dmg` installer — eliminates Docker, Python, and Node.js requirements

**Migration path**:
1. Package frontend as Tauri app (web view)
2. Bundle backend as sidecar (PyInstaller)
3. Move daemon functionality to Rust (Tauri plugin)
4. Add system tray + notifications
5. Build auto-updater + installer

---

## 3.5.8 Infrastructure Hardening

Several infrastructure gaps identified that affect developer experience and deployment.

### Production Docker Compose

**Files**:
```
docker-compose.prod.yaml      # Production configuration
```

**Design**:
- Health checks for all services (`/health` endpoint)
- Restart policies (`unless-stopped`)
- Resource limits (memory, CPU)
- Volume mounts for persistent data (`data/`, `logs/`)
- Non-root user in containers
- Log rotation configuration

### CI/CD Pipeline

**Files**:
```
.github/workflows/
  ci.yaml                    # Lint, type-check, test on PR
  build.yaml                 # Build Docker images on merge to main
```

**Design**:
- **CI** (on PR): TypeScript type-check (`tsc --noEmit`), ESLint, Python type-check (`mypy`), Python lint (`ruff`), unit tests
- **Build** (on merge): Build and tag Docker images, push to GitHub Container Registry
- Badge in README showing CI status

### API Key Security

- Rotate the OpenRouter key committed in `.env.dev`
- Move to `.env.dev.example` with placeholder values
- Add `.env.dev` to `.gitignore`
- Document environment variable setup in README

---

## 3.5.9 Frontend Accessibility

Small but impactful UI improvements.

**Files**:
```
frontend/src/index.css                    # Font size adjustments
frontend/src/components/HudButtons.tsx    # Keyboard shortcut hints
```

**Design**:
- Increase minimum font sizes from 7-9px to 11-12px in chat messages and quest panel
- Add keyboard shortcuts: `C` toggle chat, `Q` toggle quests, `S` toggle settings, `Esc` close active panel
- Show shortcut hints on HudButtons hover tooltips

---

## Implementation Order

1. **Interruption Mode UI** (3.5.2) — lowest effort, unlocks attention guardian for users
2. **Goal Management UI** (3.5.1) — low effort, backend already complete
3. **Frontend Accessibility** (3.5.9) — quick wins, improves daily usability
4. **Avatar State Reflection** (3.5.3) — visual polish, leverages existing ambient system
5. **Agent Execution Timeout** (3.5.6) — backend robustness, prevents hangs
6. **Token-Aware Context Window** (3.5.5) — conversation quality improvement
7. **Calendar Scan Job** (3.5.4) — enables calendar-aware proactivity
8. **Infrastructure Hardening** (3.5.8) — CI/CD + prod Docker + key rotation
9. **Tauri Desktop App** (3.5.7) — largest effort, biggest adoption impact, do last

---

## Verification Checklist

- [ ] Goal create/edit modal works from QuestPanel
- [ ] Goal delete with confirmation works
- [ ] Quest panel search filters goals by text
- [ ] Interruption mode toggle changes mode via API
- [ ] Current interruption mode shown in HUD area
- [ ] Avatar changes behavior in has_insight / goal_behind / on_track states
- [ ] Click avatar in has_insight state opens chat
- [ ] Calendar scan job polls Google Calendar and updates context
- [ ] Long conversations (100+ messages) maintain quality with adaptive windowing
- [ ] Agent execution times out gracefully after 120s
- [ ] `docker-compose.prod.yaml` starts all services with health checks
- [ ] GitHub Actions CI runs on PR (lint + type-check)
- [ ] No API keys committed in repo
- [ ] Font sizes readable (11px+) in chat and quest panels
- [ ] Keyboard shortcuts toggle panels
- [ ] Tauri app launches with embedded backend (stretch goal)

---

## Dependencies

### Backend
- `tiktoken>=0.8` — Token counting for adaptive context window
- `google-auth-oauthlib>=1.2` — Google Calendar OAuth (for calendar scan)
- `google-api-python-client>=2.0` — Google Calendar API client

### Frontend
- No new packages — React + Zustand + Phaser + Tailwind cover everything

### Infrastructure
- GitHub Actions (free for public repos)
- PyInstaller (for Tauri sidecar bundling)
- Tauri v2 CLI (`cargo install tauri-cli`)
