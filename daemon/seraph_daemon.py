"""Seraph native macOS daemon — captures active window context and posts to backend.

Polls the frontmost application name and window title, posting changes to the
Seraph backend's /api/observer/context endpoint.  Runs natively on macOS
(outside Docker) and requires only the Accessibility permission for window titles.

Usage:
    python seraph_daemon.py [--url URL] [--interval SECS] [--idle-timeout SECS] [--verbose]
"""

import argparse
import asyncio
import logging
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


# ─── Main loop ────────────────────────────────────────────


async def poll_loop(
    url: str,
    interval: float,
    idle_timeout: float,
    verbose: bool,
) -> None:
    """Core polling loop — detect window changes, post to backend."""
    last_posted: str | None = None
    was_idle = False

    async with httpx.AsyncClient(timeout=5.0) as client:
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

                # Post to backend
                try:
                    await client.post(
                        f"{url}/api/observer/context",
                        json={
                            "active_window": active_window,
                            "screen_context": None,
                        },
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

    # Clean shutdown on signals
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _shutdown(sig: int) -> None:
        logger.info("Received %s — shutting down", signal.Signals(sig).name)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown, sig)

    logger.info(
        "Seraph daemon started — polling every %gs, idle timeout %gs, backend %s",
        args.interval,
        args.idle_timeout,
        args.url,
    )

    poll_task = asyncio.create_task(
        poll_loop(args.url, args.interval, args.idle_timeout, args.verbose)
    )

    await stop_event.wait()
    poll_task.cancel()
    try:
        await poll_task
    except asyncio.CancelledError:
        pass

    logger.info("Daemon stopped")


if __name__ == "__main__":
    asyncio.run(main())
