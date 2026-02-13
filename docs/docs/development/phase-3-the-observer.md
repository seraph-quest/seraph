---
sidebar_position: 3
---

# Phase 3 — The Observer

**Goal**: Seraph understands what you're doing and starts thinking proactively.

**Status**: Phases 3.1–3.5 implemented

---

## Architecture Decisions

- **Deployment**: Docker + native macOS daemon for screen capture. No Tauri migration — stay with the current Docker architecture and add a lightweight local service for OS-level observation.
- **Interruption philosophy**: User attention is a limited, sacred resource. Seraph should be a guardian of focus, not another notification machine. Thinks constantly, speaks rarely and at the right moment.
- **Strategist LLM**: Configurable per job — morning briefing uses the main model (quality matters), background checks use a cheap/fast model to save cost.

---

## 3.1 Interruption Modes

Three levels the user can set manually, or Seraph can suggest switching:

### Focus Mode — Maximum Protection
- Only morning briefing and evening review delivered
- All other insights queued silently
- Avatar shows Seraph is thinking (writing at desk, subtle glow) but never speaks
- Zero mid-day interruptions regardless of urgency
- For: deep work days, crunch time, "leave me alone" days

### Balanced Mode (Default) — Smart Timing
- Morning briefing + evening review (scheduled)
- Batched delivery at **transition moments** (between meetings, after focus blocks end)
- Attention budget: **3-5 non-scheduled touchpoints per day** — forces strategist to prioritize ruthlessly
- Never interrupts deep work blocks or meetings
- Urgent-only bypass: missed meeting alert, same-day deadline
- Queue accumulates during focus/meetings, delivered as one bundle at next break: "3 things while you were focused..."

### Active Mode — Engaged Coaching
- Morning briefing + evening review
- Real-time nudges throughout the day
- Prep reminders 10 min before meetings
- Proactive suggestions when opportunities or risks detected
- Still respects calendar Focus Time blocks
- For: days when user wants active coaching, onboarding period, goal sprints

### Auto-Switching (Suggestions)
Seraph can propose mode changes based on context:
- Heavy calendar (4+ hours meetings) → suggest Balanced
- Clear calendar with one big deadline → suggest Focus
- User hasn't interacted in 2+ days → suggest Active to re-engage
- User manually enters deep work → auto-switch to Focus for that block

### User State Tracking
Derived from calendar + activity + time + native daemon:

| State | Source | Interruption |
|-------|--------|-------------|
| `deep_work` | Calendar focus block, or manual set | **None** (queue everything) |
| `in_meeting` | Calendar event active | **None** (queue everything) |
| `transitioning` | Meeting/focus just ended | **Deliver queued bundle** |
| `available` | No calendar blocks, normal hours | Per-mode rules |
| `away` | No activity for 30+ min | Queue everything |
| `winding_down` | Evening hours | Only evening review |

---

## 3.2 Background Scheduler

**Package**: `apscheduler>=3.11.0,<4.0.0`

**Files**:
```
backend/src/scheduler/
  __init__.py
  engine.py                        # AsyncIOScheduler setup, job registration
  jobs/
    __init__.py
    memory_consolidation.py        # Consolidate recent sessions into long-term memory
    goal_check.py                  # Check goal progress, detect stalled goals
    calendar_scan.py               # Poll calendar for upcoming events
    daily_briefing.py              # Generate morning briefing
    evening_review.py              # Generate evening reflection prompt
    strategist_tick.py             # Periodic proactive reasoning
```

**Scheduler setup** (`engine.py`):
- `AsyncIOScheduler` running on FastAPI's event loop
- Registered in app lifespan (start on startup, shutdown gracefully)
- Jobs configured via settings (enable/disable, schedule times)
- Respects current interruption mode when delivering results

**Job schedule**:

| Job | Default Schedule | Model | Purpose |
|-----|-----------------|-------|---------|
| `memory_consolidation` | Every 30 min | Cheap | Embed recent messages, update vector store |
| `goal_check` | Every 4 hours | Cheap | Calculate progress, detect stalled/overdue goals |
| `calendar_scan` | Every 15 min | None (API only) | Poll calendar, update user state |
| `strategist_tick` | Every 15 min | Cheap | Evaluate context, generate insights (queued) |
| `daily_briefing` | 8:00 AM (configurable) | Main | Morning briefing message |
| `evening_review` | 9:00 PM (configurable) | Main | Evening reflection prompt |

---

## 3.3 Context Awareness Layer

