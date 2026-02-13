---
sidebar_position: 5
---

# Phase 4 — The Network

**Goal**: Seraph reaches beyond the village — multi-channel presence, composable skills, voice, multi-agent collaboration, and agent-rendered visualizations.

**Status**: Planned

**Inspiration**: Capabilities identified from OpenClaw analysis that are not covered by Phases 1-3.

---

## 4.1 SKILL.md Plugin Ecosystem

Phase 2's `@tool` auto-discovery requires Python code. OpenClaw's SKILL.md approach is text-based — skills are markdown files that teach the LLM how to use tools, requiring zero compiled code.

**Files**:
```
backend/src/skills/
  __init__.py
  loader.py              # Scan workspace/skills/ for SKILL.md files
  registry.py            # Merge SKILL.md skills with @tool plugins
  validator.py           # Validate frontmatter, check requirements
```

**SKILL.md format**:
```markdown
---
name: daily-standup
description: Generate a standup report from git, calendar, and goals
requires:
  tools: [shell_execute, get_calendar_events, get_goals]
user-invocable: true
---

# Daily Standup Generator

When the user asks for a standup report:
1. Use `shell_execute` to run `git log --oneline --since=yesterday`
2. Use `get_calendar_events(days_ahead=1)` for today's meetings
3. Use `get_goals(level="daily")` for today's tasks
4. Format as: Yesterday / Today / Blockers
```

**Loading precedence**:
1. Workspace skills (`data/skills/`) — highest priority, user-created
2. Managed skills (`~/.seraph/skills/`) — installed from registry
3. Bundled skills (shipped with Seraph) — defaults

**Skill gating**:
- `requires.tools` — only load if listed tools are available
- `requires.env` — only load if env vars are set
- `os` — platform restrictions (darwin/linux)

**Skill creation tool**:
- Agent can create new skills on the fly via `write_file` to `data/skills/`
- Hot reload: file watcher detects new SKILL.md files mid-session

**Village element**: Library building — Seraph walks there when loading/creating skills

---

## 4.2 Multi-Channel Messaging

Seraph currently only lives at `localhost:3000`. Multi-channel support lets Seraph reach users on their phone, desktop chat apps, and team platforms.

**Files**:
```
backend/src/channels/
  __init__.py
  manager.py             # Channel registry, message routing
  base.py                # Abstract BaseChannel class
  telegram.py            # Telegram bot via python-telegram-bot
  discord.py             # Discord bot via discord.py
  whatsapp.py            # WhatsApp via baileys bridge
  slack.py               # Slack bot via slack-bolt
```

**Architecture**:
- Each channel is a `BaseChannel` subclass with `send()`, `receive()`, `setup()`, `teardown()`
- Channel manager routes inbound messages to the agent, outbound responses back to the channel
- All channels share the same session/memory system — conversations are unified
- Channel-specific formatting (Telegram markdown, Discord embeds, Slack blocks)

**Priority order** (implement incrementally):
1. **Telegram** — simplest API, best bot support, free, python-telegram-bot is mature
2. **Discord** — strong community use case, discord.py is mature
3. **Slack** — team/work use case, slack-bolt is official
4. **WhatsApp** — highest reach, but requires bridge (baileys)

**Routing rules**:
- Each channel maps to a session (channel + chat_id = session_id)
- Group chats: mention-gated (only respond when @mentioned)
- DMs: always respond
- Proactive messages (Phase 3) delivered to user's preferred channel

**Settings**:
- `TELEGRAM_BOT_TOKEN`, `DISCORD_BOT_TOKEN`, `SLACK_BOT_TOKEN` in `.env`
- Per-channel enable/disable in settings
- Preferred notification channel for proactive messages

**Village element**: Carrier pigeon / messenger bird near the village gate

---

## 4.3 Workflow / Pipeline Engine

Reusable, composable tool chains that the agent can invoke in a single step. Inspired by OpenClaw's Lobster shell.

**Files**:
```
backend/src/workflows/
  __init__.py
  engine.py              # Pipeline executor
  models.py              # Workflow, Step, ApprovalGate DB models
  parser.py              # YAML workflow definition parser
  repository.py          # CRUD for saved workflows
```

