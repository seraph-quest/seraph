---
name: web-brief-to-file
description: Search the web for a topic and save the result as a workspace note.
user_invocable: true
requires:
  tools: [web_search, write_file]
inputs:
  query:
    type: string
    description: Search query to run on the web.
  file_path:
    type: string
    description: Workspace-relative output path for the saved brief.
steps:
  - id: search
    tool: web_search
    arguments:
      query: "{{ query }}"
  - id: save
    tool: write_file
    arguments:
      file_path: "{{ file_path }}"
      content: |
        Web brief for "{{ query }}"

        {{ steps.search.result }}
result: Saved a web brief for "{{ query }}" to {{ file_path }}.
---

Use this when you want a repeatable search-to-note workflow instead of ad hoc tool calls.
