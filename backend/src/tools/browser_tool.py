"""Browser automation tool — extracts content from web pages using Playwright."""

import asyncio
import ipaddress
import logging
from urllib.parse import urlparse

from smolagents import tool

from config.settings import settings
from src.audit.runtime import log_integration_event_sync

logger = logging.getLogger(__name__)

# Block access to internal/private networks
_BLOCKED_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "[::1]"}


def _is_internal_url(url: str) -> bool:
    """Check if a URL points to an internal/private network address."""
    parsed = urlparse(url)
    hostname = parsed.hostname or ""

    if hostname in _BLOCKED_HOSTS:
        return True

    # Check for private IP ranges
    try:
        ip = ipaddress.ip_address(hostname)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        pass

    # Block common internal hostnames
    if hostname.endswith((".local", ".internal", ".localhost")):
        return True

    return False


def _browser_details(url: str, action: str) -> dict[str, str]:
    parsed = urlparse(url)
    return {
        "hostname": parsed.hostname or "",
        "scheme": parsed.scheme or "",
        "action": action,
    }


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

    if _is_internal_url(url):
        log_integration_event_sync(
            integration_type="browser",
            name="playwright",
            outcome="blocked",
            details=_browser_details(url, action),
        )
        return "Error: Access to internal/private network addresses is not allowed."

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
            details=_browser_details(url, action),
        )
        return result
    except (TimeoutError, PlaywrightTimeoutError) as e:
        log_integration_event_sync(
            integration_type="browser",
            name="playwright",
            outcome="timed_out",
            details={
                **_browser_details(url, action),
                "timeout_seconds": settings.browser_timeout,
                "error": str(e),
            },
        )
        logger.exception("Browser automation timed out")
        return f"Error browsing {url}: timed out after {settings.browser_timeout}s"
    except Exception as e:
        log_integration_event_sync(
            integration_type="browser",
            name="playwright",
            outcome="failed",
            details={
                **_browser_details(url, action),
                "error": str(e),
            },
        )
        logger.exception("Browser automation failed")
        return f"Error browsing {url}: {e}"
