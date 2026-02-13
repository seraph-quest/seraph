from config.settings import settings
from ddgs import DDGS
from smolagents import tool


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """Search the web using DuckDuckGo and return results.

    Args:
        query: The search query string.
        max_results: Maximum number of results to return (default 5).

    Returns:
        Formatted search results with titles, URLs, and snippets.
    """
    try:
        with DDGS(timeout=settings.web_search_timeout) as ddgs:
            results = list(ddgs.text(query, max_results=max_results))

        if not results:
            return f"No results found for: {query}"

        formatted = []
        for i, r in enumerate(results, 1):
            title = r.get("title", "No title")
            href = r.get("href", "No URL")
            body = r.get("body", "No description")
            formatted.append(f"{i}. {title}\n   URL: {href}\n   {body}")

        return "\n\n".join(formatted)
    except Exception as e:
        return f"Search error: {e}"
