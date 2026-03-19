---
title: S4-B1 Avatar Reflection
---

# S4-B1: Avatar Reflection

> Archive note: this batch belongs to the retired village/avatar direction and is kept only as historical planning context.

## Intent

Make the avatar visibly reflect real user and guardian state.

## Capabilities in scope

- state-driven ambient behavior
- mood and focus reflection
- stronger visual feedback loops

## Non-goals

- full village progression systems

## Required architectural changes

- connect ambient and observer state to Phaser behaviors
- define a stable mapping from system state to avatar expression

## Likely files/systems touched

- frontend game scene
- animation state machine
- ambient/proactive state plumbing

## Acceptance criteria

- avatar behavior changes meaningfully with real state

## Dependencies on earlier batches

- depends on Season 3 making state richer

## Open risks

- can feel cosmetic if state mapping is shallow