**Workflow definition** (YAML):
```yaml
name: morning-routine
description: Daily morning check-in workflow
steps:
  - id: calendar
    tool: get_calendar_events
    args: { days_ahead: 1 }

  - id: goals
    tool: get_goals
    args: { level: daily }

  - id: git
    tool: shell_execute
    args: { code: "git log --oneline --since=yesterday" }
    condition: "$calendar.success"

  - id: briefing
    tool: fill_template
    args:
      template: "morning-briefing"
      context:
        events: "$calendar.output"
        goals: "$goals.output"
        commits: "$git.output"

  - id: approve
    type: approval_gate
    message: "Send this briefing?"
```

**Features**:
- **Step chaining**: Output of one step available as `$stepId.output` in subsequent steps
- **Conditional execution**: `condition` field references prior step results
- **Approval gates**: Human-in-the-loop checkpoints before sensitive actions
- **Error handling**: `on_error: skip | abort | retry`
- **Saved workflows**: Stored in DB, invocable by name
- **Agent-created workflows**: Agent can define and save new workflows

**Agent tools**:
- `run_workflow(name)` — Execute a saved workflow
- `create_workflow(name, definition)` — Save a new workflow
- `list_workflows()` — List available workflows

**Integration with Phase 3**: Scheduled jobs can trigger workflows (e.g., morning-routine at 8:00 AM)

**Village element**: Workshop building — Seraph assembles workflows at the workbench

---

## 4.4 Multi-Agent Architecture

Multiple specialized agents that can collaborate, each with isolated context and tools.

**Files**:
```
backend/src/agents/
  __init__.py
  manager.py             # Agent registry, lifecycle management
  router.py              # Route messages to appropriate agent
  models.py              # Agent configuration DB model
  communication.py       # Inter-agent messaging (send, spawn, history)
```

**Agent types** (built-in):
- **Seraph** (default) — Full agent with all tools, soul, memory, quest system
- **Researcher** — Read-only agent (web_search, browse_webpage, read_file only). For untrusted content summarization.
- **Coder** — Shell + filesystem tools, no email/calendar. For code tasks.
- **Planner** — Goals + soul + memory tools only. For strategic thinking.

**Isolation**:
- Each agent has its own system prompt, tool set, and session history
- Agents can have different LLM models (cheap model for researcher, strong for planner)
- Per-agent tool allow/deny lists

**Inter-agent communication tools**:
- `agent_send(agent_id, message)` — Send a message to another agent, get response
- `agent_spawn(agent_type, task)` — Spawn a sub-agent for a specific task
- `agent_list()` — List available agents and their capabilities

**Routing**:
- Default: all messages go to Seraph
- Seraph can delegate to sub-agents: "Let me have my researcher look into that..."
- Channel-based routing: Telegram → Seraph, Discord #code → Coder

**Village element**: NPCs in the village represent sub-agents visually

---

## 4.5 Multi-Model Support & Failover

Explicit model selection per task type with automatic failover.

**Files**:
```
backend/src/models/
  profiles.py            # Model profile definitions
  selector.py            # Task-based model selection
  failover.py            # Retry with fallback model on failure
```

**Model profiles**:
```python
MODEL_PROFILES = {
    "main":     {"model": "anthropic/claude-opus-4", "fallback": "anthropic/claude-sonnet-4"},
    "cheap":    {"model": "anthropic/claude-haiku-4", "fallback": "deepseek/deepseek-chat"},
    "coding":   {"model": "anthropic/claude-sonnet-4", "fallback": "anthropic/claude-haiku-4"},
    "fast":     {"model": "anthropic/claude-haiku-4", "fallback": None},
}
```

**Task-based selection**:
- Chat responses → `main` profile
- Memory consolidation → `cheap` profile
- Strategist ticks (Phase 3) → `cheap` profile
- Morning briefing → `main` profile
- Code generation → `coding` profile

**Failover**:
- On model error (429, 500, timeout): retry with fallback model
- Configurable retry count and backoff
- Logging of failover events for cost tracking

**Per-agent model override**: Each agent (4.4) can specify its own model profile

**Settings**: Model profiles configurable via `.env` or settings API

---

## 4.6 Enhanced Sandboxing

Move beyond snekbox to granular tool-level permissions and Docker-based isolation.

**Files**:
```
backend/src/security/
  __init__.py
  permissions.py         # Tool allow/deny policies
  sandbox.py             # Docker sandbox manager
  audit.py               # Security audit logging
```

