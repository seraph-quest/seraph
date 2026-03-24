---
name: llm-task-triage
description: Gather prior context for a bounded task using the LLM Task runtime profile.
runtime_profile: llm-task
requires:
  tools: [session_search]
inputs:
  task:
    type: string
    description: Task prompt to recall context for.
steps:
  - id: context
    tool: session_search
    arguments:
      query: "{{ task }}"
      limit: 2
result: LLM task triage prepared for {{ task }}.
---

LLM Task-style triage workflow.
