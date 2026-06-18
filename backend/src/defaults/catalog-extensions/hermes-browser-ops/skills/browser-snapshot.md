---
name: browser-snapshot
description: Capture a focused follow-up snapshot from an existing browser session.
requires:
  tools: [browser_session]
user_invocable: false
---

Use this support skill when a running browser session needs one more structured
capture before Seraph summarizes or writes the result elsewhere.

1. Inspect the current session id and latest ref.
2. Capture a follow-up snapshot with `browser_session(action="snapshot", ...)`.
3. Read the new ref with `browser_session(action="read", ref=...)` if a later
   step needs the exact capture payload.
