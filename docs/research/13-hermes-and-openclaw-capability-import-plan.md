---
title: 13. Hermes And OpenClaw Capability Import Plan
---

# 13. Hermes And OpenClaw Capability Import Plan

## Goal

Define exactly which Hermes and OpenClaw capability surfaces Seraph should import next, which should stay out, and how each surface should land in Seraph's extension platform.

This document answers:

- what "port all from Hermes" should actually mean
- which OpenClaw capability families are worth selective import
- which surfaces belong in Seraph core runtime versus extension packages
- which new extension contribution types are still needed
- what execution waves should follow from the research

## Executive Summary

Seraph should import **all major Hermes capability families**, but not by copying Hermes' interface verbatim.

That means Seraph should reach parity on:

- Hermes' broad tool/runtime surface
- Hermes' skill growth loop and skill registry ergonomics
- Hermes' bounded memory plus session-search recall model
- Hermes' messaging-gateway reach
- Hermes' browser, MCP, cron, delegation, code-execution, and clarify surfaces
- Hermes' approval, allowlist, pairing, and sandbox controls

Seraph should import **only the highest-value OpenClaw capability families**:

- multi-channel routing and delivery breadth
- richer browser modes
- node/device/canvas companion surfaces
- automation triggers like webhooks, polls, and pub/sub
- typed workflow/tool runtimes like OpenProse / Lobster / LLM-task style surfaces
- voice wake and talk mode, when Seraph is ready for companion/device reach

Seraph should **not** copy:

- OpenClaw's unrestricted in-process plugin runtime
- provider/plugin sprawl as the primary architecture
- headless gateway-first product framing
- open public skill/plugin distribution without stronger trust and review controls

The key architectural rule is:

- imported **runtime primitives** stay core
- imported **reusable capabilities** become extension contributions
- imported **reach/integration surfaces** become connectors, channel adapters, observer sources, or future extension types

## Evidence Base

This plan is grounded in:

- official Hermes docs and homepage
- official OpenClaw docs
- Seraph's existing benchmark and ecosystem research

Primary sources:

- Hermes homepage: https://hermes-agent.nousresearch.com/
- Hermes tools: https://hermes-agent.nousresearch.com/docs/user-guide/features/tools/
- Hermes skills: https://hermes-agent.nousresearch.com/docs/user-guide/features/skills/
- Hermes memory: https://hermes-agent.nousresearch.com/docs/user-guide/features/memory/
- Hermes browser: https://hermes-agent.nousresearch.com/docs/user-guide/features/browser/
- Hermes MCP: https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp/
- Hermes security: https://hermes-agent.nousresearch.com/docs/user-guide/security/
- Hermes messaging docs: Telegram, Discord, Slack, WhatsApp under `docs/user-guide/messaging/`
- OpenClaw tools and plugins: https://docs.openclaw.ai/tools/index
- OpenClaw plugins: https://docs.openclaw.ai/tools/plugin
- OpenClaw plugin agent tools: https://docs.openclaw.ai/plugins/agent-tools
- OpenClaw skills: https://docs.openclaw.ai/skills
- OpenClaw ClawHub: https://docs.openclaw.ai/tools/clawhub
- OpenClaw browser: https://docs.openclaw.ai/tools/browser
- OpenClaw Chrome extension relay: https://docs.openclaw.ai/tools/chrome-extension
- OpenClaw control UI: https://docs.openclaw.ai/web/control-ui
- OpenClaw nodes: https://docs.openclaw.ai/nodes
- OpenClaw voice wake / talk: https://docs.openclaw.ai/voicewake and https://docs.openclaw.ai/talk
- OpenClaw security: https://docs.openclaw.ai/gateway/security

Existing Seraph background docs:

- [08. Ecosystem And Delegation](./08-ecosystem-and-delegation.md)
- [10. Competitive Benchmark](./10-competitive-benchmark.md)
- [12. Plugin System And MCP Strategy](./12-plugin-system-and-mcp-strategy.md)
- `docs/docs/architecture/competitive-agent-research.md`
- `docs/docs/development/openclaw-feature-parity.md`

## Hermes Capability Inventory

Hermes is the system Seraph should mirror most aggressively on capability breadth.

### What Hermes ships today

Official Hermes materials describe:

- a broad tool/runtime surface:
  - `web_search`, `web_extract`
  - `terminal`, `process`
  - file tools
  - Browserbase browser automation
  - vision, image generation, and text-to-speech
  - `todo`
  - `memory`
  - `session_search`
  - `schedule_cronjob`, `list_cronjobs`, `remove_cronjob`
  - `execute_code`
  - `delegate_task`
  - `clarify`
  - auto-discovered MCP tools
