---
title: "ADR-002: One-GPU Serial Priority Scheduling"
---

# ADR-002: One-GPU Serial Priority Scheduling

**Status:** Accepted

**Decision class:** Target architecture

## Context

The target machine has one physical GPU. Pretending GPU inference is parallel
creates contention, unpredictable latency, and starvation.

## Decision

All GPU-bound work enters one priority-aware broker. At most one GPU job is
active. A running job is not preempted by default; after it finishes, the broker
selects the highest-priority ready job. Interactive work outranks scheduled and
background work. Background screenshot analysis fills otherwise idle capacity.

Bounded CPU preparation and a small feeder window are allowed only when they do
not violate serial GPU execution or priority selection.

## Consequences

- Queue position, active job, priority, age, and degraded state must be visible.
- Accepted work needs bounded retries, cancellation, and non-starvation rules.
- Separate feature queues may expose domain state but cannot bypass the broker.

## Verification

Tests must prove mutual exclusion, next-ready priority, eventual background
progress, and interactive non-starvation. Runtime proof must show the active and
queued GPU work through an operator-visible status surface.
