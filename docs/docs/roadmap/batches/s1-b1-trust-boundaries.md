---
title: S1-B1 Trust Boundaries
---

# S1-B1: Trust Boundaries

## Intent

Add the minimum serious trust and control layer Seraph needs before broader autonomy work.

## Capabilities in scope

- policy profiles for tool access
- approval gates for sensitive actions
- secret scoping and safer credential usage
- audit log of meaningful actions and approvals
- clearer isolation boundaries between planning, execution, and privileged operations

## Non-goals

- full enterprise multi-tenant security
- hardware enclave or IronClaw-style Wasm rearchitecture
- complete OAuth platform buildout

## Required architectural changes

- introduce policy-aware execution paths around tools
- add an approval checkpoint model for high-risk actions
- separate secret access from ordinary tool invocation
- define a consistent audit event format for agent actions

## Likely files/systems touched

- backend agent and tool orchestration
- settings and policy configuration
- vault / secret handling
- chat and WS flows where approvals surface to the user

## Acceptance criteria

- privileged tools cannot run without an explicit policy path
- high-risk actions can be paused for approval
- secret use becomes scoped and auditable
- actions and approvals are logged in a structured way

## Dependencies on earlier batches

- none; this batch starts the season

## Open risks

- poor policy design can create user friction without meaningful safety
- approval UX can become annoying if the risk model is too coarse
- partial trust boundaries may create a false sense of safety if messaging is sloppy
