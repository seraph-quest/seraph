---
name: goal-snapshot-to-file
description: Export the current goal state into a workspace note for review or planning.
user_invocable: true
requires:
  tools: [get_goals, write_file]
  skills: [goal-reflection]
inputs:
  file_path:
    type: string
    description: Workspace-relative output path for the goal snapshot.
steps:
  - id: goals
    tool: get_goals
    arguments: {}
  - id: save
    tool: write_file
    arguments:
      file_path: "{{ file_path }}"
      content: |
        Goal snapshot

        {{ steps.goals.result }}
result: Saved the current goal snapshot to {{ file_path }}.
---

Use this when you want a reusable export of current goals into a persistent note.
