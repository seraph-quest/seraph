---
sidebar_position: 2
---

# Seraph — Product Roadmap

> Each phase builds on the previous. The trajectory moves from **reactive tool** → **persistent partner** → **proactive observer** → **strategic guardian** → **life operating system**.

---

## Phase 0 — Foundation

**Status**: Complete

What was built:
- Chat interface with WebSocket streaming
- 4 tools (web search, file read/write, template fill)
- Animated RPG avatar in Phaser 3 village scene
- Day/night cycle, idle wandering, speech bubbles
- In-memory sessions (no persistence)
- Single model via OpenRouter

---

## Phase 1 — Persistent Identity

**Theme**: Seraph remembers. Conversations have continuity. The human exists between sessions.

### 1.1 Persistent Sessions & Chat History
- SQLite (or Postgres) backed session storage
- Full conversation history survives restarts
- Session list UI (switch between conversations)
- Conversation search

### 1.2 Soul System (Long-Term Memory)
- `soul.md` — Core identity file: user's name, values, goals, personality notes
- Seraph reads and references soul on every interaction
- User can view and edit their soul file through the UI
- Seraph proposes soul updates after meaningful conversations ("I learned something about you — should I remember this?")

### 1.3 Goal Hierarchy (The Quest Log)
- Data model for hierarchical goals:
  ```
  Life Vision → Annual Objectives → Quarterly Key Results
  → Monthly Milestones → Weekly Plans → Daily Tasks
  ```
- Quest Log UI panel in the RPG interface
- Seraph can create, update, and decompose goals through conversation
- Progress tracking with completion percentages at every level
- Goal conflict detection ("These two objectives compete for the same time")

### 1.4 User Onboarding Flow
- First-run experience where Seraph asks about:
  - Who the user is (name, role, context)
  - What they're trying to achieve (top 3 life goals)
  - What a great day/week/month looks like
  - What their biggest obstacles are
  - How proactive they want Seraph to be (dial from passive to aggressive)
- Populates soul.md and initial goal hierarchy from this conversation
- RPG-style: "A new hero enters the village..."

### Milestone
Seraph knows who you are, what you want, and remembers everything.

---

## Phase 2 — Capable Executor

**Theme**: Seraph can actually do things in the world, not just talk about them.

### 2.1 Shell Execution Tool
- Sandboxed shell command execution (Docker-based)
- Configurable permissions (read-only, workspace-scoped, full)
- Output streaming to chat
- New village location: **Forge** (avatar walks to anvil/forge for code/shell tasks)

