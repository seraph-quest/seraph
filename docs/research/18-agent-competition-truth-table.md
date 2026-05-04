---
title: 18. Agent Competition Truth Table
---

# 18. Agent Competition Truth Table

## Purpose

This is the broad M0 competition truth table for Seraph's capability-first world-class strategy.

As of: 2026-05-04

This document covers source-backed pressure from major agent systems and frameworks. It does not make superiority claims for Seraph. It records what public official/source evidence says competitors are strong at, how confident we are in that read, and which Seraph milestones must answer the gap.

Use this with:

- [17. Seraph World-Class Strategy](./17-seraph-world-class-strategy.md)
- [19. Strategy Claim Ledger](./19-strategy-claim-ledger.md)

## Confidence Legend

| Confidence | Meaning |
| --- | --- |
| `High` | Current official docs, product docs, or primary repository evidence directly supports the row. |
| `Medium` | Official/source evidence exists, but the public surface is narrower, changing, deprecated, or incomplete for some axes. |
| `Low` | Current primary evidence is insufficient for a strong competitive claim; use only as a watch item. |

## Milestone Key

| Milestone | Strategic gap class |
| --- | --- |
| `M0` | Competition truth, claim discipline, source hygiene, and benchmark governance |
| `M1` | Capability kernel, manifests, taxonomy, and contribution contracts |
| `M2` | Execution supremacy across terminal, process, files, browser/computer use, patching, artifacts, and repair |
| `M3` | Trusted execution boundaries, isolation, credentials, approvals, prompt-injection defense, and provider trust |
| `M4` | Selective reach through native, browser, messaging, node, webhook, and external channels |
| `M5` | Jobs, routines, workflows, delegation, checkpoint, branch, resume, compare, and background ownership |
| `M6` | Memory superiority with provenance, confidence, freshness, privacy, correction, and behavior-changing recall |
| `M7` | Dense cockpit and activity ledger for inspection, approval, routing, spend, artifacts, failures, and recovery |
| `M8` | Guardian brain over the capability substrate: salience, timing, restraint, goals, feedback, and follow-through |
| `M9` | Governed ecosystem, managed connectors, package trust, versioning, compatibility, and review flows |

## Truth Table

