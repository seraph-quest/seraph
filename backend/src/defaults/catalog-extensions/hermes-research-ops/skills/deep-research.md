---
name: deep-research
description: Search, inspect, and synthesize multiple sources into a concise operator brief
requires:
  tools: [web_search, browse_webpage, write_file]
user_invocable: true
---

Use this skill when the user asks for a researched brief, comparison, or source-grounded summary.

Process:

1. Use `web_search` to discover candidate sources.
2. Use `browse_webpage` to inspect the most relevant results, preferring primary sources.
3. Synthesize findings into:
   - a short summary
   - key findings
   - open questions or uncertainty
   - cited source URLs
4. If the user asks to save the result, write a concise workspace note with `write_file`.

Keep the brief compact and evidence-led.