**Tool permissions**:
- Allow/deny policies per agent, per channel, per session
- Deny always wins over allow
- Built-in profiles: `minimal` (read-only), `standard` (no shell/email), `full` (unrestricted)
- Sensitive tools require confirmation: `shell_execute`, `write_file`

**Docker sandbox improvements**:
- Replace snekbox with configurable Docker sandbox:
  - Network isolation (default: no egress)
  - Capability dropping (`--cap-drop ALL`)
  - Memory/CPU limits
  - Read-only root filesystem
  - Per-session or per-agent container scope
- Sandbox profiles: `minimal`, `coding` (with runtime), `browser` (with Chromium)

**Audit logging**:
- Log all tool invocations with args, results, timestamps
- Flag suspicious patterns (rapid file writes, network access attempts)
- `GET /api/security/audit` — review recent tool usage
- Redaction of sensitive values in logs (API keys, passwords)

**DM access control** (for multi-channel, 4.2):
- **Pairing**: Unknown senders receive a one-time code
- **Allowlist**: Only pre-approved users
- **Open**: Anyone can interact (with rate limiting)

---

## 4.7 Webhook & Event Triggers

Phase 3's scheduler polls on intervals. Webhooks enable instant, event-driven agent activation.

**Files**:
```
backend/src/webhooks/
  __init__.py
  manager.py             # Webhook registration and dispatch
  handlers.py            # Built-in webhook handlers
  models.py              # Webhook configuration DB model
```

**API endpoints**:
- `POST /api/webhooks` — Register a new webhook
- `GET /api/webhooks` — List registered webhooks
- `DELETE /api/webhooks/{id}` — Remove a webhook
- `POST /api/webhooks/incoming/{id}` — Generic webhook receiver

**Built-in webhook handlers**:
- **GitHub**: Push, PR, issue events → agent summarizes changes, reviews code
- **Gmail pub/sub**: New email notification → agent processes immediately (vs. polling)
- **Calendar push**: Event created/updated → update context in real-time
- **Generic**: User-defined webhooks with custom payload parsing

**Agent tools**:
- `create_webhook(name, event_type, action)` — Agent can set up its own triggers
- `list_webhooks()` — View active webhooks

**Integration with Phase 3**: Webhooks can feed into the strategist engine as high-priority context changes, bypassing the 15-min poll interval.

**Village element**: Carrier pigeon arrives at village gate when webhook fires

---

## 4.8 Voice Interface

Voice input/output for hands-free interaction with Seraph.

**Files**:
```
frontend/src/hooks/useVoice.ts           # Web Speech API wrapper
frontend/src/components/VoiceButton.tsx   # Push-to-talk button
backend/src/voice/
  __init__.py
  tts.py                 # Text-to-speech via ElevenLabs or local
  stt.py                 # Speech-to-text via Whisper or Web Speech API
```

**Phase A — Browser-based (zero cost)**:
- Web Speech API for speech-to-text (Chrome/Edge built-in)
- Push-to-talk button in HUD or continuous listening toggle
- Text responses displayed as normal (no TTS yet)
- Transcribed speech sent as regular chat messages

**Phase B — Full voice (requires API key)**:
- **TTS**: ElevenLabs API for Seraph's voice (RPG narrator style)
- **STT**: OpenAI Whisper API for higher accuracy (or local whisper.cpp)
- Voice responses play as audio with speech bubble animation
- Wake word detection (optional, local via Porcupine or similar)

**Village integration**:
- Speech bubble appears over Seraph when speaking
- Seraph's mouth animation synced to TTS playback
- Voice activity indicator in HUD

**Settings**:
- Voice on/off toggle
- TTS provider selection (ElevenLabs, browser, off)
- STT provider selection (Web Speech API, Whisper, off)
- Voice ID / style selection

---

## 4.9 Agent-Rendered Canvas (A2UI)

Seraph can push interactive visual content into the UI — charts, diagrams, timelines, data tables.

**Files**:
```
frontend/src/components/canvas/
  CanvasPanel.tsx          # Overlay panel for agent-rendered content
  ChartRenderer.tsx        # Render chart specifications
  TableRenderer.tsx        # Render data tables
  TimelineRenderer.tsx     # Render goal/project timelines
  MermaidRenderer.tsx      # Render Mermaid diagrams
backend/src/tools/canvas_tool.py
```

