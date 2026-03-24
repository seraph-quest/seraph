---
name: openprose-brief
description: Reconstruct a brief from prior session context using the OpenProse runtime profile.
runtime_profile: openprose
requires:
  tools: [session_search]
inputs:
  topic:
    type: string
    description: Topic or thread to recall.
steps:
  - id: recall
    tool: session_search
    arguments:
      query: "{{ topic }}"
      limit: 3
result: OpenProse brief scaffold prepared for {{ topic }}.
---

OpenProse-style briefing workflow.
