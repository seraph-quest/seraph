---
name: session-recall
description: Recover the most relevant prior thread context before continuing
requires:
  tools: [session_search, clarify]
user_invocable: true
---

Use this skill when the user asks Seraph to resume a topic, recover prior context, or continue work across older threads.

Process:

1. Use `session_search` with a short topic query taken from the user's request.
2. Prefer the smallest useful recall set rather than dumping every matching snippet.
3. If the request is ambiguous, use `clarify` to ask what thread, project, or time window the user means.
4. Return:
   - the most relevant thread or threads
   - the key commitments or open questions
   - the next recommended continuation step

Never pretend recall happened if `session_search` returned nothing.
