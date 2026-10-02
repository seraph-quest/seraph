---
title: Attention and Recovery
---

# Attention and Recovery

**Status:** Partial, branch-local implementation; not shipped on `develop`.
**Scope:** composition of existing Home, Guardian Inbox, task inspector,
approvals, owning capability readback and Settings accounting controls.

Home shows a bounded **Needs attention** list. Exact current-root task
approvals rank first, followed by unknown effects or cost, blocked tasks,
failed tasks and stale verification. A linked Inbox item and task appear once.
Each entry retains its reason, age, goal and thread context. Counts describe
the fetched page. Home takes one bounded snapshot on mount; **Refresh Home**
updates it explicitly. Failed reads retain last-confirmed metadata and remove
unconfirmed recovery authority.

Opening a task from attention or Inbox uses the existing Work inspector.
**Return to Home attention** or **Return to Inbox decision** restores the
originating view and keyboard focus. Goal inspection reads the owning goal
tree once and selects only the exact goal present in that response. Thread
inspection uses the existing thread loader. All navigation bindings use the
finite authenticated principal and operator root; a root change discards
retained origins and asynchronous responses. Recovered selected history stays
visibly read only, with navigation available and effect controls unavailable.

Pending approval timestamps include UTC offsets, so valid approvals retain
their expiry instant in browsers in other timezones. The task approval review
reads the actual pending approval and revalidates
task revision, latest attempt, durable run, goal revision and current owner
before and after the owning approval decision. Missing, conflicting, expired,
foreign or stale metadata withholds controls. Approving a row records the
decision; execution and verified completion remain the task's canonical state.
Standalone approvals continue to use the existing approval pane.

Unknown effects never authorize a blind retry. For a GitHub follow-through
task advertising reconciliation, the inspector reads the exact owning job
from the latest attempt and requests its existing GET-only destination
readback. It retains the original operation identity and displays the actual
verified or unresolved receipt. Successful readback atomically annotates only
matching failed-readback diagnostics as resolved, preserving their original
status and exact parent linkage. The existing dispatcher adopts the latest
ended unknown GitHub attempt only after its exact root, proof, original finite
owner, goal and connection revision remain valid. Expiry, revocation or goal
changes retain the verified owning receipt and a specific blocked task reason;
they cannot revive authority. Unsupported capabilities explicitly say that
no readback control is available; existing inspect, cancel and review actions
remain governed by their owning API. Cost recovery opens the existing Settings
accounting inspector only when its API advertises settlement for the exact
job, owner and goal revision. The attention UI constructs no settlement amount
or evidence claim.

The rendered Warsaw browser journey is mechanically verified through real ASGI
HTTP/WebSocket handlers and retained SQLite/artifacts, with intercepted
public-source/GitHub transport and an explicit server-side test permission.
Recreating the ASGI app against the same database proves persisted recovery,
not a managed-host backend process restart. Production GitHub write-consent
creation remains a separate incomplete boundary owned by the tested-publication
milestone; this UI does not create that consent. Live external usefulness
remains unverified.
