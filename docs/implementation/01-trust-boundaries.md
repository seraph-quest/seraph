# Workstream 01: Trust Boundaries

## Status On `develop`

- [ ] Workstream 01 is only partially shipped on `develop`.

## Shipped On `develop`

- [x] tool policy modes for `safe`, `balanced`, and `full`
- [x] MCP access modes for `disabled`, `approval`, and `full`
- [x] approval gates for high-risk actions in chat and WebSocket flows
- [x] structured audit logging for tool calls, tool results, approvals, and runtime events
- [x] secret egress redaction for surfaced responses and errors
- [x] vault CRUD with audit visibility
- [x] session-scoped secret references for safer downstream tool usage

## Working On Now

- [ ] this workstream is not the repo-wide active focus while Runtime Reliability is still being hardened
- [ ] reduce reliance on raw secret retrieval in favor of narrower secret-injection paths

## Still To Do On `develop`

- [ ] tighten isolation between planning, privileged execution, and future workflow layers
- [ ] add deeper policy distinctions inside MCP and external execution paths
- [ ] keep trust UX strict without making approvals noisy or unusable

## Non-Goals

- a fake sense of safety based only on prompt instructions
- broadening high-risk execution before policy paths are clear

## Acceptance Checklist

- [ ] privileged tools cannot run without an explicit policy path
- [x] high-risk actions are pauseable and resumable with audit visibility
- [ ] secret use is scoped and auditable end to end
