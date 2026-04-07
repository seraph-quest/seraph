---
name: source-daily-review
description: Build a connector-first daily review plan, gather bounded source evidence, and summarize what moved
requires:
  tools: [get_goals, source_capabilities, plan_source_review, collect_source_evidence]
user_invocable: true
---

When the user asks for a daily review grounded in external work systems, follow this sequence:

1. Use `get_goals` first when active goals would help focus the review.
2. Inspect `source_capabilities` before claiming any authenticated access.
3. Use `plan_source_review` with `intent=daily_review` to choose provider-neutral evidence steps.
4. Use `collect_source_evidence` for the ready or partially ready steps in the plan. Do not fake missing steps.
5. Summarize:
   - what moved today
   - which items appear aligned with active goals
   - blockers, ambiguity, or missing evidence
   - the next best source if a preferred adapter is degraded
6. Keep observed facts separate from inferences.
