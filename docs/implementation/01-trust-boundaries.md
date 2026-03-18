# Workstream 01: Trust Boundaries

## Status On `develop`

- [ ] Workstream 01 is only partially shipped on `develop`.

## Paired Research

- primary design doc: [04. Trust And Governance](/research/trust-and-governance)

## Shipped On `develop`

- [x] tool policy modes for `safe`, `balanced`, and `full`
- [x] MCP access modes for `disabled`, `approval`, and `full`
- [x] approval gates for high-risk actions in chat and WebSocket flows
- [x] structured audit logging for tool calls, tool results, approvals, and runtime events
- [x] secret egress redaction for surfaced responses and errors
- [x] vault CRUD with audit visibility
- [x] session-scoped secret references for safer downstream tool usage
- [x] explicit execution-boundary metadata and approval behavior surfaced for tools and reusable workflows
- [x] forced approval wrapping for reusable workflows that cross high-risk or approval-mode MCP boundaries

## Working On Now

- [x] this workstream shipped the first refreshed queue item through `execution-safety-hardening-v1`
- [x] this workstream now hands the queue lead to workflow control and guardian-learning work
- [ ] reduce reliance on raw secret retrieval in favor of narrower secret-injection paths

## Still To Do On `develop`

- [ ] tighten isolation between planning, privileged execution, and future workflow layers beyond the first metadata/approval hardening pass
- [ ] add deeper policy distinctions inside MCP and external execution paths
- [ ] keep trust UX strict without making approvals noisy or unusable

## Non-Goals

- a fake sense of safety based only on prompt instructions
- broadening high-risk execution before policy paths are clear

## Acceptance Checklist

- [x] privileged reusable workflows now expose an explicit policy path through approval behavior and execution-boundary metadata
- [x] high-risk actions are pauseable and resumable with audit visibility
- [ ] secret use is scoped and auditable end to end
