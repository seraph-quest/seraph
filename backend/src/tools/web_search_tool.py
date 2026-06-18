import logging
from typing import Any

from ddgs import DDGS
from smolagents import tool

from config.settings import settings
from src.audit.runtime import log_integration_event_sync
from src.security.site_policy import evaluate_site_access

logger = logging.getLogger(__name__)


def _search_details(query: str, max_results: int, **extra: object) -> dict[str, object]:
    return {
        "query_length": len(query),
        "max_results": max_results,
        **extra,
    }


def _filter_search_results(results: list[dict[str, object]]) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    allowed: list[dict[str, object]] = []
    blocked: list[dict[str, str]] = []
    for result in results:
        href = str(result.get("href") or "").strip()
        decision = evaluate_site_access(href)
        if decision.allowed:
            allowed.append(result)
            continue
        blocked.append(
            {
                "hostname": decision.hostname,
                "reason": decision.reason or "",
                "rule": decision.matched_rule or "",
            }
        )
    return allowed, blocked


def search_web_records(query: str, max_results: int = 5) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Return structured allowed search results plus blocked-result metadata."""
    with DDGS(timeout=settings.web_search_timeout) as ddgs:
        results = list(ddgs.text(query, max_results=max_results))
    return _filter_search_results(results)


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
        filtered_results, blocked_results = search_web_records(query, max_results=max_results)

        if not filtered_results and not blocked_results:
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

        filtered_count = len(blocked_results)
        blocked_hostnames = sorted({item["hostname"] for item in blocked_results if item["hostname"]})
        block_reasons = sorted({item["reason"] for item in blocked_results if item["reason"]})

        if not filtered_results:
            log_integration_event_sync(
                integration_type="web_search",
                name="duckduckgo",
                outcome="blocked",
                details=_search_details(
                    query,
                    max_results,
                    result_count=filtered_count,
                    filtered_result_count=filtered_count,
                    blocked_hostnames=blocked_hostnames[:5],
                    blocked_reasons=block_reasons,
                ),
            )
            return f"No allowed results found for: {query}"

        log_integration_event_sync(
            integration_type="web_search",
            name="duckduckgo",
            outcome="succeeded",
            details=_search_details(
                query,
                max_results,
                result_count=len(filtered_results),
                filtered_result_count=filtered_count,
                blocked_hostnames=blocked_hostnames[:5],
                blocked_reasons=block_reasons,
            ),
        )

        formatted = []
        for i, r in enumerate(filtered_results, 1):
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
