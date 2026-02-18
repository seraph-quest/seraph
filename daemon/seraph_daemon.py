"""Seraph native macOS daemon — captures active window context and posts to backend.

Polls the frontmost application name and window title, posting changes to the
Seraph backend's /api/observer/context endpoint.  Runs natively on macOS
(outside Docker) and requires only the Accessibility permission for window titles.

With --ocr, captures and analyzes the screen on context switch using a vision model.
Blocked apps (password managers, banking, etc.) are never screenshotted.

Usage:
    python seraph_daemon.py [--url URL] [--interval SECS] [--idle-timeout SECS] [--verbose]
    python seraph_daemon.py --ocr [--ocr-provider apple-vision] [--verbose]
    python seraph_daemon.py --ocr --ocr-provider openrouter [--blocklist-file PATH] [--verbose]
"""

import argparse
import asyncio
import logging
import os
import signal
import subprocess
import sys
import time

import httpx

logger = logging.getLogger("seraph_daemon")

# ─── macOS helpers ────────────────────────────────────────


def get_frontmost_app_name() -> str | None:
    """Return the localized name of the frontmost application via NSWorkspace.

    Requires no special permissions.
    """
    try:
        from AppKit import NSWorkspace

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return app.localizedName() if app else None
    except Exception:
        logger.debug("NSWorkspace call failed", exc_info=True)
        return None


def get_window_title() -> str | None:
    """Return the title of the frontmost window via AppleScript.

    Requires the Accessibility permission (System Settings > Privacy & Security
    > Accessibility).  Returns None if permission is not granted or no window
    is open.
    """
    try:
        result = subprocess.run(
            [
                "osascript",
                "-e",
                'tell application "System Events" to get name of first window '
                "of (first application process whose frontmost is true)",
            ],
            capture_output=True,
            text=True,
            timeout=3,
        )
        title = result.stdout.strip()
        return title if title else None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    except Exception:
        logger.debug("osascript call failed", exc_info=True)
        return None


def get_idle_seconds() -> float:
    """Return seconds since last user input event (mouse/keyboard).

    Uses Quartz Event Services — no special permission required.
    """
    try:
        import Quartz

        return Quartz.CGEventSourceSecondsSinceLastEventType(
            Quartz.kCGEventSourceStateCombinedSessionState,
            Quartz.kCGAnyInputEventType,
        )
    except Exception:
        logger.debug("Quartz idle check failed", exc_info=True)
        return 0.0


def format_active_window(app_name: str | None, window_title: str | None) -> str | None:
    """Format the active window string for the backend."""
    if not app_name:
        return None
    if window_title:
        return f"{app_name} \u2014 {window_title}"
    return app_name


# ─── Capture mode helper ─────────────────────────────────


async def fetch_capture_mode(client: httpx.AsyncClient, url: str) -> str:
    """Fetch capture mode setting from backend. Returns 'on_switch' on error."""
    try:
        r = await client.get(f"{url}/api/settings/capture-mode")
        if r.status_code == 200:
            return r.json().get("mode", "on_switch")
    except Exception:
        pass
    return "on_switch"


# ─── Main loop ────────────────────────────────────────────


