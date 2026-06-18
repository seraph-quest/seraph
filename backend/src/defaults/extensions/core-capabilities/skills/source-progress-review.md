---
name: source-progress-review
description: Build a provider-neutral progress review from typed source adapters without hardcoding one provider flow
requires:
  tools: [source_capabilities, plan_source_review, collect_source_evidence]
user_invocable: true
---

When the user asks for progress on a repo, project, or focus area:

1. Inspect `source_capabilities` first.
2. Use `plan_source_review` with `intent=progress_review` and the user’s focus area.
3. Collect evidence only from the ready or degraded typed steps the plan exposes.
4. If typed adapters are missing, say so explicitly and name the next best public fallback instead of inventing access.
5. Return a short structured review:
   - current movement
   - strongest evidence
   - uncertainty or gaps
   - suggested next evidence step
