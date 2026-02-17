---
sidebar_position: 4
---

# Seraph — Next Steps Analysis
**Date: 2026-02-13**

---

## Current State

Phases 0-3 are complete and functional. 624 automated tests (500 backend, 124 frontend). Clean codebase with no TODOs/FIXMEs. The project's unique advantages — proactive intelligence, RPG village metaphor, five-pillar life model, screen awareness — are implemented but not fully leveraged in the UI.

---

## Completed (Tier 1)

1. ~~**Fix the docs sidebar**~~ — Done. All docs wired into `docs/sidebars.ts`.

2. ~~**Interruption Mode UI**~~ (3.5.2) — Done. 3-state toggle (Focus/Balanced/Active) in SettingsPanel via `InterruptionModeToggle.tsx`.

3. ~~**Goal Management UI**~~ (3.5.1) — Done. Create/edit modal (`GoalForm.tsx`), inline actions (complete/edit/delete) on GoalTree nodes, and search/filter (text search + level/domain dropdowns) in QuestPanel.

4. ~~**Frontend Accessibility**~~ (3.5.9) — Done. Font sizes bumped to 9px minimum across all panels. Keyboard shortcuts: Shift+C (chat), Shift+Q (quests), Shift+S (settings), Escape (close). Shift modifier prevents WASD avatar movement conflict.

7. ~~**SKILL.md Plugin Ecosystem**~~ (4.1) — Done. Zero-code markdown plugins in `data/skills/`. YAML frontmatter, tool gating, runtime enable/disable via API + Settings UI. 8 bundled skills.

5. ~~**Agent Execution Timeout**~~ (3.5.6) — Done. `asyncio.wait_for` timeouts on REST chat (504), daily briefing, evening review, memory consolidation LLM + add_memory calls. DDGS web search timeout via constructor. 3 new settings: `agent_briefing_timeout` (60s), `consolidation_llm_timeout` (30s), `web_search_timeout` (15s). 5 new tests.

6. ~~**Token-Aware Context Window**~~ (3.5.5) — Done. Token-aware context window with configurable budget. Keeps first N + last M messages, summarizes middle via LLM. 3 new settings: `context_window_token_budget` (12000), `context_window_keep_first` (2), `context_window_keep_recent` (20). Info logging on both return paths. 3 new settings integration tests.

## Tier 3: The growth engine (Phase 4)

8. **Telegram Bot** (4.2, first channel) — Makes proactive system (briefings, reviews, nudges) actually useful. Users won't keep a browser tab open, but they always have their phone.

## Tier 4: Bigger bets (quarter-scale)

9. **Tauri Desktop App** (3.5.7) — Eliminates Docker barrier. Single `.dmg` with bundled backend + daemon. System tray. Biggest adoption unlock but biggest effort.

10. **Avatar Ambient State Reflection** (3.5.3) — Ambient WebSocket messages already flow; Phaser doesn't react visually. Making Seraph meditate/pace/glow based on state brings the RPG metaphor to life.

## What to skip for now

- **Calendar scan** (3.5.4) — Already more complete than REPORT.md claimed. Works when credentials are configured.
- ~~**Multi-agent architecture** (4.4) — Premature until single-agent is polished.~~ → Implemented as recursive delegation behind feature flag (`USE_DELEGATION=true`).
- **Voice** (4.8) — Not a differentiator yet.
- **Workflow engine** (4.3) — Wait for SKILL.md; workflows build on skills.

## Strategic take

Seraph's biggest advantages are **proactive intelligence** and **the RPG village**. Neither is fully leveraged:
- Proactive messages only reach a browser tab → Telegram fixes this
- Village avatar doesn't reflect agent state → ambient states fix this
- ~~Users can't control interruptions → interruption mode UI fixes this~~ ✓

**Next priorities**: ~~Agent execution timeout (prevents user frustration)~~ ✓, ~~token-aware context (prevents quality degradation)~~ ✓, then Telegram bot (makes proactive intelligence useful in daily life).
