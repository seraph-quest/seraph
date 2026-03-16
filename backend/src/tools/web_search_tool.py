import logging

from ddgs import DDGS
from smolagents import tool

from config.settings import settings
from src.audit.runtime import log_integration_event_sync

logger = logging.getLogger(__name__)


def _search_details(query: str, max_results: int, **extra: object) -> dict[str, object]:
    return {
        "query_length": len(query),
        "max_results": max_results,
        **extra,
    }


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
            log_integration_event_sync(
                integration_type="web_search",
                name="duckduckgo",
                outcome="empty_result",
                details=_search_details(
                    query,
                    max_results,
                    result_count=0,
                ),
            )
            return f"No results found for: {query}"

        log_integration_event_sync(
            integration_type="web_search",
            name="duckduckgo",
            outcome="succeeded",
            details=_search_details(
                query,
                max_results,
                result_count=len(results),
            ),
        )

        formatted = []
        for i, r in enumerate(results, 1):
            title = r.get("title", "No title")
            href = r.get("href", "No URL")
            body = r.get("body", "No description")
            formatted.append(f"{i}. {title}\n   URL: {href}\n   {body}")

        return "\n\n".join(formatted)
    except TimeoutError:
        log_integration_event_sync(
            integration_type="web_search",
            name="duckduckgo",
            outcome="timed_out",
            details=_search_details(
                query,
                max_results,
                timeout_seconds=settings.web_search_timeout,
            ),
        )
        return f"Search error: timed out after {settings.web_search_timeout}s"
    except Exception as e:
        log_integration_event_sync(
            integration_type="web_search",
            name="duckduckgo",
            outcome="failed",
            details=_search_details(
                query,
                max_results,
                error=str(e),
            ),
        )
        logger.exception("Web search failed")
        return f"Search error: {e}"
