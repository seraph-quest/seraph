---
sidebar_position: 3
---

# Phase 3 — The Observer

**Goal**: Seraph understands what you're doing and starts thinking proactively.

**Status**: Planned

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

## 3.4 Native macOS Screen Capture Daemon

**Separate project / directory**: `daemon/` at repo root

**Stack**: Python (familiar, debuggable) with PyObjC for macOS APIs

**Files**:
```
daemon/
  seraph_daemon.py               # Main entry point
  capture/
    __init__.py
    window_tracker.py            # Active window name + title via NSWorkspace
    screen_capture.py            # Screenshot + OCR via Vision framework
    activity_monitor.py          # Idle detection (mouse/keyboard activity)
  config.py                      # Backend URL, capture interval, privacy settings
  requirements.txt               # pyobjc-framework-Cocoa, pyobjc-framework-Vision, httpx
  install.sh                     # Install as launchd service
  uninstall.sh                   # Remove launchd service
```

**Capabilities**:
- **Active window tracking**: App name + window title via `NSWorkspace.sharedWorkspace().frontmostApplication` — lightweight, no screen capture needed
- **Screen OCR** (opt-in): Capture screen region via `CGWindowListCreateImage`, run through macOS Vision framework for text extraction. Local only — no cloud API.
- **Idle detection**: Track time since last mouse/keyboard event via `CGEventSourceSecondsSinceLastEventType`
- **Activity patterns**: Log app switches to detect focus vs. distraction patterns over time

**Communication**:
- Sends context to Docker backend via HTTP POST to `http://localhost:8004/api/observer/context`
- Configurable interval: default every 30 seconds for window tracking, every 5 min for screen OCR
- Backend stores latest context in memory (not DB — ephemeral)

**Privacy**:
- All processing local (macOS Vision framework for OCR)
- Screen content never sent to cloud — only summarized text sent to local Docker backend
- User can disable screen capture entirely (window tracking only)
- User can exclude specific apps from tracking (e.g., 1Password, banking)
- Clear data: daemon stores nothing persistently, backend context is ephemeral

**New API endpoint**: `POST /api/observer/context` — receives context updates from daemon

**Installation**: `launchd` plist for auto-start on login, or manual run. Install/uninstall scripts provided.

---

## 3.5 Proactive Reasoning Engine (Strategist)

**Files**:
```
backend/src/strategist/
  __init__.py
  engine.py                        # Main reasoning loop
  prompts.py                       # System prompts for different contexts
  interventions.py                 # Intervention types + urgency scoring
  queue.py                         # Insight queue (accumulate during focus, deliver at breaks)
```

**Reasoning engine** (`engine.py`):
- Triggered by scheduler every 15 min AND by significant context changes
- Inputs: CurrentContext + UserProfile + relevant memories + goal state
- Process: Calls LLM (cheap model) with strategist prompt:
  ```
  Given everything you know about this human and their current context,
  what is the single highest-leverage insight right now?
  Rate urgency 1-5. If nothing useful, return null.
  Consider: Is this worth spending one of their N remaining attention budget points today?
  ```
- Output: `Intervention(type, message, urgency, reasoning)` or null
- Respects interruption mode and attention budget before delivering

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

**Modified**: `frontend/src/game/scenes/StudyScene.ts`
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

1. **Background scheduler** (APScheduler setup) — infrastructure for everything
2. **Context awareness** (sources: calendar, git, time) — observation inputs
3. **User state machine + interruption modes** — the attention guardian
4. **Proactive reasoning engine + insight queue** — the strategist brain
5. **Intervention delivery** (WS protocol, frontend display, bundles) — output pipeline
6. **Morning briefing + evening review** (scheduled jobs) — first proactive features
7. **Avatar state reflection** (ambient animations) — visual polish
8. **Native macOS daemon** (window tracking, screen OCR) — OS-level observation
9. **Interruption mode UI** (frontend toggle, suggestions) — user control

---

## Verification Checklist

- [ ] APScheduler starts with backend, runs jobs on schedule
- [ ] Calendar scan detects meetings and updates user state
- [ ] Morning briefing appears in chat at configured time
- [ ] Evening review summarizes the day accurately
- [ ] Insights queue during Focus mode, deliver as bundle at transition
- [ ] Attention budget limits mid-day interruptions to 3-5
- [ ] Deep work calendar blocks → zero interruptions
- [ ] Strategist generates useful insights from context
- [ ] Avatar changes behavior when it has pending insights
- [ ] Click Seraph in `has_insight` state → chat opens with queued insights
- [ ] Native daemon sends active window info to backend
- [ ] Screen OCR works locally via macOS Vision framework
- [ ] Mode switcher works (Focus / Balanced / Active)
- [ ] Seraph suggests mode changes based on calendar

---

## Dependencies

### Backend
- `apscheduler>=3.11.0,<4.0.0` (already in pyproject.toml)
- All Phase 2 tools (calendar, email) for context sources

### Native Daemon
- `pyobjc-framework-Cocoa` — macOS window tracking
- `pyobjc-framework-Vision` — on-device OCR
- `pyobjc-framework-Quartz` — screen capture, idle detection
- `httpx` — HTTP client to communicate with backend

### Frontend
- No new packages — Phaser + Zustand + React cover everything
