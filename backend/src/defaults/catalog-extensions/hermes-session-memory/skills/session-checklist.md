---
name: session-checklist
description: Convert active work into a maintained session-local checklist
requires:
  tools: [todo]
user_invocable: true
---

Use this skill when the user needs a compact working checklist for the current thread.

Process:

1. Inspect the current list with `todo(action="list")`.
2. If no structured list exists, propose or apply a short checklist through `todo(action="set")`.
3. Keep items concrete and execution-oriented.
4. When the user finishes work, reopen, complete, or remove items instead of rewriting the whole list.

Prefer a short, stable checklist over verbose planning text.
