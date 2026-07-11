---
title: "ADR-004: GPU Core And Paired Mac Edge"
---

# ADR-004: GPU Core And Paired Mac Edge

**Status:** Accepted

**Decision class:** Target architecture

## Context

Seraph's current repository workspace and canonical core are on the GPU host,
`jupyter`; the production LAN deployment remains gated by #741/#742 receipts.
The earlier arrangement in which browser/backend development surfaces ran on
the Mac and used GPU-hosted model services over LAN is historical context, not
current topology. The target retains consented Mac observation and native
interaction through a paired edge without moving canonical authority back to
the Mac.

## Decision

The authenticated Seraph core, canonical state, guardian kernel, capability
runtime, and model fabric run on the GPU server and are reachable on the trusted
LAN. The Mac runs a paired, revocable edge for screen observation and native
interaction. Edge loss degrades those capabilities but does not transfer
authority or canonical state to the edge.

Administration from the GPU-host workspace is local; an SSH hop back to
`jupyter` is not part of the lifecycle. Any genuinely remote administration is
separate from application transport. Runtime traffic uses authenticated
documented APIs. LAN exposure is not anonymous exposure.

## Consequences

- Pairing, consent, revocation, freshness, and capture state are visible.
- Migration requires backup/restore proof before the GPU copy is canonical.
- The current workspace and canonical core are on `jupyter`. Issues #741 and
  #742 provide deployment and data receipts. The Mac screenshot-push edge is
  not shipped until #749 provides pairing, upload, and revoke/offline receipts.

## Verification

Acceptance needs authenticated LAN probes, restart persistence, backup/restore,
edge revoke/offline tests, and UI/API proof of effective topology.
Internal model, wrapper, and backend ports are accepted only with an independent
Mac-side negative reachability receipt; local firewall interpretation alone is
not acceptance evidence.
