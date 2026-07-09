---
title: "ADR-003: Canonical Memory Boundary"
---

# ADR-003: Canonical Memory Boundary

**Status:** Accepted

**Decision class:** Target architecture

## Context

Seraph may benefit from vector stores, graph memory, or external advisory memory
systems. Letting any optional provider become authoritative would make identity,
privacy, deletion, provenance, and recovery provider-dependent.

## Decision

Seraph-owned storage is canonical for goals, user-approved facts, episodic
records, jobs, artifacts, checkpoints, approvals, and audit history. Advisory
memory providers may index, retrieve, rank, or suggest records. Their evidence
must preserve provenance and confidence and cannot overwrite canonical truth.

## Consequences

- Canonical writes succeed before optional mirroring.
- Provider outages cannot prevent canonical recall or deletion workflows.
- Conflicts are visible and resolved through Seraph policy/operator action.
- A graph-memory system such as Graphiti/GBrain may be benchmarked behind the
  advisory boundary; adoption is not implied by this ADR.

## Verification

Tests must cover canonical-first writes, provider failure, provenance,
conflicting evidence, deletion, export, backup, and restore.
