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
import hashlib
import contextlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

import httpx

logger = logging.getLogger("seraph_daemon")


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip().lower()).strip("-")
    return slug[:40] or "screen"


def _archive_provider_capture(
    *,
    archive_dir: str,
    png_bytes: bytes,
    app_name: str,
    provider_name: str,
    analysis: dict,
) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    root = Path(archive_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        root.chmod(0o700)
    day_dir = root / now.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        day_dir.chmod(0o700)
    analysis_json = json.dumps(analysis, indent=2, sort_keys=True)
    digest = hashlib.sha256(
        png_bytes + provider_name.encode("utf-8") + analysis_json.encode("utf-8")
    ).hexdigest()[:16]
    stem = f"{now.strftime('%H%M%S')}-{_slug(app_name)}-{digest}"
    image_path = day_dir / f"{stem}.png"
    provider_output_path = day_dir / f"{stem}.{_slug(provider_name)}.json"
    analysis_path = day_dir / f"{stem}.analysis.json"
    provider_payload = {
        "artifact_schema": "seraph.screen_analysis.v1",
        "provider": provider_name,
        "app": app_name,
        "created_at": now.isoformat(),
        "analysis": analysis,
    }
    _write_private_bytes(image_path, png_bytes)
    _write_private_text(provider_output_path, json.dumps(provider_payload, indent=2, sort_keys=True))
    _write_private_text(analysis_path, analysis_json)
    return {
        "id": digest,
        "provider": provider_name,
        "image_path": str(image_path),
        "provider_output_path": str(provider_output_path),
        "analysis_path": str(analysis_path),
        "created_at": now.isoformat(),
    }


def _write_private_bytes(path: Path, content: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
    finally:
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)


def _write_private_text(path: Path, content: str) -> None:
    _write_private_bytes(path, content.encode("utf-8"))


def _daemon_status_path() -> Path | None:
    configured = os.environ.get("SERAPH_DAEMON_STATUS_FILE", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    workspace_dir = os.environ.get("WORKSPACE_DIR", "").strip()
    if workspace_dir:
        return Path(workspace_dir).expanduser().resolve() / "daemon-status.json"
    return None


def _write_daemon_status(**updates: object) -> None:
    status_path = _daemon_status_path()
    if status_path is None:
        return
    try:
        status_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload: dict[str, object] = {}
        if status_path.exists():
            try:
                existing = json.loads(status_path.read_text(encoding="utf-8"))
                if isinstance(existing, dict):
                    payload.update(existing)
            except (OSError, json.JSONDecodeError):
                payload = {}
        payload.update(updates)
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        _write_private_text(status_path, json.dumps(payload, indent=2, sort_keys=True))
    except Exception:
        logger.debug("Failed to write daemon status", exc_info=True)

# ─── macOS helpers ────────────────────────────────────────


def _get_frontmost_app_name_from_system_events() -> str | None:
    try:
        result = subprocess.run(
            [
                "osascript",
                "-e",
                'tell application "System Events" to get name of first application process whose frontmost is true',
            ],
            capture_output=True,
            text=True,
            timeout=3,
        )
        app_name = result.stdout.strip()
        if app_name:
            return app_name
        if result.stderr.strip():
            logger.debug("System Events frontmost app lookup failed: %s", result.stderr.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    except Exception:
        logger.debug("System Events frontmost app lookup failed", exc_info=True)
    return None


def _get_frontmost_app_name_from_workspace() -> str | None:
    try:
        from AppKit import NSWorkspace

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        app_name = app.localizedName() if app else None
        if app_name:
            return app_name
    except Exception:
        logger.debug("NSWorkspace call failed", exc_info=True)
    return None


def get_frontmost_app_name() -> str | None:
    """Return the visible foreground app name from the same source as window title.

    System Events tracks the Accessibility-observed foreground process that Seraph
    also uses for the active window title. NSWorkspace is kept as a fallback, but
    should not mask app switches with a stale value.
    """
    return _get_frontmost_app_name_from_system_events() or _get_frontmost_app_name_from_workspace()


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


def _escape_applescript(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def show_notification(title: str, body: str) -> bool:
    """Display a macOS notification via AppleScript."""
    try:
        result = subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification "{_escape_applescript(body)}" with title "{_escape_applescript(title)}"',
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    except Exception:
        logger.debug("Failed to display native notification", exc_info=True)
        return False


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


async def fetch_screen_analysis_settings(client: httpx.AsyncClient, url: str) -> dict | None:
    """Fetch screen-analysis runtime settings from backend."""
    try:
        r = await client.get(f"{url}/api/settings/screen-analysis")
        if r.status_code != 200:
            return None
        payload = r.json()
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


async def fetch_next_notification(client: httpx.AsyncClient, url: str) -> dict | None:
    """Fetch the next pending native notification from the backend."""
    try:
        r = await client.get(f"{url}/api/observer/notifications/next")
        if r.status_code == 200:
            return r.json().get("notification")
    except Exception:
        pass
    return None


async def ack_notification(client: httpx.AsyncClient, url: str, notification_id: str) -> bool:
    """Acknowledge a displayed native notification."""
    try:
        r = await client.post(f"{url}/api/observer/notifications/{notification_id}/ack")
        if r.status_code == 200:
            return bool(r.json().get("acked"))
    except Exception:
        pass
    return False


class ScreenAnalysisRuntime:
    """Runtime screen-analysis config that follows backend Settings."""

    def __init__(
        self,
        *,
        enabled: bool,
        provider: str,
        model: str | None,
        preserve_captures: bool,
        archive_dir: str,
        openrouter_api_key: str | None,
        blocklist_file: str | None,
    ) -> None:
        self._fallback = {
            "enabled": enabled,
            "provider": provider,
            "model": model or "",
            "preserve_captures": preserve_captures,
            "archive_dir": archive_dir,
        }
        self._openrouter_api_key = openrouter_api_key
        self._blocklist_file = blocklist_file
        self._signature: tuple[object, ...] | None = None
        self._last_refresh = 0.0
        self.provider = None
        self.blocklist: set[str] = set()
        self.enabled = False
        self.preserve_captures = False
        self.archive_dir = archive_dir

    async def refresh(self, client: httpx.AsyncClient, url: str, *, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_refresh < 30:
            return
        self._last_refresh = now
        payload = await fetch_screen_analysis_settings(client, url) or self._fallback
        enabled = bool(payload.get("enabled", self._fallback["enabled"]))
        provider_name = str(payload.get("provider") or self._fallback["provider"])
        if provider_name not in {"apple-vision", "openrouter", "codex-local"}:
            provider_name = str(self._fallback["provider"])
        model = str(payload.get("model") or self._fallback["model"] or "")
        preserve = bool(payload.get("preserve_captures", self._fallback["preserve_captures"]))
        archive_dir = str(payload.get("archive_dir") or self._fallback["archive_dir"])
        signature = (enabled, provider_name, model, preserve, archive_dir)
        if signature == self._signature:
            return

        if not enabled:
            old_provider = self.provider
            self.provider = None
            self.enabled = False
            self.preserve_captures = preserve
            self.archive_dir = archive_dir
            self._signature = signature
            if old_provider is not None and hasattr(old_provider, "close"):
                await old_provider.close()
            logger.info("Screen analysis disabled by settings")
            _write_daemon_status(
                state="running",
                screen_analysis="disabled",
                provider=provider_name,
                capture_ready=False,
                last_error=None,
                last_error_kind=None,
            )
            return

        from blocklist import load_blocklist
        from ocr import create_provider

        provider_model = model or None
        if provider_model is None and provider_name == "openrouter":
            provider_model = "google/gemini-2.5-flash-lite"
        try:
            provider = create_provider(
                name=provider_name,
                api_key=self._openrouter_api_key,
                model=provider_model,
                preserve_captures=preserve,
                archive_dir=archive_dir,
            )
        except Exception as exc:
            logger.error("Failed to configure OCR provider %r from settings: %s", provider_name, exc)
            old_provider = self.provider
            self.provider = None
            self.enabled = False
            self.preserve_captures = preserve
            self.archive_dir = archive_dir
            if old_provider is not None and hasattr(old_provider, "close"):
                await old_provider.close()
            _write_daemon_status(
                state="running",
                screen_analysis="provider_error",
                provider=provider_name,
                capture_ready=False,
                last_error=f"Failed to configure provider: {exc}",
                last_error_kind="provider_configuration",
            )
            return
        if not provider.is_available():
            logger.error("OCR provider %r is not available — screen analysis paused", provider_name)
            if hasattr(provider, "close"):
                await provider.close()
            old_provider = self.provider
            self.provider = None
            self.enabled = False
            self.preserve_captures = preserve
            self.archive_dir = archive_dir
            if old_provider is not None and hasattr(old_provider, "close"):
                await old_provider.close()
            _write_daemon_status(
                state="running",
                screen_analysis="provider_unavailable",
                provider=provider_name,
                capture_ready=False,
                last_error=f"OCR provider '{provider_name}' is not available.",
                last_error_kind="provider_unavailable",
            )
            return

        old_provider = self.provider
        if old_provider is not None and old_provider is not provider and hasattr(old_provider, "close"):
            await old_provider.close()
        self.provider = provider
        self.enabled = True
        self.preserve_captures = preserve
        self.archive_dir = archive_dir
        self._signature = signature
        self.blocklist = load_blocklist(self._blocklist_file)
        logger.info(
            "Screen analysis settings active — provider=%s, preserve=%s, archive=%s, blocked=%d apps",
            provider.name,
            preserve,
            archive_dir,
            len(self.blocklist),
        )
        _write_daemon_status(
            state="running",
            screen_analysis="active",
            provider=provider.name,
            preserve_captures=preserve,
            archive_dir=archive_dir,
            capture_ready=True,
            last_error=None,
            last_error_kind=None,
        )

    async def close(self) -> None:
        if self.provider is not None and hasattr(self.provider, "close"):
            await self.provider.close()


# ─── Main loop ────────────────────────────────────────────


async def poll_loop(
    url: str,
    interval: float,
    idle_timeout: float,
    verbose: bool,
    screen_runtime: ScreenAnalysisRuntime | None = None,
) -> None:
    """Core polling loop — detect window changes, post to backend.

    When Settings enables screen analysis, captures and analyzes the screen on
    each context switch. Blocked apps are never screenshotted.
    """
    last_posted: str | None = None
    was_idle = False
    shown_notification_ids: set[str] = set()

    async with httpx.AsyncClient(timeout=10.0) as client:
        if screen_runtime is not None:
            await screen_runtime.refresh(client, url, force=True)
        while True:
            try:
                if screen_runtime is not None:
                    await screen_runtime.refresh(client, url)
                notification = await fetch_next_notification(client, url)
                if notification is not None:
                    notification_id = notification.get("id")
                    if notification_id in shown_notification_ids:
                        acked = await ack_notification(client, url, notification_id)
                        if acked:
                            shown_notification_ids.discard(notification_id)
                    else:
                        displayed = await asyncio.to_thread(
                            show_notification,
                            notification.get("title", "Seraph"),
                            notification.get("body", ""),
                        )
                        if displayed:
                            if notification_id:
                                shown_notification_ids.add(notification_id)
                            acked = await ack_notification(client, url, notification["id"])
                            if acked and notification_id:
                                shown_notification_ids.discard(notification_id)
                            if verbose:
                                ts = time.strftime("%H:%M:%S")
                                logger.info("[%s] notification \u2192 %s", ts, notification.get("title", "Seraph"))

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
                poll_at = datetime.now(timezone.utc).isoformat()

                if not active_window:
                    _write_daemon_status(
                        state="running",
                        screen_analysis="frontmost_unavailable" if screen_runtime is not None else "window_unavailable",
                        capture_ready=False,
                        active_window=None,
                        frontmost_app=None,
                        window_title=None,
                        last_poll_at=poll_at,
                        last_error=(
                            "Seraph cannot read the frontmost application, so on-switch screen capture is paused. "
                            "Grant Accessibility/Automation permission to the terminal/app running Seraph, then restart."
                        ),
                        last_error_kind="frontmost_app_unavailable",
                    )
                    await asyncio.sleep(interval)
                    continue

                # Skip if unchanged
                if active_window == last_posted:
                    _write_daemon_status(
                        state="running",
                        active_window=active_window,
                        frontmost_app=app_name,
                        window_title=window_title,
                        last_poll_at=poll_at,
                    )
                    await asyncio.sleep(interval)
                    continue

                # Build observation payload on context switch
                observation = None
                if screen_runtime is not None and screen_runtime.provider is not None and app_name:
                    observation = {
                        "app": app_name,
                        "window_title": window_title or "",
                    }

                    from blocklist import is_blocked

                    if is_blocked(app_name, screen_runtime.blocklist):
                        observation["blocked"] = True
                        if verbose:
                            logger.info("Blocked app: %s — skipping screenshot", app_name)
                    else:
                        try:
                            from ocr.screenshot import capture_screen_png

                            png_bytes = await asyncio.to_thread(capture_screen_png)
                            if not png_bytes:
                                observation["capture_error"] = (
                                    "macOS screen capture returned no image. Grant Screen Recording / "
                                    "Screen & System Audio Recording permission to the terminal/app running Seraph, "
                                    "then restart the daemon."
                                )
                                observation["capture_error_kind"] = "screen_capture_permission"
                                _write_daemon_status(
                                    state="running",
                                    screen_analysis="capture_error",
                                    provider=screen_runtime.provider.name,
                                    capture_ready=False,
                                    active_window=active_window,
                                    frontmost_app=app_name,
                                    window_title=window_title,
                                    last_poll_at=poll_at,
                                    last_error=observation["capture_error"],
                                    last_error_kind="screen_capture_permission",
                                )
                            if png_bytes:
                                result = await screen_runtime.provider.analyze_screen(png_bytes, app_name)
                                if result.success:
                                    observation.update(result.data)
                                    if screen_runtime.preserve_captures and "capture_artifacts" not in observation:
                                        observation["capture_artifacts"] = _archive_provider_capture(
                                            archive_dir=screen_runtime.archive_dir,
                                            png_bytes=png_bytes,
                                            app_name=app_name,
                                            provider_name=screen_runtime.provider.name,
                                            analysis=result.data,
                                        )
                                    if verbose:
                                        ts = time.strftime("%H:%M:%S")
                                        logger.info(
                                            "[%s] analyzed (%dms): %s",
                                            ts,
                                            result.duration_ms,
                                            result.data.get("summary", "")[:80],
                                        )
                                    _write_daemon_status(
                                        state="running",
                                        screen_analysis="active",
                                        provider=screen_runtime.provider.name,
                                        capture_ready=True,
                                        active_window=active_window,
                                        frontmost_app=app_name,
                                        window_title=window_title,
                                        last_capture_at=datetime.now(timezone.utc).isoformat(),
                                        last_poll_at=poll_at,
                                        last_error=None,
                                        last_error_kind=None,
                                    )
                                else:
                                    logger.debug("Screen analysis failed: %s", result.error)
                                    observation["capture_error"] = result.error or "Screen analysis failed."
                                    observation["capture_error_kind"] = "analysis_error"
                                    _write_daemon_status(
                                        state="running",
                                        screen_analysis="analysis_error",
                                        provider=screen_runtime.provider.name,
                                        capture_ready=False,
                                        last_error=observation["capture_error"],
                                        last_error_kind="analysis_error",
                                    )
                        except Exception:
                            logger.debug("Screenshot/analysis error", exc_info=True)
                            observation["capture_error"] = "Screenshot or screen analysis failed unexpectedly."
                            observation["capture_error_kind"] = "analysis_exception"
                            _write_daemon_status(
                                state="running",
                                screen_analysis="analysis_error",
                                provider=screen_runtime.provider.name,
                                capture_ready=False,
                                active_window=active_window,
                                frontmost_app=app_name,
                                window_title=window_title,
                                last_poll_at=poll_at,
                                last_error=observation["capture_error"],
                                last_error_kind="analysis_exception",
                            )

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
                    capture_error = (
                        str(observation.get("capture_error"))
                        if observation is not None and observation.get("capture_error")
                        else None
                    )
                    capture_error_kind = (
                        str(observation.get("capture_error_kind"))
                        if observation is not None and observation.get("capture_error_kind")
                        else "capture_error"
                    )
                    status_update = {
                        "state": "running",
                        "active_window": active_window,
                        "frontmost_app": app_name,
                        "window_title": window_title,
                        "capture_ready": bool(
                            observation is not None
                            and not observation.get("blocked")
                            and capture_error is None
                        ),
                        "last_context_post_at": datetime.now(timezone.utc).isoformat(),
                        "last_poll_at": poll_at,
                    }
                    if capture_error:
                        status_update.update(
                            {
                                "screen_analysis": "analysis_error"
                                if capture_error_kind.startswith("analysis")
                                else "capture_error",
                                "last_error": capture_error,
                                "last_error_kind": capture_error_kind,
                            }
                        )
                    _write_daemon_status(
                        **status_update
                    )
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
    screen_runtime: ScreenAnalysisRuntime | None = None,
) -> None:
    """Periodic capture loop — takes screenshots at intervals within the same app.

    Runs alongside poll_loop. Polls backend for capture_mode every 60s.
    When mode is 'balanced' (300s) or 'detailed' (60s), captures periodic
    screenshots of the current app if it hasn't changed (poll_loop handles switches).
    """
    capture_mode = "on_switch"
    last_periodic = 0.0
    mode_poll_interval = 60  # poll setting every 60s
    check_interval = 10  # check timer every 10s

    async with httpx.AsyncClient(timeout=10.0) as client:
        if screen_runtime is not None:
            await screen_runtime.refresh(client, url, force=True)
        last_mode_poll = 0.0
        while True:
            try:
                if screen_runtime is not None:
                    await screen_runtime.refresh(client, url)
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
                poll_at = datetime.now(timezone.utc).isoformat()
                if not app_name:
                    _write_daemon_status(
                        state="running",
                        screen_analysis="frontmost_unavailable",
                        capture_ready=False,
                        active_window=None,
                        frontmost_app=None,
                        window_title=None,
                        last_poll_at=poll_at,
                        last_error=(
                            "Seraph cannot read the frontmost application, so periodic screen capture is paused. "
                            "Grant Accessibility/Automation permission to the terminal/app running Seraph, then restart."
                        ),
                        last_error_kind="frontmost_app_unavailable",
                    )
                    last_periodic = now
                    await asyncio.sleep(check_interval)
                    continue
                from blocklist import is_blocked

                if screen_runtime is None or screen_runtime.provider is None or is_blocked(app_name, screen_runtime.blocklist):
                    last_periodic = now
                    await asyncio.sleep(check_interval)
                    continue

                # Take periodic screenshot
                window_title = get_window_title() or ""
                active_window = format_active_window(app_name, window_title)
                observation = {
                    "app": app_name,
                    "window_title": window_title,
                }

                try:
                    from ocr.screenshot import capture_screen_png

                    png_bytes = await asyncio.to_thread(capture_screen_png)
                    if not png_bytes:
                        observation["capture_error"] = (
                            "macOS screen capture returned no image. Grant Screen Recording / "
                            "Screen & System Audio Recording permission to the terminal/app running Seraph, "
                            "then restart the daemon."
                        )
                        _write_daemon_status(
                            state="running",
                            screen_analysis="capture_error",
                            provider=screen_runtime.provider.name,
                            capture_ready=False,
                            last_error=observation["capture_error"],
                            last_error_kind="screen_capture_permission",
                        )
                    if png_bytes:
                        result = await screen_runtime.provider.analyze_screen(png_bytes, app_name)
                        if result.success:
                            observation.update(result.data)
                            if screen_runtime.preserve_captures and "capture_artifacts" not in observation:
                                observation["capture_artifacts"] = _archive_provider_capture(
                                    archive_dir=screen_runtime.archive_dir,
                                    png_bytes=png_bytes,
                                    app_name=app_name,
                                    provider_name=screen_runtime.provider.name,
                                    analysis=result.data,
                                )
                            if verbose:
                                ts = time.strftime("%H:%M:%S")
                                logger.info(
                                    "[%s] periodic (%s, %ds): %s",
                                    ts,
                                    capture_mode,
                                    period,
                                    result.data.get("summary", "")[:80],
                                )
                            _write_daemon_status(
                                state="running",
                                screen_analysis="active",
                                provider=screen_runtime.provider.name,
                                capture_ready=True,
                                active_window=active_window,
                                frontmost_app=app_name,
                                window_title=window_title,
                                last_capture_at=datetime.now(timezone.utc).isoformat(),
                                last_poll_at=poll_at,
                                last_error=None,
                                last_error_kind=None,
                            )
                        else:
                            logger.debug("Periodic analysis failed: %s", result.error)
                            observation["capture_error"] = result.error or "Periodic screen analysis failed."
                            _write_daemon_status(
                                state="running",
                                screen_analysis="analysis_error",
                                provider=screen_runtime.provider.name,
                                capture_ready=False,
                                last_error=observation["capture_error"],
                                last_error_kind="analysis_error",
                            )
                except Exception:
                    logger.debug("Periodic screenshot/analysis error", exc_info=True)
                    observation["capture_error"] = "Periodic screenshot or screen analysis failed unexpectedly."
                    _write_daemon_status(
                        state="running",
                        screen_analysis="analysis_error",
                        provider=screen_runtime.provider.name,
                        capture_ready=False,
                        active_window=active_window,
                        frontmost_app=app_name,
                        window_title=window_title,
                        last_poll_at=poll_at,
                        last_error=observation["capture_error"],
                        last_error_kind="analysis_exception",
                    )

                # Post to backend
                try:
                    payload: dict = {
                        "active_window": active_window,
                        "observation": observation,
                        "switch_timestamp": now,
                    }
                    await client.post(f"{url}/api/observer/context", json=payload)
                    if verbose:
                        ts = time.strftime("%H:%M:%S")
                        logger.info("[%s] periodic capture → %s", ts, app_name)
                    _write_daemon_status(
                        state="running",
                        active_window=active_window,
                        frontmost_app=app_name,
                        window_title=window_title,
                        capture_ready=bool(
                            not observation.get("blocked") and "capture_error" not in observation
                        ),
                        last_context_post_at=datetime.now(timezone.utc).isoformat(),
                        last_poll_at=poll_at,
                    )
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
        choices=["apple-vision", "openrouter", "codex-local"],
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
        default=None,
        help="Model for OCR provider (OpenRouter default: google/gemini-2.5-flash-lite; codex-local default: gpt-5.5)",
    )
    parser.add_argument(
        "--preserve-captures",
        action="store_true",
        default=os.environ.get("SERAPH_PRESERVE_SCREEN_CAPTURES", "").strip().lower()
        in {"1", "true", "yes", "on"},
        help="Preserve allowed screenshots, local Codex output, and normalized analysis artifacts",
    )
    parser.add_argument(
        "--capture-archive-dir",
        default=os.environ.get("SERAPH_SCREEN_CAPTURE_ARCHIVE_DIR")
        or os.environ.get("SCREEN_CAPTURE_ARCHIVE_DIR")
        or "~/Library/Application Support/Seraph/artifacts/screen-captures",
        help="Directory for preserved capture artifacts (default: ~/Library/Application Support/Seraph/artifacts/screen-captures)",
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

    screen_runtime = ScreenAnalysisRuntime(
        enabled=args.ocr,
        provider=args.ocr_provider,
        model=args.ocr_model,
        preserve_captures=args.preserve_captures,
        archive_dir=args.capture_archive_dir,
        openrouter_api_key=args.openrouter_api_key,
        blocklist_file=args.blocklist_file,
    )

    logger.info(
        "Seraph daemon started — polling every %gs, idle timeout %gs, backend %s",
        args.interval,
        args.idle_timeout,
        args.url,
    )
    _write_daemon_status(
        state="running",
        backend_url=args.url,
        poll_interval=args.interval,
        idle_timeout=args.idle_timeout,
        screen_analysis="starting",
        capture_ready=False,
        last_error=None,
        last_error_kind=None,
    )

    async def heartbeat_loop(heartbeat_interval: float = 15) -> None:
        """Log a periodic heartbeat to confirm daemon is alive."""
        while True:
            await asyncio.sleep(heartbeat_interval)
            _write_daemon_status(state="running")
            logger.info("heartbeat — daemon alive (poll every %gs)", args.interval)

    heartbeat_task = asyncio.create_task(heartbeat_loop())
    poll_task = asyncio.create_task(
        poll_loop(
            args.url,
            args.interval,
            args.idle_timeout,
            args.verbose,
            screen_runtime=screen_runtime,
        )
    )
    periodic_task = asyncio.create_task(
        periodic_capture_loop(
            args.url,
            args.idle_timeout,
            args.verbose,
            screen_runtime=screen_runtime,
        )
    )

    await stop_event.wait()

    heartbeat_task.cancel()
    poll_task.cancel()
    periodic_task.cancel()

    tasks = [heartbeat_task, poll_task, periodic_task]
    for t in tasks:
        try:
            await t
        except asyncio.CancelledError:
            pass

    await screen_runtime.close()

    logger.info("Daemon stopped")
    _write_daemon_status(state="stopped", capture_ready=False, screen_analysis="stopped")


if __name__ == "__main__":
    asyncio.run(main())