| System | Current source-backed shape | Primary/source URLs | Confidence | Main Seraph pressure | Gap map |
| --- | --- | --- | --- | --- | --- |
| Hermes Agent | Dense operator agent with broad built-in tools, toolsets, MCP, terminal/process/files, browser automation, memory, delegation, cron/background work, messaging, skills, and defense-in-depth security controls. | [Docs](https://hermes-agent.nousresearch.com/docs/), [tools](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools/), [browser](https://hermes-agent.nousresearch.com/docs/user-guide/features/browser), [MCP](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp), [security](https://hermes-agent.nousresearch.com/docs/user-guide/security/) | `High` | Seraph must match raw capability breadth without losing guardian governance, receipts, and cockpit legibility. | `M1`, `M2`, `M3`, `M4`, `M5`, `M7`, `M9` |
| OpenClaw | Gateway-centered agent/control plane with Control UI, browser execution, tools/plugins/skills, multi-agent routing, node/device pairing, channel reach, and gateway security posture. | [Docs](https://docs.openclaw.ai/index), [Control UI](https://docs.openclaw.ai/web/control-ui), [browser](https://docs.openclaw.ai/tools/browser), [architecture](https://docs.openclaw.ai/concepts/architecture), [security](https://docs.openclaw.ai/gateway/security) | `High` | Seraph needs at least comparable control-plane legibility, channel governance, browser safety, and node/channel reach, but with guardian memory and stricter supervision. | `M1`, `M2`, `M3`, `M4`, `M5`, `M7`, `M9` |
| IronClaw | Security-first agent OS/reference with public source framing around capability permissions, sandboxing, credential boundaries, routines/jobs, extensions, and multi-surface operation. | [GitHub](https://github.com/nearai/ironclaw), [site](https://www.ironclaw.com/) | `Medium` | Treat IronClaw as the security parity gauntlet: breadth is not acceptable until isolation, permission, credential, and prompt-injection boundaries are proven. | `M2`, `M3`, `M5`, `M7`, `M9` |
| Claude Code | Agentic coding system across terminal, IDE, desktop, browser, and web/cloud surfaces; reads codebases, edits files, runs commands, uses MCP, skills, hooks, subagents, CI/GitHub/GitLab flows, and managed environments for cloud work. | [Overview](https://code.claude.com/docs/en/overview), [Anthropic docs](https://docs.anthropic.com/en/docs/claude-code/overview), [security](https://docs.anthropic.com/en/docs/claude-code/security) | `High` | Seraph is behind on polished coding-agent execution and multi-surface developer workflow unless task benchmarks prove otherwise. | `M2`, `M3`, `M5`, `M7`, `M9` |
| Codex | OpenAI coding agent with cloud delegated tasks and local CLI/IDE paths; reads, modifies, runs code, creates PRs, runs in task-scoped cloud containers, and supports approval modes locally. | [Codex cloud](https://platform.openai.com/docs/codex), [CLI help](https://help.openai.com/en/articles/11096431), [Codex repo](https://github.com/openai/codex) | `High` | Seraph needs coding-task execution, background delegation, sandbox, PR, and review benchmarks before any Codex-class claim. | `M0`, `M2`, `M3`, `M5`, `M7` |
| OpenHands | Open-source/cloud software-agent platform with SDK, CLI, local GUI/web GUI, Docker/process/remote sandbox modes, terminal, browser, app preview, VS Code/file editor, and enterprise/cloud coding-agent positioning. | [Product](https://openhands.dev/product), [docs](https://docs.openhands.dev/), [GitHub](https://github.com/OpenHands/OpenHands), [sandbox overview](https://docs.openhands.dev/usage/runtimes/overview), [runtime architecture](https://docs.openhands.dev/usage/architecture/runtime) | `High` | Seraph must match engineering-task workbench clarity, sandbox options, and transparent artifact/change review outside a coding-only niche. | `M2`, `M3`, `M5`, `M7`, `M9` |
| Goose | Open-source native agent with desktop, CLI, API, broad MCP extension ecosystem, provider choice, recipes, subagents, MCP Apps, Code Mode, and documented security controls. | [Docs/home](https://goose-docs.ai/), [GitHub](https://github.com/block/goose), [Code Mode](https://goose-docs.ai/docs/mcp/code-mode-mcp), [Apps extension](https://goose-docs.ai/docs/mcp/apps-mcp/) | `High` | Goose pressures Seraph on local/native distribution, MCP breadth, recipes, subagents, and extension UX. Seraph must win with stronger governance and guardian context. | `M1`, `M2`, `M3`, `M4`, `M7`, `M9` |
| Aider | Focused terminal AI pair programmer with repo map, multi-file edits, git diffs/commits, broad model support, and benchmark-friendly coding workflow. | [Docs](https://aider.chat/docs/), [site](https://aider.chat/), [GitHub](https://github.com/Aider-AI/aider) | `High` | Aider is a narrow but serious terminal coding baseline. Seraph should not claim coding superiority without diff, test, repo-map, and commit-quality benchmarks. | `M0`, `M2`, `M7` |
| Cline / Roo Code | Cline is an editor/terminal coding agent with file edits, terminal/browser actions, MCP, checkpoints, rules/skills/workflows, Memory Bank, subagents, and explicit approval framing. Roo Code is similar Cline-derived/editor-agent pressure, but public docs say Roo Code products sunset on 2026-05-15. | [Cline docs](https://docs.cline.bot/), [Cline overview](https://docs.cline.bot/introduction/overview), [Roo docs](https://docs.roocode.com/), [Roo GitHub](https://github.com/RooCodeInc/Roo-Code) | `High` for Cline, `Medium` for Roo | Seraph needs comparable IDE/editor-agent execution quality, approval ergonomics, MCP use, checkpoints, and task transparency. Roo should be treated as a changing/deprecating baseline. | `M0`, `M2`, `M5`, `M7`, `M9` |
| Devin | Managed autonomous software engineer for engineering tickets, bugs, features, tests, internal tools, terminal workflows, repository setup, and team backlog execution. | [Docs](https://docs.devin.ai/) | `High` | Devin pressures Seraph on long-running engineering-task endurance, managed task UX, repo setup, test/fix loops, and human handoff. | `M0`, `M2`, `M3`, `M5`, `M7` |
| Manus | General AI agent API for asynchronous tasks with projects, files, webhooks, skills, connectors, custom agents, task follow-ups, and task visibility controls. | [API overview](https://open.manus.ai/docs/quickstart), [task.create](https://open.manus.ai/docs/api-reference/create-task), [website management](https://open.manus.ai/docs/v2/website) | `High` | Manus pressures Seraph on general task delegation, connector/skill-controlled runs, task APIs, and consumer-facing task completion. | `M2`, `M4`, `M5`, `M7`, `M9` |
| Browserbase / Stagehand | Browser-agent infrastructure with cloud browser sessions, search/fetch, functions, model gateway, session inspector, Stagehand natural-language browser automation, Playwright/Puppeteer/Selenium support, and MCP server. | [Browserbase docs](https://docs.browserbase.com/), [Stagehand docs](https://docs.browserbase.com/introduction/stagehand), [Stagehand GitHub](https://github.com/browserbase/stagehand), [Browserbase MCP](https://www.browserbase.com/mcp) | `High` | Seraph is behind on production browser-agent infrastructure, session observability, replay, and browser automation reliability unless it integrates or matches these patterns. | `M2`, `M3`, `M4`, `M7`, `M9` |
| AutoGPT / Forge | Agent-platform lineage with self-hosted AutoGPT platform, block/workflow-style automations, Forge toolkit, Agent Protocol, benchmark culture, UI, CLI, and continuous agents. | [AutoGPT GitHub](https://github.com/Significant-Gravitas/AutoGPT), [Forge protocols](https://docs.agpt.co/forge/components/protocols/) | `Medium` | Use AutoGPT/Forge primarily as M0/M1/M9 pressure for agent protocols, benchmarking, agent construction, and platform history; avoid current parity claims without narrower refresh. | `M0`, `M1`, `M5`, `M9` |
| CrewAI | Multi-agent framework for agents, crews, flows, tools, memory, knowledge, guardrails, observability, human-in-the-loop triggers, and enterprise automations. | [Docs](https://docs.crewai.com/), [introduction](https://docs.crewai.com/en/introduction), [agents](https://docs.crewai.com/core-concepts/Agents/), [tools](https://docs.crewai.com/core-concepts/Tools/), [processes](https://docs.crewai.com/core-concepts/Processes) | `High` | CrewAI pressures Seraph on mature multi-agent/workflow framework shape, enterprise automation, reusable agents, flows, and governance vocabulary. | `M1`, `M5`, `M7`, `M9` |
| LangGraph | Low-level orchestration framework/runtime for long-running stateful agents with durable execution, streaming, human-in-the-loop, checkpoint persistence, memory, time travel, interrupts, replay/fork, and fault tolerance. | [Overview](https://docs.langchain.com/oss/python/langgraph/overview), [persistence](https://docs.langchain.com/oss/python/langgraph/persistence), [workflows and agents](https://docs.langchain.com/oss/python/langgraph/workflows-agents), [memory](https://docs.langchain.com/oss/python/langgraph/add-memory), [GitHub](https://github.com/langchain-ai/langgraph) | `High` | LangGraph sets the durable workflow primitive bar. Seraph needs checkpoint, branch, replay, fork, interrupt, and state inspection semantics that are productized for operators. | `M5`, `M6`, `M7`, `M9` |

## Cross-Competitor Gap Map

| Milestone | Competition truth to answer first |
| --- | --- |
| `M0` | Keep this table and the claim ledger current before any world-class, best, superior, secure, private, production-ready, or ahead claim is repeated. |
| `M1` | Hermes, Goose, CrewAI, AutoGPT/Forge, LangGraph, and OpenClaw all pressure Seraph to make capability contracts, manifests, taxonomy, and contribution boundaries explicit. |
| `M2` | Claude Code, Codex, OpenHands, Aider, Cline, Devin, Hermes, Browserbase/Stagehand, and Manus set the execution-quality bar for terminal, files, code, browser, patching, tests, and artifacts. |
| `M3` | IronClaw, OpenHands, Codex cloud, Claude Code cloud, Browserbase, Hermes, and OpenClaw force Seraph to prove isolation, permissions, credential boundaries, prompt-injection defenses, and provider trust path by path. |
| `M4` | Hermes, OpenClaw, Goose, Manus, Browserbase, and Claude Code pressure Seraph on selective native/browser/messaging/API reach without trust fragmentation. |
| `M5` | LangGraph, Devin, Claude Code, Codex, OpenHands, CrewAI, Hermes, and AutoGPT/Forge pressure Seraph on durable jobs, background work, delegation, checkpointing, resume, replay, and repair. |
| `M6` | Hermes and LangGraph set bounded/contextual memory expectations; Seraph's chance to win is behavior-changing guardian memory, not larger memory inventory. |
| `M7` | OpenClaw, OpenHands, Claude Code, Devin, Browserbase, Cline, Goose, and Hermes pressure Seraph's cockpit to expose execution, approvals, artifacts, failures, spend, and recovery faster than source diving. |
| `M8` | Few competitors are explicitly guardian-first. Seraph's differentiator must be proven by salience, restraint, intervention timing, goal continuity, and feedback-conditioned capability choice. |
| `M9` | Goose, Hermes, CrewAI, LangGraph, AutoGPT/Forge, Cline, OpenClaw, and Browserbase pressure Seraph's ecosystem to scale through governed packages, managed connectors, review, compatibility, and trust levels. |

## Claim Rules

- This file may support `Behind`, `At par`, `Unknown`, `Partially backed`, or aspirational strategy language.
- It does not by itself support `Ahead`, `Best`, `World-class`, `Secure`, `Private`, `Production-ready`, or `Superior` claims.
- Any such claim must also pass the status and allowed-wording rules in [19. Strategy Claim Ledger](./19-strategy-claim-ledger.md).
- If a competitor row depends on non-official or stale evidence, keep confidence below `High` and phrase the gap as a watch item.