**Files**:
```
backend/src/observer/
  __init__.py
  context.py                       # CurrentContext dataclass + manager
  user_state.py                    # User state machine + interruption mode
  sources/
    __init__.py
    calendar_source.py             # Poll Google Calendar
    git_source.py                  # Watch git repo activity
    time_source.py                 # Time-of-day, day-of-week context
    screen_source.py               # Receive context from native daemon
```

**`CurrentContext`** dataclass:
```python
@dataclass
class CurrentContext:
    time_of_day: str               # morning / afternoon / evening / night
    day_of_week: str               # monday, tuesday, ...
    is_working_hours: bool
    upcoming_events: list          # Next 3 calendar events
    current_event: str | None      # Currently active calendar event
    recent_git_activity: str | None  # Last commit, active branch
    active_goals_summary: str      # Today's tasks + weekly progress
    last_interaction: datetime | None
    user_state: str                # deep_work / in_meeting / available / away / etc.
    interruption_mode: str         # focus / balanced / active
    active_window: str | None      # From native daemon (app name + window title)
    screen_context: str | None     # From native daemon (OCR summary)
    attention_budget_remaining: int  # How many touchpoints left today
```

**User state machine** (`user_state.py`):
- Tracks current state transitions based on calendar + activity + daemon input
- Manages interruption mode (manual override + auto-suggestions)
- Tracks daily attention budget (resets at configured morning hour)
- Determines if a given insight should be delivered now, queued, or dropped

**Context sources**:
- **Calendar source**: Polls Google Calendar every 15 min, caches events. Detects meetings and focus blocks.
- **Git source**: Monitors configured repo directory for recent commits (reads `.git/logs/HEAD`). Non-blocking, filesystem-based.
- **Time source**: Derives from system clock. User's timezone from preferences.
- **Screen source**: Receives HTTP POST from native macOS daemon with active window info and optional OCR text.

---

## 3.4 Native macOS Screen Daemon

**Status**: Implemented (Phase 3.5) — Level 0: app name + window title

**Directory**: `daemon/` at repo root

**Stack**: Python 3.12 + PyObjC + httpx

**Files**:
```
daemon/
  seraph_daemon.py               # Main entry point — polling daemon
  requirements.txt               # pyobjc-framework-Cocoa, pyobjc-framework-Quartz, httpx
  run.sh                         # Quick start script (creates venv, installs deps, runs)
  README.md                      # Setup, permissions, usage, troubleshooting
```

**Capabilities (Level 0 — current)**:
- **Active window tracking**: App name via `NSWorkspace.sharedWorkspace().frontmostApplication` (no permission) + window title via AppleScript (Accessibility permission)
- **Idle detection**: Seconds since last input via `Quartz.CGEventSourceSecondsSinceLastEventType` (no permission)
- **Change detection**: Skips POST if `active_window` unchanged since last POST
- **Idle gating**: Skips POST if user idle for `--idle-timeout` seconds (default 300)

**IDE-based deep work detection** (`backend/src/observer/user_state.py`):
- `_DEEP_WORK_APPS` tuple matches IDE/terminal app names (VS Code, Xcode, IntelliJ, iTerm, etc.)
- When `active_window` matches an IDE and no calendar event is active, user state becomes `deep_work`
- Calendar events always take priority (meeting overrides IDE)

**Communication**:
- POST to `http://localhost:8004/api/observer/context` with `{"active_window": "App — Title", "screen_context": null}`
- CLI args: `--url`, `--interval` (default 5s), `--idle-timeout` (default 300s), `--verbose`
- Graceful shutdown on SIGINT/SIGTERM, handles backend-down with warning + retry

**Privacy**:
- Only captures app name + window title — no screenshots, keystrokes, or screen content
- Requires only Accessibility permission (one-time grant, no monthly nag)
- All data stays local (daemon posts to localhost backend only)

**Future upgrade path** (see [Screen Daemon Research](./screen-daemon-research)):
- Level 1: + OCR text extraction (requires Screen Recording permission)
- Level 2: + Local VLM descriptions (FastVLM/Moondream, ~1GB RAM)
- Level 3: + Cloud VLM on-demand (Gemini Flash-Lite ~$0.09/mo at 1/5min)

**Quick start**: `./daemon/run.sh --verbose`

---

## 3.5 Proactive Reasoning Engine (Strategist)

**Status**: Implemented (Phase 3.4)

**Files**:
```
backend/src/agent/strategist.py      # Agent factory + response parser
backend/src/scheduler/jobs/
  strategist_tick.py                 # Scheduler job (every 15 min)
  daily_briefing.py                  # Morning briefing (cron 8 AM)
  evening_review.py                  # Evening review (cron 9 PM)
backend/src/observer/
  delivery.py                        # deliver_or_queue() — routes through attention guardian
  insight_queue.py                   # DB-backed queue (24h expiry)
```