### 2.2 Browser Automation Tool
- Playwright-based browser control
- Navigate, extract, fill forms, screenshot
- Sandboxed browser instance (no access to user's real browser)
- New village location: **Observatory/Tower** (avatar climbs tower for web exploration)

### 2.3 Calendar & Email Integration
- Google Calendar / Outlook read/write
- Gmail / Outlook email read, compose, send (with user approval)
- New village locations: **Mailbox** (email), **Town Clock** (calendar)

### 2.4 Note-Taking & Knowledge Base
- Integration with Obsidian / local markdown vault
- Seraph can read, search, create, and link notes
- New village location: **Library** (avatar browses shelves)

### 2.5 Plugin/Skill Architecture
- Tool registration API (define new tools without modifying core)
- Tool metadata (name, description, required permissions, village location, avatar animation)
- Community skill directory (future)
- Each new tool automatically gets a village building and avatar animation

### Milestone
Seraph can search the web, run code, manage your calendar, handle email, take notes, and learn new skills.

---

## Phase 3 — The Observer

**Theme**: Seraph watches (with consent) and understands what the human is doing.

**Status**: Complete (Phases 3.1–3.5). Background scheduler, context awareness, user state machine, strategist agent, daily briefing, evening review, frontend ambient/nudge feedback, and native macOS screen daemon are all operational. Pattern detection, behavioral state inference, and avatar state reflection remain planned for future iterations.

### 3.1 Context Awareness Layer ✅
- **Active application detection** — What app/site is the user on? *(API contract documented, daemon deferred)*
- **Calendar awareness** — What's coming up? What just ended?
- **Git activity monitoring** — Commits, branches, build status
- **Idle/active detection** — Is the user at their machine? *(basic: last interaction tracking)*
- All observation is opt-in, configurable per source, locally processed

### 3.2 Screen Context (Optional, Opt-In)
- Periodic screen capture → local vision model extraction → discard image
- Extracts: active document name, key content, current task context
- Never stores raw screenshots — only extracted semantic context
- User controls frequency (never / every 5 min / every minute)

*Status: Planned. Backend endpoint and `ContextManager.update_screen_context()` are ready. Native daemon deferred.*

### 3.3 Working Memory System
- Real-time context buffer: what the user is doing *right now*
- Combines: active task, recent messages, calendar, observations
- Pruned aggressively — only the relevant survives
- Fed to Strategist as "current situation" context

*Status: Partially implemented via `CurrentContext.to_prompt_block()` — aggregates time, calendar, git, goals, screen context, and user state into a text block for agent injection.*

### 3.4 Pattern Detection Engine
- Tracks over time: work hours, break patterns, productivity cycles
- Detects: energy peaks/valleys, procrastination triggers, context-switch frequency
- Identifies: recurring behaviors, habit formation/decay, stress signals
- Stores patterns in long-term memory, not raw data

*Status: Planned.*

### 3.5 State Inference
- Estimates current human state: focused / distracted / stressed / energized / idle
- Uses: typing speed, app switching frequency, time of day, calendar density
- Feeds into Advisor's interruption intelligence

*Status: Partially implemented. User state machine derives state from calendar + time + activity, but not from behavioral signals like typing speed or app switching.*

### 3.6 Avatar Reflects Human State
- Avatar's idle behavior changes based on inferred state:
  - **Focused**: Avatar meditates peacefully, glowing aura
  - **Stressed**: Avatar paces, darker lighting
  - **Energized**: Avatar practices sword forms, bright scene
  - **Idle/resting**: Avatar sleeps, fireflies in scene
  - **Distracted**: Avatar looks around nervously

*Status: Planned. Frontend ambient indicator dot and nudge speech bubble are implemented (Phase 3.4), but Phaser-level avatar animation changes are not yet wired.*

### Milestone
Seraph sees what you're doing, understands your patterns, and its avatar mirrors your state.

---

## Phase 3.5 — Polish & Production Readiness

**Theme**: Make what exists production-quality. Complete unfinished UI, fix backend gaps, harden infrastructure, eliminate adoption barriers.

**Status**: Planned

### 3.5.1 Goal Management UI
- Create/edit/delete goals via dedicated form (not just chat)
- Quest panel search, filter by domain/level/status, sort
- Backend APIs already exist — this is frontend-only

### 3.5.2 Interruption Mode UI
- Focus / Balanced / Active toggle in Settings panel
- Mode indicator in HUD area
- Backend API exists at `/api/settings/interruption-mode`

### 3.5.3 Avatar State Reflection
- Avatar behavior changes based on ambient state (has_insight, goal_behind, on_track)
- Click avatar in has_insight state → opens chat with queued insights
- Ambient WebSocket messages already flow — Phaser visuals missing

### 3.5.4 Calendar Scan Completion
- Complete the calendar_scan job with Google Calendar API integration
- Enable calendar-aware user state transitions

### 3.5.5 Token-Aware Context Window
- Replace fixed 50-message window with adaptive token counting
- Summarize middle messages when budget exceeded
- Prevent quality degradation in long conversations

### 3.5.6 Agent Execution Timeout
- Timeout handling for agent runs (120s default)
- Per-tool timeouts (shell 30s, browser 60s, others 15s)
- Graceful error messages on timeout

### 3.5.7 Tauri Desktop App
- Native macOS app (single `.dmg` installer)
- Eliminates Docker + Python + Node.js requirements
- System tray, native notifications, auto-updater
- Bundles backend as sidecar, daemon as Rust plugin

### 3.5.8 Infrastructure Hardening
- Production Docker Compose with health checks, restart policies, resource limits
- CI/CD pipeline (GitHub Actions: lint, type-check, test on PR)
- API key security (rotate exposed key, `.gitignore` protection)

### 3.5.9 Frontend Accessibility
- Increase minimum font sizes (7-9px → 11-12px)
- Keyboard shortcuts for panel toggling (C/Q/S/Esc)

### Milestone
Everything that exists is robust, polished, and installable. Ready to expand.

---

## Phase 4 — The Strategist

**Theme**: Seraph doesn't just observe — it *thinks*. Continuously reasoning about how to elevate its human.

### 4.1 Proactive Reasoning Engine
- Background process that runs on a schedule (every 15 min) and on triggers
- Inputs: current context, user model, goal hierarchy, calendar, recent patterns
- Outputs: ranked list of potential interventions with urgency scores
- Uses chain-of-thought reasoning: "Given that the user's Q1 goal is X, and they've spent today doing Y, and tomorrow they have Z..."

### 4.2 Interruption Intelligence
- Cost/benefit calculator for each potential intervention
- Factors: urgency, user receptivity state, time until window closes, intervention history
- Urgency levels: Ambient → Nudge → Advisory → Alert → Autonomous
- Respects user's proactivity dial (set during onboarding, adjustable anytime)
- Learns from dismissal/engagement patterns

### 4.3 Morning Briefing
- Proactive daily briefing at user's configured time
- Contents:
  - Today's calendar with strategic notes
  - Goal progress snapshot (on track / behind / ahead)
  - Priority recommendations for the day
  - Unresolved items from yesterday
  - Relevant news/information (from user's interest areas)
- Delivered as RPG "quest briefing" in the village

### 4.4 Evening Review
- End-of-day reflection prompt
- What was accomplished vs. planned
- Goal progress updates
- "What did you learn today?" prompt
- Tomorrow preview and planning
- RPG "campfire scene" — avatar and user review the day

### 4.5 Decision Support
- When the user faces a decision, Seraph can:
  - Run a pre-mortem ("Imagine this decision failed — why?")
  - Analyze second/third-order consequences
  - Reference similar past decisions and their outcomes
  - Present a structured pros/cons with weighted scoring
  - Play devil's advocate on request

### 4.6 Mistake Prevention
- Pattern-based alerts:
  - "You're about to commit credentials to a public repo"
  - "This email tone is more aggressive than your usual style"
  - "You've scheduled 6 hours of meetings tomorrow — when will you do deep work?"
  - "You've been on social media for 45 minutes — your deadline is in 2 days"
  - "Last time you made this type of decision under stress, the outcome was..."

### 4.7 Weekly Strategy Session
- Weekly proactive conversation (user picks the day/time)
- Review: goal progress, wins, setbacks, patterns
- Plan: next week's priorities, time allocation, key decisions
- Adjust: goal timelines, reprioritize if needed
- RPG framing: "War room" scene in the village

### Milestone
Seraph thinks about your life constantly, intervenes at the right moments, and helps you strategize.

---

## Phase 5 — The Guardian

**Theme**: Seraph operates autonomously within boundaries. It doesn't just advise — it acts.

### 5.1 Autonomous Actions (Pre-Authorized)
- User defines action classes Seraph can take without asking:
  - Block calendar conflicts with deep work
  - Decline meetings that don't serve current priorities (with templated response)
  - Send pre-approved check-in messages
  - Create and assign tasks from conversations
  - File and organize notes
- Each autonomous action is logged and reviewable

### 5.2 Scheduled/Heartbeat Tasks
- Cron-like system for recurring autonomous work:
  - Morning: check calendar, prepare briefing, scan email for urgent items
  - Periodic: monitor goal-relevant metrics, check deadlines
  - Evening: compile daily summary, prepare tomorrow's plan
  - Weekly: generate progress report, detect stalled goals
- User configures which heartbeats are active

### 5.3 Multi-Channel Delivery
- Telegram / Discord bot as notification channel
- Push notifications (PWA or mobile app)
- Email digests for non-urgent items
- The RPG web UI remains the primary deep-interaction interface
- Channel selection based on urgency and user preferences

### 5.4 Delegation Intelligence
- Seraph identifies tasks it can handle autonomously vs. tasks that need the human
- "I can research this for you in the background. Want me to?"
- "This requires a decision only you can make. Here's what you need to know."
- Background task execution with status updates in the quest log

### 5.5 Accountability System
- Tracks commitments the user makes (in conversation or during planning)
- Gentle follow-ups: "You said you'd finish X by today. How's it going?"
- Patterns: "You've rescheduled this commitment 3 times. Let's either do it or drop it."
- Never nagging — strategic accountability based on the user's own stated priorities

### Milestone
Seraph acts on your behalf within defined boundaries, follows up on your commitments, and reaches you where you are.

---

## Phase 6 — The Life Operating System

**Theme**: The full vision realized. Seraph as the operating layer between the human and their life.

### 6.1 RPG Life Dashboard
- Full character sheet with real stats computed from real data:
  - **Vitality** (health, energy, sleep, exercise)
  - **Focus** (deep work hours, context switches, flow states)
  - **Influence** (network activity, communication quality, reputation signals)
  - **Wisdom** (learning velocity, reflection frequency, decision quality)
  - **Execution** (goal completion rate, task velocity, commitment follow-through)
- Leveling system based on sustained stat improvement
- Historical graphs showing progression over months/years

### 6.2 Village Evolution
- Village visually evolves based on life progress:
  - New buildings unlock as capabilities expand
  - Buildings upgrade as related skills improve (library grows, forge gets better)
  - Neglected domains show visible decay (motivational feedback)
  - Seasonal events tied to quarterly goal cycles
- The village becomes a mirror of the user's life investment

### 6.3 Network Intelligence
- Relationship mapping: key people, interaction frequency, relationship health
- Proactive: "You haven't connected with [mentor] in 2 months — they're important to [goal]"
- Meeting prep: "Here's context on everyone in your 3pm meeting"
- Communication coaching: real-time tone and strategy suggestions

### 6.4 Financial Awareness (Optional)
- Spending pattern awareness (with explicit opt-in)
- Alignment checking: "Your spending this month doesn't align with your savings goal"
- Investment/opportunity flagging relevant to stated financial goals

### 6.5 Health Integration (Optional)
- Wearable data integration (Apple Health, WHOOP, Fitbit)
- Correlate health data with productivity patterns
- "Your deep work quality drops 40% on days you sleep under 6 hours"
- Proactive health-productivity recommendations

### 6.6 Multi-Agent Architecture
- Specialized sub-agents for different domains:
  - **Sentinel** — Security, mistake prevention, risk monitoring
  - **Strategist** — Goal planning, decision support, trajectory analysis
  - **Executor** — Task execution, tool operation, background work
  - **Chronicler** — Memory management, pattern detection, reflection
  - **Herald** — Communication coaching, meeting prep, network management
- Agents coordinate through shared memory, overseen by the Seraph core

### 6.7 TTS / Voice
- Seraph speaks in the RPG UI (avatar voice with retro effect)
- Voice interaction option (speak to Seraph, hear responses)
- Fits the guardian angel aesthetic — a voice in your ear

### 6.8 Mobile Experience
- PWA or native app for mobile access
- Quick capture: "Seraph, remind me to..." / "Seraph, I just decided to..."
- Notification-driven interaction on mobile (full UI on desktop)
- Mobile as the "always with you" channel, desktop as the "deep work" channel

### Milestone
Seraph is a comprehensive life operating system — observing, thinking, acting, and evolving alongside its human toward their highest potential.

---

## Technical Prerequisites by Phase

| Phase | Key Technical Requirements |
|-------|---------------------------|
| **1** | Database (SQLite/Postgres), file-based memory (soul.md), goal data model, onboarding flow |
| **2** | Docker sandboxing, Playwright, OAuth for Google/Microsoft, plugin API, tool registry |
| **3** | OS-level context APIs, local vision model, pattern detection pipeline, state inference model |
| **4** | Background scheduler, reasoning engine (LLM-in-a-loop), interruption cost model, briefing templates |
| **5** | Telegram/Discord bot SDK, push notification service, delegation framework, action audit log |
| **6** | Stat computation engine, Phaser village evolution system, wearable APIs, sub-agent orchestration |

---

## Design Principles (Every Phase)

1. **Proactive over reactive** — Default to initiating, not waiting
2. **Strategic over tactical** — Ask "does this serve the highest goal?" not just "is this task done?"
3. **Earn trust incrementally** — Start quiet, prove value, then increase agency
4. **Transparent always** — Every observation, reasoning step, and action is visible and auditable
5. **Human sovereignty** — The human is always in control. Seraph advises and acts within boundaries, never overrides
6. **Privacy by architecture** — All data local by default. Raw observations discarded after processing. Nothing leaves the machine without explicit consent
7. **The RPG metaphor serves the mission** — Every game element must map to real life value. No decoration without purpose
8. **Optimize for the human, not for engagement** — Seraph should make itself less needed over time as the human grows. The goal is elevation, not dependency
