---
name: web-briefing
description: Fetch URLs and summarize into a structured briefing
requires:
  tools: [http_request, web_search]
user_invocable: true
---

When the user asks for a web briefing or to summarize web content, follow these steps:

1. If the user provides specific URLs, use `http_request` with `GET` to fetch each page
2. If the user provides a topic instead, use `web_search` to find relevant sources first, then fetch the top results with `http_request`
3. Extract the key content from each fetched page (ignore navigation, ads, boilerplate)
4. Synthesize a structured briefing with:
   - **Summary**: 2-3 sentence overview of the topic
   - **Key points**: bullet points of the most important information
   - **Sources**: list of URLs consulted with one-line descriptions
5. If a page fails to load, note it and continue with available sources
6. Keep the briefing concise — aim for a 2-minute read
