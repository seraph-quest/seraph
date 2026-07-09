---
title: "ADR-005: Epic Integration Branch Workflow"
---

# ADR-005: Epic Integration Branch Workflow

**Status:** Accepted for Epic #736

**Decision class:** Delivery workflow

## Context

Epic #736 spans dependent architecture, runtime, security, migration, presence,
and interface milestones. Merging incomplete slices directly to `develop` would
make the target contract and shipped contract difficult to distinguish.

## Decision

`feat/seraph-native-guardian-epic` is the integration branch. Each milestone
branch starts from the latest epic branch and opens a ready PR back to that
branch. Each PR requires focused validation and a fresh independent
Critic/Contrarian review. The epic branch periodically merges `develop` and runs
cumulative checks. One final ready PR targets `develop` after release-gate proof.

No part of this workflow authorizes a merge from `develop` to `main`.

## Consequences

- A milestone merged to the epic branch is integrated, not shipped on `develop`.
- Implementation docs on a milestone branch describe intended post-epic truth
  only when explicitly labeled.
- Issue and Project fields remain authoritative for execution state.
- Behavior-affecting updates to an open PR trigger fresh review.

## Verification

PR bases, issue linkage, Project fields, validation receipts, critic disposition,
periodic `develop` merges, and the final integration receipt must be confirmed by
GitHub state rather than prose alone.