- toolsets as first-class capability presets
- skills as on-demand procedural memory, with agent-managed creation and patching
- a Skills Hub with install, search, update, and security-scan flows
- bounded persistent memory split between agent memory and user profile
- session-search over all past sessions
- a multi-channel messaging gateway
- browser automation through Browserbase
- MCP with per-server filtering and runtime `mcp-<server>` toolsets
- layered security:
  - allowlists
  - DM pairing
  - dangerous-command approvals
  - container backends
  - MCP env filtering
  - website blocklists
  - context-file injection scanning

### Hermes import table

| Capability family | Official Hermes evidence | Why it matters | Seraph import decision | Seraph landing |
|---|---|---|---|---|
| Toolsets + broad built-in tools | Hermes tools docs list web, terminal, file, browser, vision, image, TTS, memory, session search, cron, code execution, delegation, clarify, MCP | This is the clearest "serious operator runtime" baseline | Import all | Mixed: core-native runtime tools plus extension-packaged presets |
| Skills as procedural memory | Hermes skills docs describe on-demand `SKILL.md`, agent-managed skills, slash commands, optional skills, registry install | Strongest compounding capability surface Hermes has | Import all | Capability packs + registry/install UX |
| Bounded memory + session search | Hermes memory docs describe `MEMORY.md`, `USER.md`, `memory` tool, `session_search`, optional Honcho | Seraph already has stronger long-horizon memory, but still lacks Hermes-style fast bounded recall discipline | Import all conceptually | Core memory/search runtime, plus packaged memory policies/presets |
| Messaging gateway reach | Hermes homepage and messaging docs cover Telegram, Discord, Slack, WhatsApp with cross-platform continuation | Major parity gap today | Import all | Messaging connectors plus a separate channel-routing/adapter layer |
| Browserbase browser automation | Hermes browser docs describe cloud Browserbase sessions, snapshots, ref-based actions, vision, session isolation | Strong high-leverage browsing lane | Import all | Managed browser connector + browser provider contribution |
| MCP as first-class tool ingress | Hermes MCP docs show stdio + HTTP servers, per-server filtering, runtime toolsets | Aligns directly with Seraph's connector model | Import all | Connector packages + MCP lifecycle |
| Dangerous command approvals + pairing + allowlists | Hermes security docs are unusually explicit | Good trust/runtime baseline for real-world use | Import all | Core policy/approval/auth runtime |
| `execute_code` | Hermes code execution docs show RPC sandbox for multi-step tool pipelines | High-value capability multiplier and major token-efficiency win | Import all | Core-native runtime tool |
| `delegate_task` | Hermes tools and tips docs call out parallel subagents with isolated context | Strong parity target for decomposition | Import all | Core-native delegation runtime |
| `clarify` | Hermes tools docs include structured user clarification | High-quality interaction primitive for guarded execution | Import all | Core-native runtime tool |
| `todo` | Hermes tools docs include first-class task lists | Strong planning scaffold | Import all | Core-native runtime tool with possible package templates |
| cron job tools | Hermes tools docs include scheduled tasks and messaging delivery | Important automation parity surface | Import all | Core scheduler engine + extension-packaged trigger/delivery templates |

## What "Port All From Hermes" Means

For Seraph, "port all from Hermes" should mean:

1. **Port every major Hermes capability family**, not every implementation detail.
2. **Keep Seraph's guardian product shape** instead of copying Hermes' TUI or file layout literally.
3. **Separate core runtime imports from extension imports**.

So the actual import target is:

### Hermes surfaces that should become Seraph core runtime

- terminal/process improvements
- `execute_code`
- `delegate_task`
- `clarify`
- `todo`
- `session_search`
- bounded fast memory/profile layer
- command approval, pairing, allowlists, site blocklists, context scanning
- core cron/scheduling runtime

These are agent runtime primitives, not extension packages.

### Hermes surfaces that should become Seraph extension-backed capability

- skill packs and optional skill packs
- workflow/runbook packs that mirror Hermes-style repeatable procedures
- toolset presets
- MCP connector packages
- browser provider/bridge packages
- messaging channel connectors
- voice/speech packs
- capability registries and install/update/remove flows

These are packageable and should ride the extension platform directly.

## OpenClaw Capability Inventory

OpenClaw is broader than Hermes, but much less clean as a direct model for Seraph.

The right move is selective import.

### What OpenClaw ships today

Official OpenClaw materials describe:

- built-in tools for:
  - `exec`, `process`
  - `browser`
  - `web_search`, `web_fetch`
  - file I/O
  - `apply_patch`
  - `message`
  - `canvas`
  - `nodes`
  - `cron`, `gateway`
  - `image`, `image_generate`
  - `sessions_*`, `agents_list`