async def poll_loop(
    url: str,
    interval: float,
    idle_timeout: float,
    verbose: bool,
    ocr_provider: "ocr.base.OCRProvider | None" = None,
    blocklist: set[str] | None = None,
) -> None:
    """Core polling loop — detect window changes, post to backend.

    When ocr_provider is set, captures and analyzes the screen on each context
    switch (not on a timer). Blocked apps are never screenshotted.
    """
    from blocklist import is_blocked

    last_posted: str | None = None
    was_idle = False
    _blocklist = blocklist or set()

    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            try:
                # Check idle state
                idle_secs = get_idle_seconds()
                is_idle = idle_secs > idle_timeout

                if is_idle:
                    if not was_idle:
                        idle_min = int(idle_timeout / 60)
                        logger.info("idle (%dm)", idle_min)
                        was_idle = True
                    await asyncio.sleep(interval)
                    continue

                if was_idle:
                    logger.info("active again")
                    was_idle = False

                # Get current window
                app_name = get_frontmost_app_name()
                window_title = get_window_title()
                active_window = format_active_window(app_name, window_title)

                # Skip if unchanged
                if active_window == last_posted:
                    await asyncio.sleep(interval)
                    continue

                # Build observation payload on context switch
                observation = None
                if ocr_provider is not None and app_name:
                    observation = {
                        "app": app_name,
                        "window_title": window_title or "",
                    }

                    if is_blocked(app_name, _blocklist):
                        observation["blocked"] = True
                        if verbose:
                            logger.info("Blocked app: %s — skipping screenshot", app_name)
                    else:
                        try:
                            from ocr.screenshot import capture_screen_png

                            png_bytes = await asyncio.to_thread(capture_screen_png)
                            if png_bytes:
                                result = await ocr_provider.analyze_screen(png_bytes, app_name)
                                if result.success:
                                    observation.update(result.data)
                                    if verbose:
                                        ts = time.strftime("%H:%M:%S")
                                        logger.info(
                                            "[%s] analyzed (%dms): %s",
                                            ts,
                                            result.duration_ms,
                                            result.data.get("summary", "")[:80],
                                        )
                                else:
                                    logger.debug("Screen analysis failed: %s", result.error)
                        except Exception:
                            logger.debug("Screenshot/analysis error", exc_info=True)

                # Post to backend
                try:
                    payload: dict = {
                        "active_window": active_window,
                    }
                    if observation is not None:
                        payload["observation"] = observation
                        payload["switch_timestamp"] = time.time()

                    await client.post(
                        f"{url}/api/observer/context",
                        json=payload,
                    )
                    if verbose:
                        ts = time.strftime("%H:%M:%S")
                        logger.info("[%s] \u2192 %s", ts, active_window)
                    last_posted = active_window
                except httpx.ConnectError:
                    logger.warning("Backend not reachable at %s — will retry", url)
                except httpx.HTTPError as exc:
                    logger.warning("HTTP error posting context: %s", exc)

            except Exception:
                logger.exception("Unexpected error in poll loop")

            await asyncio.sleep(interval)


