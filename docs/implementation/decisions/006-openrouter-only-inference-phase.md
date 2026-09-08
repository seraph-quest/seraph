---
title: "ADR-006: OpenRouter-only inference phase"
---

# ADR-006: OpenRouter-only inference phase

**Status:** Accepted for Epic #736 target; effective on merge to `develop`

**Decision class:** Superseding phase target

## Context

Epic #736 currently carries a GPU-hosted model server and VLM wrapper as
runtime prerequisites. That hardware dependency prevents the authenticated
Seraph core, canonical workspace, and operator cockpit from running on a
CPU-capable host when inference capacity is unavailable. The user has selected
OpenRouter as the only active inference gateway for the current product phase.

The decision changes the active provider target. It does not claim that
OpenRouter supports every modality or upstream policy without a capability
check, and it does not make the gateway the authority for goals, tools,
approvals, memory, or canonical state.

## Decision

For the Epic #736 implementation phase, all active model inference goes through
the governed OpenRouter HTTPS API. The active profile rejects local, Ollama,
direct OpenAI, Anthropic, and arbitrary OpenAI-compatible inference routes.
The server validates the fixed `https://openrouter.ai/api/v1` destination,
keeps the API key in the trusted backend transport, and applies explicit
upstream allowlisting, parameter requirements, fallback policy, data-collection
policy, consent, and finite cost budgets before dispatch.

The Seraph core and canonical data remain host-local. CPU parsing, image/audio
normalization, lexical indexing, tool sandboxes, authenticated APIs, and
operator UI do not require CUDA, downloaded model weights, a local model
server, or a VLM wrapper. OpenRouter is an inference gateway only; governed
tools and Telegram retain their own destination and credential policies.

The target contract for every remote request is a shared bounded admission
contract with authenticated owner and operation identity, priority, deadline,
cancellation, idempotency, cost reservation, retry rules, and
durable/observable outcome receipts. This migration implements the process-local
serial admission, identity, deadline, cancellation, and bounded retry seam.
Durable job persistence and provider cost reservation/reconciliation remain
explicit follow-up work in #743/#744; the current phase does not pretend those
properties are shipped. The initial policy allows one in-flight request and a
bounded queue; it does not claim control of OpenRouter's upstream concurrency.
Unknown or partial remote outcomes retain possible cost liability and are not
automatically replayed.

Vision, audio, and embeddings are capability-selected. A required capability
is blocked with a visible reason until its exact model, request shape, policy,
and live route have been verified. There is no silent local or direct-vendor
fallback. Canonical memory remains local; remote embeddings are versioned by
model, dimension, and schema before an index switch, with lexical degraded
retrieval available only when clearly labeled.

## Relationship to earlier ADRs

- This ADR narrows ADR-001's currently selectable provider families for this
  phase while retaining its inference-only boundary and Seraph-owned authority.
- It supersedes ADR-002's physical-GPU resource requirement with a bounded
  `remote_inference` resource class. Priority, cancellation, fencing, and
  reconciliation principles remain.
- It supersedes ADR-004's GPU-host inference requirement. The host may still be
  `jupyter`, but its GPU and the separate VLM repository are not product
  prerequisites. Authenticated core/edge authority boundaries remain.
- ADR-003 remains canonical-memory ownership authority.
- ADR-005 remains the epic integration-branch workflow authority.

Earlier ADRs and receipts remain historical records. Re-activating another
inference provider requires a separately reviewed target change; rollback of
an application/configuration release must not silently re-enable one.

## Consequences

- Provider outage, invalid credentials, unsupported capability, consent
  revocation, rate limiting, and budget exhaustion degrade or block inference
  while the local core remains usable.
- Screenshots and audio have separate local-capture and cloud-egress consent;
  a provider change does not upload historical backlog.
- OpenRouter upstream identity, retention, and usage are recorded as returned
  or verified. Unknown values remain unknown; the system makes no absolute
  retention or security claim.
- Existing GPU/VLM code and receipts may remain as dormant or historical
  artifacts during the migration, but active selectors, launchers, readiness
  gates, and tests cannot require or invoke them.

## Verification

The migration ticket #775 owns the versioned configuration migration, active
route guard, vision/embedding adapters, CPU-only lifecycle, and receipts.
Acceptance requires negative absence tests plus authorized text, vision, and
embedding live receipts. #751/#752 own audio capability proof. #744 owns the
shared remote admission extension after #743's durable job contract.