- tool profiles and tool groups
- plugin-provided typed workflow/runtime tools; the official tools docs name Lobster, OpenProse, LLM Task, and Diffs as examples
- a plugin system that can register tools, channels, providers, speech, image, and more
- workspace/shared/plugin-shipped skills, hot-reload, and ClawHub registry flows
- control UI surfaces for:
  - chat
  - tool event cards
  - channels
  - sessions
  - cron
  - skills
  - nodes
  - exec approvals
  - config
  - health/debug/logs
- browser modes:
  - isolated OpenClaw-managed browser
  - Chrome extension relay to existing tabs
  - remote CDP control
- node/device companion surfaces
- talk mode and voice wake
- strong operational docs for channel routing, auth, sandboxing, browser risks, and per-agent tool controls

### OpenClaw import table

| Capability family | Official OpenClaw evidence | Value to Seraph | Import? | Seraph landing |
|---|---|---|---|---|
| Broad tool inventory | OpenClaw tools docs list exec, process, browser, message, canvas, nodes, cron, sessions, image tools | Confirms Seraph still needs more runtime breadth | Selective | Mixed core runtime and connector packages |
| Tool profiles + groups | OpenClaw tools docs define `full`, `coding`, `messaging`, `minimal`, plus groups | Very high leverage for operator control and policy | Yes | New `toolset_presets` contribution type + core policy integration |
| Browser modes | OpenClaw browser docs define isolated managed browser, extension relay, and remote CDP | High-value import | Yes | Browser provider / browser bridge contributions |
| Channels + routing + bindings | Control UI + channels docs show many channel integrations and per-channel config | Very high-value import | Yes | Messaging connectors plus a separate channel-routing/adapter layer |
| Nodes / canvas / device surfaces | OpenClaw nodes docs expose command surfaces from paired devices | Valuable for Seraph reach and embodied presence | Yes | New `node_adapters` contribution type |
| OpenProse / Lobster / LLM Task style runtimes | OpenClaw tools docs describe typed plugin-provided workflow/runtime tools and name OpenProse, Lobster, and LLM Task as examples | Valuable, but needs reinterpretation | Yes, selectively | Core workflow/runtime improvements plus extension-packaged workflow engines |
| Skills watcher + ClawHub | OpenClaw skills docs show watch/reload and ClawHub registry/versioning | Useful, but trust-heavy as-is | Yes, selectively | Capability registry with stronger scanning/trust controls |
| Voice wake / talk mode | OpenClaw voice docs show wake-word and continuous talk flows | Valuable later for companion reach | Yes, later | Voice/speech packs + node/channel adapters |
| Native/plugin provider sprawl | OpenClaw plugin docs show many provider plugins | Low-value to copy directly | No | Keep Seraph provider routing core-owned |
| Unrestricted native plugin runtime | OpenClaw plugin docs allow native plugin packages to register many capabilities in-process | Too trust-heavy | No | Do not copy |

## What Seraph Should Not Copy From OpenClaw

Seraph should explicitly reject these OpenClaw patterns:

- native in-process plugin runtime as the default ecosystem model
- packaging model where providers, channels, tools, and runtime services can all arrive as equally trusted community code
- gateway-first product framing where the main surface is "whatever chat app the user already uses"
- default-open public registry behavior for executable extensions

These make OpenClaw broad, but they also push too much trust and review burden onto the operator.

## Core Runtime Versus Extension Platform

The import plan only works if Seraph draws the boundary cleanly.

### Keep core-owned

These must remain Seraph core runtime features even if competitor inspiration comes from Hermes or OpenClaw:

- terminal/process execution
- `execute_code`
- `delegate_task`
- `clarify`
- `todo`
- `session_search`
- memory/profile state
- approvals
- tool policy
- audit/activity
- routing
- scheduler engine
- browser safety policy
- channel authorization and delivery policy

### Package as extensions

These should be extension contributions:

- skill packs
- workflow packs
- runbook packs
- starter packs
- MCP connectors
- managed SaaS connectors
- messaging channel connectors
- browser providers / browser bridges
- observer source packages
- channel routing / delivery adapter packages
- speech/voice packs
- toolset preset packs
- automation trigger packs
- context/persona packs

## New Extension Contribution Types To Add

The current extension architecture is strong, but the Hermes/OpenClaw import plan still needs a few **additional or more specialized** typed contributions.

These do **not** replace the canonical extension model from [12. Plugin System And MCP Strategy](./12-plugin-system-and-mcp-strategy.md). They refine it:

- `toolset_presets` extends the existing `presets` idea into a first-class operator/runtime control surface
- `automation_triggers` extends scheduled routines/jobs into installable trigger packages
- `browser_providers` is a specialization of managed connectors
- `node_adapters` is a specialization of channel/observer/device connector work
- `speech_profiles` is a specialization of managed connectors plus delivery/channel packages
- `context_packs` is a cleaner shape for prompt/persona bundles that would otherwise be awkwardly hidden inside starter packs or skills

