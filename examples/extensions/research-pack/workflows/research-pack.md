---
name: Research Pack
description: Research Pack workflow
requires:
  tools: [read_file]
inputs:
  file_path:
    type: string
    description: File to inspect
steps:
  - id: inspect_file
    tool: read_file
    arguments:
      file_path: "{{ file_path }}"
---

Use the Research Pack workflow as a starting point.
