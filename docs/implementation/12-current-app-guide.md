---
slug: /current-app
title: Current App Guide
---

# Current App Guide

This is the short reader-facing guide to Seraph as it exists in the current
`v2026.4.11` era. For exhaustive shipped-state detail, use
[Development Status](./STATUS.md). For target shape and comparative evidence,
use the [research tree](/research).

Seraph is a cockpit-first AI guardian workspace. It remembers, watches, and
acts through a browser cockpit, a FastAPI runtime, a local observer daemon,
workflow and tool execution, approval gates, audit trails, memory, settings,
and governed extension surfaces. The old village and editor direction is
archive-only and is not the active app contract.

## First Run

Use the managed local lifecycle script for local development:

```bash
./manage.sh -e dev local up
```

The managed dev stack starts the local backend and frontend, prints their
ports, and keeps local paths consistent with the app settings. Use
`./manage.sh -e dev local status` to check backend, frontend, and daemon state.
Use `./manage.sh -e dev local down` before restarting the stack.

The browser cockpit normally runs on `http://127.0.0.1:3001`, and the backend
API normally runs on `http://127.0.0.1:8004` in the managed dev environment.

## The Cockpit

The cockpit is the primary interface. It is not a landing page and not a
marketing shell. It is an operator workspace for active work:

- conversation and live response state
- guardian state, restraint reasons, and user-model evidence
- workflow runs, branch families, recovery controls, and artifact handoff
- approvals, audit activity, and the Activity Ledger
- capability discovery, starter packs, runbooks, and extension governance
- continuity, reach health, native notification state, and desktop presence
- settings for runtime policy, daemon/screen analysis, artifact storage, and
  connector or MCP posture

The cockpit is dense by design. It should tell the operator what Seraph is
doing, why it is doing it, what is blocked, what can be recovered, and which
actions require approval.

## Actions And Workflows

Seraph can route work through native tools, workflows, skills, MCP surfaces,
starter packs, runbooks, and managed extension contributions. Important
execution paths are approval-aware and audit-visible.

Current workflow surfaces include run history, step records, checkpoint truth,
branch/resume controls, artifact lineage, source-run follow-through, recovery
drafts, and workflow-family comparison. These controls are intended to make
longer work inspectable and recoverable, not to claim crash-proof production
workflow orchestration.

## Runtime And Providers

Seraph uses a provider-neutral runtime layer for remote LLM/provider routing
and a separate local Codex operator adapter for command-backed local Codex
execution. Provider profiles decide who thinks. Tool policy, MCP policy,
approval policy, and execution boundaries decide what the agent may do.

Supported configuration families include OpenRouter, OpenAI-compatible routes,
remote OpenAI API routes, Anthropic/Claude-oriented routes, local Ollama, and
the separate `codex-local` command-backed operator path. Missing credentials
or missing local commands should fail closed with operator-visible state.

## Screen Awareness And Reports

The local observer daemon can capture screen context and route screen analysis
through local Apple Vision, local Codex CLI parsing, or explicitly configured
cloud OCR. Screen capture and analysis artifacts can be preserved locally when
enabled in settings, so future better models can re-analyze the same evidence.

End-of-day report infrastructure is part of the local guardian direction:
screen-derived summaries, goals, activity, and analysis records can be stored
locally and used for configured report delivery. Email delivery remains a
configuration-sensitive integration surface; local previews and stored reports
are the safer default while mail settings are being validated.

## Memory And Guardian State

Seraph has canonical local memory plus guarded external memory-provider
augmentation. External providers can add evidence and recall, but they do not
replace canonical guardian memory. Stale, conflicting, privacy-limited, or
low-confidence provider evidence should be visible as such.

Guardian state exposes intent, confidence, restraint, judgment risks, user-model
evidence, and next-step guidance. The goal is useful restraint and grounded
follow-through, not unbounded autonomy.

## Presence And Reach

Seraph currently centers on the browser cockpit plus a macOS observer/desktop
presence path. The app surfaces browser WebSocket state, daemon state, screen
analysis settings, native notification continuity, route health, and degraded
reach reasons.

Broader mobile, messaging, always-available reach, voice, and media claims stay
bounded. Some receipts and canaries exist, but the public docs should not
claim OpenClaw-class reach, full voice/media parity, always-available mobile
operation, or production-ready broad channel coverage unless the claim ledger
permits exact wording.

## Extensions And Source Adapters

Seraph's extension platform packages skills, workflows, runbooks, starter
packs, MCP definitions, browser providers, messaging connectors, observer
sources, channel adapters, node adapters, canvas outputs, and workflow runtimes
under governed manifests.

The current app includes extension lifecycle APIs, an extension studio,
catalog/marketplace flow composition, diagnostics, compatibility metadata,
rollback/quarantine/re-entry concepts, and source-adapter contracts for
provider-neutral evidence and bounded authenticated source actions.

## Proof And Boundaries

Seraph has many deterministic benchmark, proof, and receipt surfaces, but those
are claim boundaries, not blanket product claims. The public docs should keep
these distinctions clear:

- bounded proof is not production readiness
- provider configurability is not provider parity
- deterministic or recorded-live receipts are not broad outcome superiority
- local Codex command execution is not an OpenAI API dependency
- full parity, security superiority, solved operator control, safe autonomous
  computer use, and broad reference-system exceedance remain blocked unless
  the claim ledger permits exact wording

## Where Truth Lives

- `docs/implementation/` is shipped-state and delivery truth.
- `docs/research/` is product thesis, target shape, and evidence logic.
- GitHub issues, Project items, and PRs are the live execution layer.
- `/legacy` is historical archive material and may contradict the current app.

