---
name: source-evidence-review
description: Inspect available source surfaces first, then collect bounded evidence without hallucinating authenticated access
requires:
  tools: [source_capabilities, collect_source_evidence, web_search, browse_webpage, browser_session]
user_invocable: true
---

When the user asks for evidence from a source system, inspect `source_capabilities` first and prefer `collect_source_evidence` when a provider-neutral contract already exists.

Follow these rules:

1. Prefer typed authenticated connectors over browser login for authenticated systems.
2. Use `collect_source_evidence` for provider-neutral source contracts before falling back to raw tool composition, including query-based authenticated connector reads when a typed adapter is bound to a live runtime.
3. Use `web_search` for public discovery, `browse_webpage` for explicit public pages, and `browser_session` for multi-step public inspection when a normalized adapter path is not available.
4. If the source is only available through raw MCP or is not available at all, say that clearly instead of claiming typed read or write access.
5. If the user provides an explicit public URL, inspect that page directly before generalizing.
6. When evidence is ambiguous, separate observed facts from inferences.

Your output should be short and structured:

- available source path
- evidence gathered
- confidence or ambiguity
- next best source if the preferred typed path is unavailable
