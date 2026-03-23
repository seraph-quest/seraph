"""Browser automation tool — extracts content from web pages using Playwright."""

import asyncio
import logging
from urllib.parse import urlparse

from smolagents import tool

from config.settings import settings
from src.audit.runtime import log_integration_event_sync
from src.security.site_policy import SiteAccessDecision, evaluate_site_access

logger = logging.getLogger(__name__)

def _browser_details(url: str, action: str, decision: SiteAccessDecision | None = None) -> dict[str, str | bool]:
    parsed = urlparse(url)
    details: dict[str, str | bool] = {
        "hostname": parsed.hostname or "",
        "scheme": parsed.scheme or "",
        "action": action,
    }
    if decision is not None:
        details["site_policy_reason"] = decision.reason or ""
        details["site_policy_rule"] = decision.matched_rule or ""
        details["site_allowlist_active"] = decision.allowlist_active
    return details


def _blocked_site_message(decision: SiteAccessDecision) -> str:
    if decision.reason == "internal_private":
        return "Error: Access to internal/private network addresses is not allowed."
    if decision.reason == "blocklisted_domain":
        return f"Error: Access to '{decision.hostname}' is blocked by site policy."
    if decision.reason == "not_allowlisted":
        return f"Error: Access to '{decision.hostname}' is not permitted by the browser site allowlist."
    return "Error: URL could not be evaluated by site policy."


async def _browse(url: str, action: str) -> str:
    """Async Playwright browsing implementation."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        try:
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=settings.browser_timeout * 1000,
            )
            # Wait a bit for dynamic content
            await page.wait_for_timeout(1000)

            if action == "screenshot":
                screenshot = await page.screenshot(type="png")
                import base64
                encoded = base64.b64encode(screenshot).decode("utf-8")
                return f"Screenshot captured ({len(screenshot)} bytes). Base64 data: {encoded}"

            elif action == "html":
                html = await page.content()
                if len(html) > 50000:
                    html = html[:50000] + "\n... (truncated)"
                return html

            else:  # "extract" — default
                # Remove script/style tags, get text content
                text = await page.evaluate("""() => {
                    const scripts = document.querySelectorAll('script, style, nav, footer, header');
                    scripts.forEach(el => el.remove());
                    return document.body.innerText;
                }""")
                if len(text) > 20000:
                    text = text[:20000] + "\n... (truncated)"
                return text if text.strip() else "(page had no readable text content)"

        finally:
            await browser.close()


def _run_browse_sync(url: str, action: str) -> str:
    """Bridge the async browser implementation into the thread pool."""
    return asyncio.run(_browse(url, action))


@tool
def browse_webpage(url: str, action: str = "extract") -> str:
    """Browse a webpage and extract its content.

    Use this tool to visit web pages, read articles, check documentation,
    or gather information from any URL. This uses a real browser so it
    works with JavaScript-rendered pages.

    Args:
        url: The full URL to visit (must start with http:// or https://).
        action: What to do with the page. Options:
            - "extract" (default): Get the readable text content.
            - "html": Get the raw HTML source.
            - "screenshot": Take a screenshot of the page.

    Returns:
        The page content based on the chosen action.
    """
    if not url.startswith(("http://", "https://")):
        return "Error: URL must start with http:// or https://"

    decision = evaluate_site_access(url)
    if not decision.allowed:
        log_integration_event_sync(
            integration_type="browser",
            name="playwright",
            outcome="blocked",
            details=_browser_details(url, action, decision),
        )
        return _blocked_site_message(decision)

    if action not in ("extract", "html", "screenshot"):
        return f"Error: action must be 'extract', 'html', or 'screenshot', got '{action}'"

    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    except Exception:  # pragma: no cover - playwright import failures are handled below
        PlaywrightTimeoutError = TimeoutError

    try:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            result = pool.submit(_run_browse_sync, url, action).result()
        log_integration_event_sync(
            integration_type="browser",
            name="playwright",
            outcome="succeeded",
            details=_browser_details(url, action, decision),
        )
        return result
    except (TimeoutError, PlaywrightTimeoutError) as e:
        log_integration_event_sync(
            integration_type="browser",
            name="playwright",
            outcome="timed_out",
            details={
                **_browser_details(url, action, decision),
                "timeout_seconds": settings.browser_timeout,
                "error": str(e),
            },
        )
        logger.exception("Browser automation timed out")
        return f"Error: browsing {url} timed out after {settings.browser_timeout}s"
    except Exception as e:
        log_integration_event_sync(
            integration_type="browser",
            name="playwright",
            outcome="failed",
            details={
                **_browser_details(url, action, decision),
                "error": str(e),
            },
        )
        logger.exception("Browser automation failed")
        return f"Error: browsing {url} failed: {e}"
