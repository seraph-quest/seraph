"""Screenshot capture via macOS Quartz APIs.

Captures the full screen as in-memory PNG bytes. Never writes to disk.
Requires Screen Recording permission (System Settings > Privacy & Security > Screen Recording).
"""

import logging

logger = logging.getLogger("seraph_daemon")

# Track whether we've already warned about missing permission
_warned_no_permission = False


def capture_screen_png() -> bytes | None:
    """Capture the full screen and return PNG bytes, or None on failure.

    Returns None if Screen Recording permission is not granted (logs warning once).
    """
    global _warned_no_permission

    try:
        import Quartz
        from AppKit import NSBitmapImageRep, NSPNGFileType

        # Capture the full screen
        image = Quartz.CGWindowListCreateImage(
            Quartz.CGRectInfinite,
            Quartz.kCGWindowListOptionOnScreenOnly,
            Quartz.kCGNullWindowID,
            Quartz.kCGWindowImageDefault,
        )

        if image is None:
            if not _warned_no_permission:
                logger.warning(
                    "Screen capture returned None — Screen Recording permission likely not granted. "
                    "Grant permission in System Settings > Privacy & Security > Screen Recording."
                )
                _warned_no_permission = True
            return None

        # Check if the image has meaningful content (permission denied returns a tiny image)
        width = Quartz.CGImageGetWidth(image)
        height = Quartz.CGImageGetHeight(image)
        if width < 100 or height < 100:
            if not _warned_no_permission:
                logger.warning(
                    "Screen capture returned a %dx%d image — Screen Recording permission likely not granted.",
                    width,
                    height,
                )
                _warned_no_permission = True
            return None

        # Reset warning flag on success
        _warned_no_permission = False

        # Convert CGImage → PNG bytes via NSBitmapImageRep
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
        logger.exception("Unexpected error capturing screenshot")
        return None