**Agent tools**:
- `render_chart(type, data, title)` — Push a chart (bar, line, pie, progress)
- `render_timeline(items)` — Push a timeline visualization
- `render_table(headers, rows)` — Push a data table
- `render_diagram(mermaid_code)` — Push a Mermaid diagram

**WS protocol**:
```json
{
  "type": "canvas",
  "canvas_type": "chart",
  "spec": { "type": "bar", "data": [...], "title": "Q1 Goal Progress" }
}
```

**Frontend rendering**:
- Lightweight chart library (e.g., Chart.js or Recharts) styled with retro/pixel theme
- Mermaid.js for diagrams
- Canvas panel opens as overlay (like quest panel) when agent pushes content
- Content persisted in session — scroll back to see past visualizations

**Use cases**:
- Quest progress over time (bar chart)
- Goal dependency graph (Mermaid diagram)
- Weekly habit tracker (table)
- Project timeline (Gantt-style)
- Email/calendar analytics

**Village element**: Notice board building in village for visualizations

---

## 4.10 Companion Apps & Remote Access

Access Seraph from outside the local network. Companion apps for mobile.

**Files**:
```
backend/src/remote/
  __init__.py
  tunnel.py              # Tailscale/Cloudflare tunnel integration
  auth.py                # Token-based authentication for remote access
```

**Remote access options**:
- **Tailscale Serve**: Expose Gateway to tailnet (private, authenticated)
- **Cloudflare Tunnel**: Public HTTPS endpoint with auth
- **SSH tunnel**: Manual `ssh -L` for simple setups

**Authentication** (required for remote):
- Bearer token authentication for API/WS endpoints
- Token generated on first setup, stored in config
- Rate limiting for remote connections

**Mobile access** (progressive):
1. **PWA**: Make frontend installable as Progressive Web App (add manifest.json, service worker) — works on any device
2. **Telegram/Discord bot** (from 4.2): Already accessible on mobile via messaging apps
3. **Native companion app** (future): React Native or Flutter app with push notifications, voice, camera

**Village element**: Village gate opens/closes based on remote connection status

---

## Implementation Order

1. **SKILL.md ecosystem** (4.1) — low effort, high leverage, multiplies all other features
2. **Multi-model failover** (4.5) — quick win, improves reliability immediately
3. **Webhook triggers** (4.7) — event-driven complements Phase 3's scheduler
4. **Enhanced sandboxing** (4.6) — prerequisite for multi-channel and remote access
5. **Telegram channel** (4.2, first channel) — Seraph escapes the browser
6. **Workflow engine** (4.3) — composable automations, builds on skills
7. **Voice interface Phase A** (4.8) — browser Speech API, zero cost
8. **Agent-rendered canvas** (4.9) — visual leverage of quest system
9. **Multi-agent architecture** (4.4) — specialized delegation
10. **Remote access + PWA** (4.10) — Seraph goes mobile

---

## Verification Checklist

- [ ] Create a SKILL.md file in `data/skills/`, verify agent can use it
- [ ] Model failover triggers when primary model returns error
- [ ] GitHub webhook triggers agent notification
- [ ] Tool permissions block shell_execute for restricted agent
- [ ] MCP servers configurable via `mcp-servers.json`, Settings UI, and `mcp.sh` CLI
- [ ] Telegram bot responds to messages with full agent capabilities
- [ ] Workflow with 3+ steps executes end-to-end with step chaining
- [ ] Voice transcription sends text to agent, response appears in chat
- [ ] `render_chart` displays a styled chart in canvas panel
- [ ] Sub-agent spawned by Seraph completes a research task
- [ ] PWA installable on mobile, connects to remote backend
- [ ] All new tools register and appear in `GET /api/tools`
- [ ] TypeScript compiles clean

---

## Dependencies

### Backend
- `python-telegram-bot>=21.0` — Telegram channel
- `discord.py>=2.4` — Discord channel
- `slack-bolt>=1.20` — Slack channel
- `pyyaml>=6.0` — Workflow YAML parsing
- `docker>=7.0` — Enhanced sandbox management

### Frontend
- `chart.js>=4.0` or `recharts>=2.0` — Canvas chart rendering
- `mermaid>=11.0` — Diagram rendering
- Service worker + manifest.json — PWA support

### External APIs (optional)
- ElevenLabs API — TTS voice synthesis
- OpenAI Whisper API — High-accuracy STT
- Tailscale — Remote access tunnel
