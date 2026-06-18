---
name: source-progress-report
description: Build a provider-neutral progress report and publish it through an authenticated work-item adapter when one is available
requires:
  tools: [source_capabilities, plan_source_report, collect_source_evidence, execute_source_mutation]
user_invocable: true
---

When the user asks for a status report, standup note, or progress update:

1. Inspect `source_capabilities` first.
2. Use `plan_source_report` with the user’s focus area and target reference when they want the result posted somewhere.
3. Gather only the ready or degraded evidence steps from the embedded review plan.
4. Draft the report from the returned outline and evidence, naming uncertainty instead of inventing coverage.
5. If the user wants it published and the publish plan is `approval_required`, use `execute_source_mutation` with the scoped action kind and payload.
6. If no authenticated publication path is ready, say so explicitly and return the draft instead of pretending the write succeeded.
