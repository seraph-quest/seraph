---
slug: /current-app
title: Current App Guide
---

# Current App Guide

**Status:** Partial
**Scope:** current `develop` baseline; target changes in Epic #736 are not yet shipped

This is the short operator-facing description of the current application. For
the target product and locked decisions, read the
[Project Constitution](./00-project-constitution.md). For exhaustive shipped
detail, read [Development Status](./STATUS.md).

## Current Topology

```text
Seraph frontend       http://127.0.0.1:3001
  -> Seraph backend   http://127.0.0.1:8004
  -> GPU VLM wrapper  http://192.168.1.26:8001
  -> GPU model server http://192.168.1.26:8000/v1
```

The accepted architecture decision places the core on the GPU host and uses a
paired Mac edge. Until their
migration tickets ship, the backend and frontend remain local and GPU services
are reached over documented HTTP APIs. `ssh jupyter` is an administrator path
for inventory and maintenance, not application transport or a required tunnel.

## Run The Current App

```bash
cp env.dev.example .env.dev
./manage.sh -e dev local up
./manage.sh -e dev local status
```

Open `http://127.0.0.1:3001` for the cockpit and
`http://127.0.0.1:8004/docs` for API documentation. Stop with
`./manage.sh -e dev local down`.

In a managed development shell, use `./manage.sh -e dev local run` and keep the
foreground session open while verifying live behavior. Direct `uvicorn`, Vite,
or detached child-process launches are not the supported lifecycle path.

Useful probes:

```bash
./manage.sh -e dev local status
curl -sS http://127.0.0.1:8004/health
curl -sS http://127.0.0.1:8004/api/runtime/status
curl -sS http://127.0.0.1:8004/api/settings/artifact-storage
curl -sS http://192.168.1.26:8001/health
curl -sS http://192.168.1.26:8001/health/backend
curl -sS http://192.168.1.26:8001/queue/status
```

The wrapper's `/health` and model backend's `/health/backend` are separate
truths. A configured endpoint is not evidence that its backend is healthy.

## Current Capability Summary

**Shipped foundations:** the browser cockpit, FastAPI backend, conversation,
goals, memory and guardian state, settings, scheduler jobs, approvals, activity
and audit views, tools and workflows, artifacts, reports, extension adapters,
screen-observation storage, and VLM integration points exist on `develop`.

**Partial:** long-horizon guardian behavior, durable execution, isolation,
cross-surface identity, selected voice/messaging channels, and outcome-first UX
need Epic #736 milestones. Existing canaries or deterministic receipts do not
make those product capabilities complete.

## Models And Runtime

The target provider contract is inference through local models, OpenRouter, and
generic OpenAI-compatible systems. The UI and `/api/runtime/status` must report
the effective route, including degraded state and fallback.

**Partial/transitional:** `develop` still contains older provider-specific and
command-backed operator paths. They are implementation history scheduled for
removal under issue #739, not part of the target architecture. Do not configure
new deployments around them.

## Screen Awareness And Reports

**Shipped foundation:** the observer can ingest permitted context, store screen
artifacts, route screenshot analysis through configured VLM paths, and feed
report infrastructure. Capture, analysis, and report synthesis are separate
stages and should expose separate failures.

**Planned:** a paired, revocable Mac edge supplies observation and native
interaction to the GPU core. Pairing must not implicitly authorize execution or
data egress.

## Memory

**Shipped foundation:** Seraph owns canonical local memory and can augment
retrieval through guarded provider integrations.

The accepted memory-boundary decision requires goals, approved facts, jobs, artifacts, checkpoints,
approvals, and audit records stay canonical in Seraph-owned storage. Graph or
external memory systems may be benchmarked only as advisory providers with
provenance, conflict, deletion, export, and failure handling.

## Actions, Workflows, And Extensions

**Shipped foundation:** tools, workflows, skills, runbooks, starter packs, MCP
integrations, connectors, approvals, activity receipts, workflow history, and
extension governance exist in the current codebase.

**Partial:** breadth and deterministic receipts are not equivalent to a fully
durable or isolated runtime. The target gives every capability typed
inputs/outputs, permissions, limits, checkpoints, artifacts, and visible
receipts without depending on an external coding-agent runtime.

## One-GPU Behavior

The accepted scheduling decision requires serial GPU execution: the active job finishes, then the
highest-priority ready job runs. Interactive work outranks scheduled and
background observation work; background work uses idle capacity without
starving. Existing queues remain **Partial** until the global broker proves
mutual exclusion, priority, and non-starvation across every GPU consumer.

## Failure And Recovery

- Trust effective API/UI state, not a default provider label.
- Keep settings usable through partial metadata failure and show last-known state.
- Treat Codex/Desktop LAN failures as an environment limitation when an
  operator-shell receipt proves the route; do not redesign around an SSH tunnel.
- Never describe branch-local or target behavior as Shipped before merge and
  validation on `develop`.

## Next Reading

- [Project Constitution](./00-project-constitution.md)
- [Development Status](./STATUS.md)
- [Documentation Contract](./08-docs-contract.md)
- [ADR-004: GPU Core And Paired Mac Edge](./decisions/004-gpu-core-mac-edge-topology.md)
