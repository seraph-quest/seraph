---
name: browser-session-review
description: Use structured browser sessions when Seraph needs to inspect one web target over multiple steps.
requires:
  tools: [browser_session]
user_invocable: true
---

Use this skill when Seraph needs to open a page once, keep a stable session id,
capture one or more follow-up snapshots, and refer back to those stored results
by page ref while working through a multi-step browsing task.

1. Open a session with `browser_session(action="open", url=...)`.
2. If a follow-up capture is needed, call `browser_session(action="snapshot", session_id=...)`.
3. Use `browser_session(action="read", ref=...)` when a later step needs the
   exact captured content again.
4. Close the session once the task is complete.

Today, packaged remote browser providers may still run in `local_fallback`
mode. Treat the session id and refs as handles to stored captures, not as proof
that a live remote browser transport is active.

Never claim a session exists unless `browser_session` returned a session id.
