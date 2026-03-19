---
title: S3-B1 Human World Model
---

# S3-B1: Human World Model

## Intent

Define a richer memory model for people, commitments, projects, and ongoing life context.

## Capabilities in scope

- structured entities and relationships
- commitment and project tracking
- better linkage between soul, sessions, goals, and long-term memory

## Non-goals

- exhaustive personal knowledge graph design in one batch

## Required architectural changes

- introduce a richer memory schema beyond generic vector recall
- connect memory objects to real guardian workflows

## Likely files/systems touched

- memory layer
- goals and profile model
- strategist context inputs

## Acceptance criteria

- Seraph can retain more structured human context than today

## Dependencies on earlier batches

- benefits from Season 1 reliability and observability

## Open risks

- schema complexity can outrun real use cases
