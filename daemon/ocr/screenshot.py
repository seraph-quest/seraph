"""Screenshot capture via macOS APIs.

Captures the full screen as in-memory PNG bytes.
Requires Screen Recording permission (System Settings > Privacy & Security > Screen Recording).
"""

import logging
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger("seraph_daemon")

# Track whether we've already warned about missing permission
_warned_no_permission = False


def capture_screen_png() -> bytes | None:
    """Capture the full screen and return PNG bytes, or None on failure.

    Uses macOS `screencapture` CLI for maximum compatibility with Sequoia+
    and Tahoe permission models. Falls back to Quartz API if screencapture
    is unavailable. Screenshot file is deleted immediately after reading.

    Returns None if Screen Recording permission is not granted (logs warning once).
    """
    global _warned_no_permission

    png_bytes = _capture_via_screencapture()
    if png_bytes is None:
        png_bytes = _capture_via_quartz()

    if png_bytes is None:
        return None

    # Sanity check: a valid full-screen PNG should be at least a few KB.
    if len(png_bytes) < 5000:
        if not _warned_no_permission:
            logger.warning(
                "Screenshot PNG suspiciously small (%d bytes) — "
                "Screen Recording permission may not be fully active.",
                len(png_bytes),
            )
            _warned_no_permission = True
        return None

    _warned_no_permission = False
    return png_bytes


def _capture_via_screencapture() -> bytes | None:
    """Capture using macOS screencapture CLI — most reliable on newer macOS."""
    global _warned_no_permission

    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        # -x: no sound, -C: capture cursor, -t png: format
        result = subprocess.run(
            ["screencapture", "-x", "-C", "-t", "png", str(tmp_path)],
            capture_output=True,
            timeout=5,
        )

        if result.returncode != 0:
            logger.debug("screencapture exited with code %d", result.returncode)
            tmp_path.unlink(missing_ok=True)
            return None

        if not tmp_path.exists() or tmp_path.stat().st_size == 0:
            tmp_path.unlink(missing_ok=True)
            return None

        png_bytes = tmp_path.read_bytes()
        tmp_path.unlink(missing_ok=True)
        return png_bytes

    except FileNotFoundError:
        logger.debug("screencapture command not found")
        return None
    except subprocess.TimeoutExpired:
        logger.warning("screencapture timed out")
        tmp_path.unlink(missing_ok=True)
        return None
    except Exception:
        logger.debug("screencapture failed", exc_info=True)
        return None


def _capture_via_quartz() -> bytes | None:
    """Fallback capture using Quartz CGDisplayCreateImage."""
    global _warned_no_permission

    try:
        import Quartz
        from AppKit import NSBitmapImageRep, NSPNGFileType

        main_display = Quartz.CGMainDisplayID()
        image = Quartz.CGDisplayCreateImage(main_display)

        if image is None:
            if not _warned_no_permission:
                logger.warning(
                    "Screen capture returned None — Screen Recording permission likely not granted. "
                    "Grant permission in System Settings > Privacy & Security > Screen Recording."
                )
                _warned_no_permission = True
            return None

        width = Quartz.CGImageGetWidth(image)
        height = Quartz.CGImageGetHeight(image)
        if width < 100 or height < 100:
            if not _warned_no_permission:
                logger.warning(
                    "Screen capture returned a %dx%d image — Screen Recording permission "
                    "likely not granted.",
                    width,
                    height,
                )
                _warned_no_permission = True
            return None

        bitmap = NSBitmapImageRep.alloc().initWithCGImage_(image)
        png_data = bitmap.representationUsingType_properties_(NSPNGFileType, {})

        if png_data is None:
            logger.warning("Failed to convert screenshot to PNG")
            return None

        return bytes(png_data)

    except ImportError:
        logger.error("Quartz/AppKit not available — are you running on macOS with PyObjC?")
        return None
    except Exception:
        logger.exception("Unexpected error in Quartz capture")
        return None