**Strategist agent** (`src/agent/strategist.py`):
- Restricted smolagents `ToolCallingAgent` with tools: `view_soul`, `get_goals`, `get_goal_progress`
- Temperature 0.4, max_steps 5 — lightweight reasoning
- System prompt includes `proactivity_level` (1-5) and current context block
- Returns structured JSON: `{should_intervene, content, intervention_type, urgency, reasoning}`
- `parse_strategist_response()` strips markdown fences, falls back to `should_intervene=False` on parse failure

**Strategist tick** (`strategist_tick.py`):
- Triggered by scheduler every 15 min
- Refreshes context → creates strategist agent → runs in thread → parses decision
- Routes through `deliver_or_queue()` which applies the attention guardian gate

**Daily briefing** (`daily_briefing.py`):
- Uses LiteLLM directly (lighter than full agent)
- Gathers: soul file, calendar events, active goals, relevant memories
- Delivers with `is_scheduled=True` — bypasses delivery gate

**Evening review** (`evening_review.py`):
- Uses LiteLLM directly
- Gathers: soul file, today's message count, completed goals today, git activity
- Delivers with `is_scheduled=True` — bypasses delivery gate

**Intervention types**:
```python
class InterventionType(str, Enum):
    AMBIENT = "ambient"      # Visual cue only (avatar behavior change)
    QUEUED = "queued"        # Added to queue, delivered at next transition
    ADVISORY = "advisory"    # Direct message (only if available + budget allows)
    URGENT = "urgent"        # Bypasses budget (missed meeting, same-day deadline)
```

**Urgency scoring**:
- 1: Ambient only — avatar behavior change, no text
- 2: Queue for next transition moment bundle
- 3: Deliver if available + budget allows, otherwise queue
- 4: Deliver at next break regardless of budget
- 5: Urgent bypass — deliver immediately unless deep_work mode

**Insight queue** (`queue.py`):
- Accumulates insights during focus/meeting states
- On state transition to `available` or `transitioning`: deliver as batched bundle
- "While you were focused: [3 items]"
- Oldest items expire after 24 hours
- Queue persists across backend restarts (stored in DB)

---

## 3.6 Intervention Delivery

**WebSocket protocol** — new server-initiated message types:

```json
{
  "type": "proactive",
  "content": "Your Q1 goal is 40% behind pace. Want to re-strategize?",
  "urgency": 3,
  "intervention_type": "advisory",
  "reasoning": "Goal velocity has dropped 25% this week"
}
```

```json
{
  "type": "proactive_bundle",
  "items": [
    {"content": "Investor follow-up email drafted", "urgency": 2},
    {"content": "Tomorrow has 3 back-to-back meetings — suggest Focus mode", "urgency": 2},
    {"content": "Weekly run goal: 2/4 completed, suggest Thursday morning", "urgency": 1}
  ]
}
```

**Frontend intervention display**:

| Type | Visual |
|------|--------|
| AMBIENT | Seraph avatar changes behavior (writes at desk, paces, looks concerned) |
| QUEUED (bundled) | Chat panel shows bundled message with gold "insight" styling at transition |
| ADVISORY | Message appears in chat with proactive styling (gold border) |
| URGENT | Chat panel auto-opens, alert-styled message |

**Modified**: `frontend/src/components/chat/MessageBubble.tsx`
- Proactive messages styled with gold border, seraph icon
- Bundle messages collapsible ("3 insights" → expand to see all)

**Modified**: `frontend/src/hooks/useWebSocket.ts`
- Handle `proactive` and `proactive_bundle` message types
- Route to appropriate display

---

## 3.7 Morning Briefing & Evening Review

**Morning briefing** (delivered at configured time, uses main model):
```
Good morning, [Name].

Today's Schedule:
- 10:00 — Standup with team
- 14:00 — Investor call (prep: they asked about revenue last time)
- 16:00 — Deep work block

Goal Progress:
- Launch MVP: 75% (on track)
- Marathon training: behind pace — no run in 3 days

Today's Priority:
→ Finish the API integration (blocks 2 other tasks)

Heads up:
- Investor pitch deck due Friday (3 days)

Mode suggestion: Heavy meeting day — staying in Balanced mode.
```

**Evening review** (delivered at configured time, uses main model):
```
Day's end, [Name].

What you accomplished:
- 3 commits on feature branch
- Investor call went well (captured in notes)
- Missed planned run

Reflection:
- Your deep work block got interrupted by Slack — consider DND mode tomorrow
- The investor seemed interested in X — follow up?

Tomorrow preview:
- 2 meetings, 4 hours of open time
- Marathon training: suggest morning slot

Queued insights I held back today: 0
```

