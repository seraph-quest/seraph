# Workstream 02: Execution Plane

## Status On `develop`

- [ ] Workstream 02 is only partially shipped on `develop`.

## Shipped On `develop`

- [x] 17 built-in tool capabilities registered through the native tool registry
- [x] shell execution through the sandboxed shell tool
- [x] browser automation through the browser tool
- [x] filesystem read/write tool surface
- [x] soul and goals tool surface for agent self-context
- [x] vault tool surface for controlled secret storage and retrieval
- [x] web-search tool surface
- [x] dynamic MCP tool loading and runtime-managed MCP server configuration
- [x] visible tool execution in chat, WebSocket, onboarding, strategist, and specialist flows

## Working On Now

- [ ] this workstream is not the repo-wide active focus while Runtime Reliability is still being hardened

## Still To Do On `develop`

- [ ] richer browser and workflow execution beyond the current tool-level operations
- [ ] stronger multi-step workflow composition on top of the current tool primitives
- [ ] broader external system leverage without weakening trust boundaries

## Non-Goals

- adding tools just to increase the count
- unbounded process execution with weak policy control

## Interface Checklist

- [x] native tools are auto-discoverable through the registry
- [x] MCP tools can be added and removed without code changes
- [x] tool execution is visible to the user

## Acceptance Checklist

- [x] Seraph can browse, search, read/write local files, inspect goals, and use the shell
- [x] Seraph can use connected MCP servers in the current runtime
- [ ] Seraph can execute richer cross-tool workflows than it can today
