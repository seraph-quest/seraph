---
name: lobster-review
description: Delegate a bounded review pass using the Lobster runtime profile.
runtime_profile: lobster
requires:
  tools: [delegate_task]
inputs:
  task:
    type: string
    description: Review task to delegate.
steps:
  - id: delegate
    tool: delegate_task
    arguments:
      task: "{{ task }}"
      specialist: workflow
result: Lobster review delegation launched for {{ task }}.
---

Lobster-style delegated review workflow.
