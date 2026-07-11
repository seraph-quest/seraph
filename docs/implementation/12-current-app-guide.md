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
GPU host `jupyter` (`192.168.1.26`)
  Seraph frontend/backend + canonical data
    -> private VLM wrapper
    -> private GPU model server
Mac paired edge
  capture -> authenticated HTTPS ingress on jupyter
```

The repository and Codex workspace are already local to `jupyter`; administer
the host directly rather than using an SSH hop to itself. Canonical authority
stays on this GPU core. The target Mac edge is paired and revocable: it captures
consented screenshots and pushes them through authenticated Seraph ingest for
storage and analysis. That upload API is not shipped yet and is owned by #749.
Until then, screenshot push is a visible gap, not a working capability. The Mac
does not connect directly to backend, wrapper, or model ports.
The planned authenticated deployment and its acceptance boundaries are defined
in [GPU Core LAN Operations](./19-gpu-core-lan-operations.md); it is not yet a
live-deployment receipt.

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
curl -sS http://127.0.0.1:8001/health
curl -sS http://127.0.0.1:8001/health/backend
curl -sS http://127.0.0.1:8001/queue/status
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

> **Branch-local #740 target, not shipped `develop` truth:** the model-fabric
> settings, canary, proof, receipt, and runtime-path status surfaces below remain
> intended behavior until the reviewed epic integration PR lands.

The target GPU topology has three distinct inference transports:

- `LOCAL_LLM_API_BASE=http://host.docker.internal:8000/v1` is the private local
  GPU text route used from the backend container;
- `SERAPH_VLM_BASE_URL=http://vlm-wrapper:8001` also exposes an optional
  OpenAI-compatible text chat proxy under `/v1`;
- that same wrapper performs screenshot vision through `/v1/analyze-file`.

They share the model-fabric selection, trust, proof, and receipt vocabulary but
remain separate transport adapters. `/api/settings/model-fabric` reports
configured profiles and policies. The `model_fabric` field in
`/api/runtime/status` reports
selected and attempted routes, the last actual successful route, fallback,
degradation, receipt persistence, and fresh/stale/missing capability proof.
Settings retains last-known metadata if a refresh fails and labels it `STALE`.

Capability canaries are intended to run only from an explicit authenticated operator action through
`POST /api/settings/model-fabric/canary`. Each canary targets one exact profile,
capability, endpoint/model/adapter binding with a bounded deadline and no
fallback. Status and settings reads never launch inference.

**Integration dependency:** the fabric does not synthesize operator identity.
On the #740 epic branch, REST/WebSocket chat remains fail-closed with zero model
transport when ingress has not bound an authenticated principal. Issue #741
owns binding LAN/operator identity and making that interactive path functional;
#740 supplies the governed inference route but does not weaken identity checks.

Named profiles such as `codex-openai` and `claude-anthropic` are API model-route
names only. They do not invoke Codex CLI, Claude Code, or another external agent
runtime. Seraph owns planning, capabilities, tools, approvals, and receipts.

## Screen Awareness And Reports

**Shipped foundation:** the observer can ingest permitted context, store screen
artifacts, route screenshot analysis through configured VLM paths, and feed
report infrastructure. Capture, analysis, and report synthesis are separate
stages and should expose separate failures.

**Planned:** a paired, revocable Mac edge supplies observation and native
interaction to the GPU core. Pairing must not implicitly authorize execution or
data egress.

Until that edge ships, a browser connected to a remote GPU core cannot invoke
the Mac native folder picker. Screenshot-folder selection must show that edge as
unavailable/degraded rather than presenting a GPU-host path as a Mac folder.

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

The earlier command-backed runtime direction from
[#613](https://github.com/seraph-quest/seraph/issues/613) and
[#615](https://github.com/seraph-quest/seraph/issues/615) is explicitly reversed
by Epic #736 and removal issue #739. Those tickets remain historical evidence,
not current setup guidance.

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
