---
sidebar_position: 1
title: Workstream 01 - Trust Boundaries
---

# Workstream 01: Trust Boundaries

## Status On `develop`

- [ ] Workstream 01 is only partially shipped on `develop`.

## Goal

Make Seraph safe enough and governable enough to justify broader autonomy.

## Shipped On `develop`

- [x] tool policy modes for `safe`, `balanced`, and `full`
- [x] MCP access modes for `disabled`, `approval`, and `full`
- [x] structured audit logging for tool calls, tool results, and approval decisions
- [x] approval gates for high-risk actions in chat and WebSocket flows
- [x] secret egress redaction for chat output, step output, and surfaced errors
- [x] vault operation audit for secret store/get/list/delete actions
- [x] session-scoped secret references for safer downstream tool usage
- [x] approval flow improvements so approved chat actions can resume automatically

## Working On Now

- [ ] reduce reliance on raw `get_secret()` retrieval in favor of narrower secret-injection paths
- [ ] this workstream is not the repo-wide active focus while Runtime Reliability is still being hardened

## Still To Do On `develop`

- [ ] tighten isolation between planning, privileged execution, and future workflow/runtime layers
- [ ] add deeper policy distinctions inside MCP and external execution paths
- [ ] keep trust-boundary UX strict without making approvals noisy or unusable

## Acceptance Checklist

- [ ] privileged tools cannot run without an explicit policy path
- [ ] high-risk actions are pauseable and resumable with audit visibility
- [ ] secret use is scoped and auditable end to end