---

## 3.8 Avatar State Reflection

**Modified**: `frontend/src/game/objects/AgentSprite.ts`
- New ambient animations based on Seraph's strategic state:
  - **has_insight**: Seraph sits at desk writing (quill animation), subtle glow
  - **goal_behind**: Seraph looks concerned, slower pace
  - **on_track**: Seraph meditates calmly, bright aura
  - **waiting**: Seraph waves occasionally

**Modified**: `frontend/src/game/scenes/VillageScene.ts`
- EventBus handler for `"agent-ambient-state"` events
- Seraph's idle behavior changes based on proactive state
- Click Seraph while in `has_insight` state → chat opens with queued insights

**WS message type**:
```json
{
  "type": "ambient",
  "state": "has_insight",
  "tooltip": "Seraph has thoughts about your Q1 progress"
}
```

---

## 3.9 Interruption Mode UI

**New frontend component**: Settings accessible from village UI (gear icon or right-click Seraph)

**Mode switcher**: Three-state toggle — Focus / Balanced / Active
- Visual indicator in village (e.g., small icon near Seraph or in corner)
- Seraph can suggest mode changes via proactive message: "Heavy calendar tomorrow — switch to Focus?"

**API endpoints**:
- `GET /api/settings/interruption-mode` — current mode
- `PUT /api/settings/interruption-mode` — change mode
- `GET /api/observer/state` — current user state + context summary
- `POST /api/observer/context` — receive updates from native daemon

---

## Implementation Order

1. ~~**Background scheduler** (APScheduler setup) — infrastructure for everything~~ ✅ Phase 3.1
2. ~~**Context awareness** (sources: calendar, git, time, goals) — observation inputs~~ ✅ Phase 3.2
3. ~~**User state machine + interruption modes** — the attention guardian~~ ✅ Phase 3.3
4. ~~**Proactive reasoning engine + strategist agent + insight queue**~~ ✅ Phase 3.4
5. ~~**Morning briefing + evening review** (scheduled LiteLLM jobs)~~ ✅ Phase 3.4
6. ~~**Frontend ambient indicator + nudge speech bubble**~~ ✅ Phase 3.4
7. ~~**Native macOS daemon** (window tracking + IDE deep work detection)~~ ✅ Phase 3.5
8. **Avatar state reflection** (ambient Phaser animations) — visual polish
9. ~~**Interruption mode UI** (frontend toggle, suggestions) — user control~~ ✅ Phase 3.5.2

---

## Verification Checklist

- [x] APScheduler starts with backend, runs jobs on schedule (Phase 3.1)
- [x] Calendar scan detects meetings and updates user state (Phase 3.2)
- [x] User state machine derives state from context sources (Phase 3.3)
- [x] Delivery gate routes messages: deliver / queue / drop (Phase 3.3)
- [x] Attention budget limits mid-day interruptions (Phase 3.3)
- [x] Insights queue during blocked states, deliver as bundle on transition (Phase 3.3)
- [x] Strategist agent generates decisions from context (Phase 3.4)
- [x] Morning briefing generates narrative and delivers with `is_scheduled=True` (Phase 3.4)
- [x] Evening review generates reflection and delivers with `is_scheduled=True` (Phase 3.4)
- [x] Frontend ambient indicator dot in HudButtons (Phase 3.4)
- [x] Frontend nudge speech bubble on agent sprite for 5s (Phase 3.4)
- [x] Alert/advisory proactive messages open chat panel (Phase 3.4)
- [ ] Avatar changes behavior when it has pending insights
- [ ] Click Seraph in `has_insight` state → chat opens with queued insights
- [x] Native daemon sends active window info to backend (Phase 3.5)
- [x] IDE/terminal detection triggers deep_work user state (Phase 3.5)
- [x] Screen OCR works locally via macOS Vision framework (Phase 3.5)
- [x] Mode switcher works (Focus / Balanced / Active) (Phase 3.5.2)
- [ ] Seraph suggests mode changes based on calendar

---

## Dependencies

### Backend
- `apscheduler>=3.11.0,<4.0.0` (already in pyproject.toml)
- All Phase 2 tools for context sources (calendar is observer-only via `calendar_source.py`, no email dependency)

### Native Daemon
- `pyobjc-framework-Cocoa` — macOS window tracking
- `pyobjc-framework-Vision` — on-device OCR
- `pyobjc-framework-Quartz` — screen capture, idle detection
- `httpx` — HTTP client to communicate with backend

### Frontend
- No new packages — Phaser + Zustand + React cover everything
