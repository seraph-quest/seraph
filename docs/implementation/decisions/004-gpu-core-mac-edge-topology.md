---
title: "ADR-004: GPU Core And Paired Mac Edge"
---

# ADR-004: GPU Core And Paired Mac Edge

**Status:** Accepted

**Decision class:** Target architecture

## Context

Seraph currently runs its browser/backend development surfaces on the Mac and
uses GPU-hosted model services over LAN. The target is an always-available core
on the GPU server while retaining consented Mac observation and interaction.

## Decision

The authenticated Seraph core, canonical state, guardian kernel, capability
runtime, and model fabric run on the GPU server and are reachable on the trusted
LAN. The Mac runs a paired, revocable edge for screen observation and native
interaction. Edge loss degrades those capabilities but does not transfer
authority or canonical state to the edge.

SSH is an administrator path, never the application transport. Runtime traffic
uses authenticated documented APIs. LAN exposure is not anonymous exposure.

## Consequences

- Pairing, consent, revocation, freshness, and capture state are visible.
- Migration requires backup/restore proof before the GPU copy is canonical.
- The shipped Mac-core topology remains supported until issues #741, #742, and
  #749 provide migration and edge receipts.

## Verification

Acceptance needs authenticated LAN probes, restart persistence, backup/restore,
edge revoke/offline tests, and UI/API proof of effective topology.
