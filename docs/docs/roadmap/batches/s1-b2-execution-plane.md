---
title: S1-B2 Execution Plane
---

# S1-B2: Execution Plane

## Intent

Expand Seraph's ability to actually do work so the guardian thesis is backed by a more serious action layer.

## Capabilities in scope

- real shell and process execution beyond Python-only snippets
- stronger browser control and interactive web flows
- background process/session handling
- workflow engine direction for repeatable tool chains

## Non-goals

- full desktop computer control across every app
- unlimited shell access without policy controls
- large-scale workflow marketplace

## Required architectural changes

- split lightweight code execution from broader command/process execution
- upgrade browser tooling from extract-only patterns toward interactive automation
- define a process/session abstraction that can report status back to the UI
- establish the first workflow representation and execution contract

## Likely files/systems touched

- backend tools and tool registry
- sandbox / execution environment
- browser automation layer
- chat and step streaming for longer-running actions

## Acceptance criteria

- Seraph can run bounded shell/process tasks under policy control
- browser automation supports richer interactive flows
- longer-running tasks can surface intermediate state
- at least one workflow path exists for repeatable multi-step execution

## Dependencies on earlier batches

- depends on [S1-B1 Trust Boundaries](./s1-b1-trust-boundaries) for safe execution expansion

## Open risks

- execution breadth can outpace safety
- UX may degrade if longer tasks are not surfaced clearly
- browser automation can become flaky without careful constraints
