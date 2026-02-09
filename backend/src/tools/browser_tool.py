"""Browser automation tool — extracts content from web pages using Playwright."""

import asyncio
import logging

from smolagents import tool

from config.settings import settings

logger = logging.getLogger(__name__)


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
                return f"Screenshot captured ({len(screenshot)} bytes). Base64: {encoded[:200]}..."

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

    if action not in ("extract", "html", "screenshot"):
        return f"Error: action must be 'extract', 'html', or 'screenshot', got '{action}'"

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                result = pool.submit(asyncio.run, _browse(url, action)).result()
            return result
        else:
            return asyncio.run(_browse(url, action))
    except Exception as e:
        logger.exception("Browser automation failed")
        return f"Error browsing {url}: {e}"
