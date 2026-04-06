---
name: source-goal-alignment
description: Compare active goals to external source evidence through provider-neutral review steps
requires:
  tools: [get_goals, get_goal_progress, source_capabilities, plan_source_review, collect_source_evidence]
user_invocable: true
---

When the user asks whether current work is aligned with goals:

1. Use `get_goals` and `get_goal_progress` to ground the current goal set first.
2. Inspect `source_capabilities` before choosing an external source path.
3. Use `plan_source_review` with `intent=goal_alignment`, passing the goal text as `goal_context`.
4. Collect evidence with `collect_source_evidence` for the ready steps in the plan.
5. Compare:
   - evidence that clearly advances the goal
   - evidence that looks neutral or maintenance-oriented
   - evidence that may represent drift
6. If external evidence is missing or degraded, say that clearly and keep the confidence low.