### 1. `toolset_presets`

Needed for:

- Hermes-style toolset selection
- OpenClaw-style tool profiles and tool groups
- per-channel / per-agent / per-workflow tool policy presets

### 2. `automation_triggers`

Needed for:

- cron-backed jobs
- webhooks
- pollers
- pub/sub sources
- auth monitors and standing orders

These should not replace scheduled routines/jobs. They extend that idea with first-class lifecycle and health for installable trigger sources.

### 3. `browser_providers`

Needed for:

- Browserbase-like remote browser providers
- managed local browser lanes
- Chrome extension relay style bridges
- remote CDP profiles

### 4. `node_adapters`

Needed for:

- paired companion devices
- canvas/device/camera/notification surfaces
- richer embodied reach

### 5. `speech_profiles`

Needed for:

- TTS/STT providers
- talk mode
- wake-word flows
- delivery voice presets

### 6. `context_packs`

Needed for:

- SOUL / persona style packs
- context templates
- domain-specific instruction bundles that should not pretend to be skills or workflows

## Import Waves

## Wave 1: Hermes Runtime Parity

Highest-value immediate imports:

1. `execute_code`
2. `delegate_task`
3. `clarify`
4. `todo`
5. `session_search`
6. `toolset_presets`
7. stronger site blocklists / command approvals / pairing flows

Reason:

- this gives Seraph the biggest capability jump without waiting on new channels or companion apps
- it also maps cleanly to Seraph core + extension architecture

## Wave 2: Hermes Reach And Packaging Parity

Next imports:

1. Telegram connector
2. Discord connector
3. Slack connector
4. WhatsApp connector
5. Browser provider / Browserbase-style remote browsing
6. official optional skill packs + registry/install/update flows
7. cron-backed user automation packages

Reason:

- this closes the biggest practical gap between Seraph and Hermes: "persistent agent wherever the user is"

## Wave 3: OpenClaw High-Value Selective Imports

Most valuable OpenClaw imports after Hermes parity:

1. browser mode matrix:
   - isolated managed browser
   - extension relay
   - remote CDP
2. channel routing/bindings
3. automation triggers:
   - webhooks
   - polls
   - pub/sub
4. node/canvas/device adapters
5. OpenProse / Lobster / LLM-task style workflow/runtime contributions

Reason:

- these are the most strategic OpenClaw capability surfaces
- they amplify Seraph's guardian product instead of dragging it toward a generic gateway clone

## Wave 4: Voice And Embodied Reach

Later imports:

1. talk mode
2. wake-word flows
3. voice delivery policies
4. companion-device orchestration

Reason:

- very valuable, but higher product complexity
- should follow channel, policy, and node groundwork

## Recommended Product Rule

Use this rule when deciding whether a competitor capability should become an extension contribution:

- if it is a **reusable packaged capability**, make it an extension
- if it is a **runtime primitive or trust boundary**, keep it core
- if it is a **connector or reach surface**, make it a typed connector contribution
- if it requires **arbitrary third-party code with wide host access**, do not make it a default extension model

## Recommended Execution Order

If Seraph wants the highest capability gain first, the order should be:

1. Hermes core runtime tools
2. Hermes skill/registry ergonomics
3. Hermes messaging connectors
4. Hermes browser and cron parity
5. OpenClaw browser modes
6. OpenClaw routing and automation triggers
7. OpenClaw nodes/canvas/device adapters
8. OpenClaw voice surfaces

## Roadmap Translation

This research should seed future Workstream 07 candidate slices in this order, while active execution stays in GitHub:

1. Hermes runtime parity:
   - `execute-code-and-clarify-v1`
   - `todo-and-session-search-v1`
   - `toolset-presets-v1`
2. Hermes reach parity:
   - `messaging-connectors-v1`
   - `browser-provider-bridges-v1`
   - `automation-trigger-packs-v1`
3. OpenClaw selective imports:
   - `browser-mode-matrix-v1`
   - `channel-routing-and-bindings-v1`
   - `node-and-canvas-adapters-v1`
   - `workflow-runtime-imports-v1`
4. Longer-horizon embodied reach:
   - `speech-profiles-and-talk-mode-v1`
   - `voice-wake-and-companion-delivery-v1`

The roadmap should treat these as the capability-import layer that follows the extension-platform transition, not as a separate competing program.

## Bottom Line

The right import strategy is:

- **all major capability families from Hermes**
- **selected high-value capability families from OpenClaw**
- **none of OpenClaw's unrestricted plugin trust model**

Seraph should become:

- as broad as Hermes on agent capability
- as strong as OpenClaw on reach where that actually matters
- safer and more coherent than either because the extension platform stays typed and guardian-owned
