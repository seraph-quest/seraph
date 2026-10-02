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

The task approval review reads the actual pending approval and revalidates
task revision, latest attempt, durable run, goal revision and current owner
before and after the owning approval decision. Missing, conflicting, expired,
foreign or stale metadata withholds controls. Approving a row records the
decision; execution and verified completion remain the task's canonical state.
Standalone approvals continue to use the existing approval pane.

Unknown effects never authorize a blind retry. For a GitHub follow-through
task advertising reconciliation, the inspector reads the exact owning job
from the latest attempt and requests its existing GET-only destination
readback. It retains the original operation identity and displays the actual
verified or unresolved receipt. Unsupported capabilities explicitly say that
no readback control is available; existing inspect, cancel and review actions
remain governed by their owning API. Cost recovery opens the existing Settings
accounting inspector only when its API advertises settlement for the exact
job, owner and goal revision. The attention UI constructs no settlement amount
or evidence claim.

Keyless proof uses authenticated APIs, real source-watch execution, durable
tasks and approvals, file-backed SQLite restart, and intercepted remote
transport. Initial publication and later readback are separately counted;
recovery must perform no remote POST. This establishes mechanical storage,
ownership and recovery behavior, not live-provider usefulness or account-write
acceptance. Managed browser and independent review receipts remain required
before integration is described as shipped.