async def periodic_capture_loop(
    url: str,
    idle_timeout: float,
    verbose: bool,
    ocr_provider: "ocr.base.OCRProvider | None" = None,
    blocklist: set[str] | None = None,
) -> None:
    """Periodic capture loop — takes screenshots at intervals within the same app.

    Runs alongside poll_loop. Polls backend for capture_mode every 60s.
    When mode is 'balanced' (300s) or 'detailed' (60s), captures periodic
    screenshots of the current app if it hasn't changed (poll_loop handles switches).
    """
    from blocklist import is_blocked

    _blocklist = blocklist or set()
    capture_mode = "on_switch"
    last_periodic = 0.0
    mode_poll_interval = 60  # poll setting every 60s
    check_interval = 10  # check timer every 10s

    async with httpx.AsyncClient(timeout=10.0) as client:
        last_mode_poll = 0.0
        while True:
            try:
                now = time.time()

                # Poll capture mode from backend every 60s
                if now - last_mode_poll >= mode_poll_interval:
                    capture_mode = await fetch_capture_mode(client, url)
                    last_mode_poll = now

                # Nothing to do in on_switch mode
                if capture_mode == "on_switch":
                    await asyncio.sleep(check_interval)
                    continue

                period = 300 if capture_mode == "balanced" else 60
                if now - last_periodic < period:
                    await asyncio.sleep(check_interval)
                    continue

                # Skip if user is idle
                idle_secs = get_idle_seconds()
                if idle_secs > idle_timeout:
                    await asyncio.sleep(check_interval)
                    continue

                # Get current app
                app_name = get_frontmost_app_name()
                if not app_name or is_blocked(app_name, _blocklist):
                    last_periodic = now
                    await asyncio.sleep(check_interval)
                    continue

                # Take periodic screenshot
                observation = {
                    "app": app_name,
                    "window_title": get_window_title() or "",
                }

                if ocr_provider is not None:
                    try:
                        from ocr.screenshot import capture_screen_png

                        png_bytes = await asyncio.to_thread(capture_screen_png)
                        if png_bytes:
                            result = await ocr_provider.analyze_screen(png_bytes, app_name)
                            if result.success:
                                observation.update(result.data)
                                if verbose:
                                    ts = time.strftime("%H:%M:%S")
                                    logger.info(
                                        "[%s] periodic (%s, %ds): %s",
                                        ts,
                                        capture_mode,
                                        period,
                                        result.data.get("summary", "")[:80],
                                    )
                            else:
                                logger.debug("Periodic analysis failed: %s", result.error)
                    except Exception:
                        logger.debug("Periodic screenshot/analysis error", exc_info=True)

                # Post to backend
                try:
                    payload: dict = {
                        "active_window": format_active_window(app_name, observation.get("window_title")),
                        "observation": observation,
                        "switch_timestamp": now,
                    }
                    await client.post(f"{url}/api/observer/context", json=payload)
                    if verbose:
                        ts = time.strftime("%H:%M:%S")
                        logger.info("[%s] periodic capture → %s", ts, app_name)
                except httpx.ConnectError:
                    logger.warning("Backend not reachable at %s — will retry", url)
                except httpx.HTTPError as exc:
                    logger.warning("HTTP error posting periodic capture: %s", exc)

                last_periodic = now

            except Exception:
                logger.exception("Unexpected error in periodic capture loop")

            await asyncio.sleep(check_interval)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Seraph macOS screen daemon")
    parser.add_argument(
        "--url",
        default="http://localhost:8004",
        help="Backend base URL (default: http://localhost:8004)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=5,
        help="Poll interval in seconds (default: 5)",
    )
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=300,
        help="Seconds of inactivity before marking idle (default: 300)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Log every context POST",
    )

    # OCR options
    parser.add_argument(
        "--ocr",
        action="store_true",
        default=False,
        help="Enable screenshot analysis on context switch (requires Screen Recording permission)",
    )
    parser.add_argument(
        "--ocr-provider",
        choices=["apple-vision", "openrouter"],
        default="apple-vision",
        help="OCR provider (default: apple-vision)",
    )
    parser.add_argument(
        "--ocr-interval",
        type=float,
        default=None,
        help="Deprecated — OCR now runs on context switch, not a timer. This flag is ignored.",
    )
    parser.add_argument(
        "--ocr-model",
        default="google/gemini-2.5-flash-lite",
        help="Model for OpenRouter OCR (default: google/gemini-2.5-flash-lite)",
    )
    parser.add_argument(
        "--openrouter-api-key",
        default=os.environ.get("OPENROUTER_API_KEY"),
        help="OpenRouter API key (or set OPENROUTER_API_KEY env var)",
    )
    parser.add_argument(
        "--blocklist-file",
        default=None,
        help="Path to JSON blocklist config (default: use built-in defaults only)",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        stream=sys.stdout,
    )
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    # Suppress noisy httpx/httpcore debug logs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    # Warn about deprecated flag
    if args.ocr_interval is not None:
        logger.warning(
            "--ocr-interval is deprecated — OCR now runs on context switch. This flag is ignored."
        )

    # Clean shutdown on signals
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _shutdown(sig: int) -> None:
        logger.info("Received %s — shutting down", signal.Signals(sig).name)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown, sig)

    # OCR + blocklist setup
    ocr_provider = None
    blocklist: set[str] = set()

    if args.ocr:
        from blocklist import load_blocklist
        from ocr import create_provider

        ocr_provider = create_provider(
            name=args.ocr_provider,
            api_key=args.openrouter_api_key,
            model=args.ocr_model,
        )

        if not ocr_provider.is_available():
            logger.error("OCR provider %r is not available — disabling OCR", args.ocr_provider)
            ocr_provider = None
        else:
            blocklist = load_blocklist(args.blocklist_file)
            logger.info(
                "OCR enabled — provider=%s, mode=on-context-switch, blocked=%d apps",
                ocr_provider.name,
                len(blocklist),
            )

    logger.info(
        "Seraph daemon started — polling every %gs, idle timeout %gs, backend %s",
        args.interval,
        args.idle_timeout,
        args.url,
    )

    async def heartbeat_loop(heartbeat_interval: float = 300) -> None:
        """Log a periodic heartbeat to confirm daemon is alive."""
        while True:
            await asyncio.sleep(heartbeat_interval)
            logger.info("heartbeat — daemon alive (poll every %gs)", args.interval)

    heartbeat_task = asyncio.create_task(heartbeat_loop())
    poll_task = asyncio.create_task(
        poll_loop(
            args.url,
            args.interval,
            args.idle_timeout,
            args.verbose,
            ocr_provider=ocr_provider,
            blocklist=blocklist,
        )
    )
    periodic_task = asyncio.create_task(
        periodic_capture_loop(
            args.url,
            args.idle_timeout,
            args.verbose,
            ocr_provider=ocr_provider,
            blocklist=blocklist,
        )
    ) if ocr_provider is not None else None

    await stop_event.wait()

    heartbeat_task.cancel()
    poll_task.cancel()
    if periodic_task is not None:
        periodic_task.cancel()

    tasks = [heartbeat_task, poll_task]
    if periodic_task is not None:
        tasks.append(periodic_task)
    for t in tasks:
        try:
            await t
        except asyncio.CancelledError:
            pass

    # Clean up provider resources
    if ocr_provider is not None and hasattr(ocr_provider, "close"):
        await ocr_provider.close()

    logger.info("Daemon stopped")


if __name__ == "__main__":
    asyncio.run(main())
