---
title: "ADR-001: Inference-Only Model Providers"
---

# ADR-001: Inference-Only Model Providers

**Status:** Accepted

**Decision class:** Target architecture

## Context

Seraph needs strong local and remote models without outsourcing its identity,
planning loop, capabilities, durable state, or execution policy to another agent
runtime.

## Decision

Seraph owns the agent runtime. Model integrations provide inference only through
local endpoints, OpenRouter, or generic OpenAI-compatible APIs. Routing records
the effective provider, endpoint class, model, fallback decision, cost/latency
metadata when available, and degraded state.

Codex CLI, Claude Code, and similar command-backed agent runtimes are not Seraph
runtime dependencies. A model available through a supported inference API may
be used; its vendor does not gain execution authority.

## Consequences

- Planning, tools, memory, approvals, retries, and receipts remain portable.
- Provider failure degrades inference routes rather than replacing Seraph.
- Existing command-backed operator paths are transitional shipped behavior and
  are removed under issue #739; this ADR does not claim that removal has shipped.
- Data-egress policy is enforced independently from model selection.

## Verification

Provider inventory, runtime status, and tests must distinguish inference routes
from capabilities and reject command-backed agent routes as canonical profiles.
