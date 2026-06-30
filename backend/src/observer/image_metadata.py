"""Local image metadata helpers for Seraph-owned screenshot analysis."""

from __future__ import annotations

import os
import struct
from pathlib import Path
from typing import Any


def local_image_metadata(path: Path) -> dict[str, Any]:
    """Return local image facts derived from the image file itself."""
    image_bytes = path.stat().st_size
    dimensions = image_dimensions(path)
    file_format = path.suffix.lower().lstrip(".") or "unknown"
    if file_format == "jpg":
        file_format = "jpeg"
    return {
        "image_bytes": image_bytes,
        "file_format": file_format,
        "width": dimensions.get("width"),
        "height": dimensions.get("height"),
    }


def image_dimensions(path: Path) -> dict[str, int | None]:
    try:
        with path.open("rb") as handle:
            header = handle.read(32)
            if header.startswith(b"\x89PNG\r\n\x1a\n") and len(header) >= 24:
                width, height = struct.unpack(">II", header[16:24])
                return {"width": int(width), "height": int(height)}
            if header.startswith(b"\xff\xd8"):
                return _jpeg_dimensions(path)
    except OSError:
        pass
    return {"width": None, "height": None}


def _jpeg_dimensions(path: Path) -> dict[str, int | None]:
    try:
        with path.open("rb") as handle:
            handle.read(2)
            while True:
                marker_prefix = handle.read(1)
                if marker_prefix == b"":
                    break
                if marker_prefix != b"\xff":
                    continue
                marker = handle.read(1)
                while marker == b"\xff":
                    marker = handle.read(1)
                if marker in {b"\xc0", b"\xc1", b"\xc2", b"\xc3"}:
                    segment_length = int.from_bytes(handle.read(2), "big")
                    if segment_length < 7:
                        break
                    handle.read(1)
                    height = int.from_bytes(handle.read(2), "big")
                    width = int.from_bytes(handle.read(2), "big")
                    return {"width": width, "height": height}
                if marker in {b"\xd8", b"\xd9"}:
                    continue
                segment_length = int.from_bytes(handle.read(2), "big")
                if segment_length < 2:
                    break
                handle.seek(segment_length - 2, os.SEEK_CUR)
    except OSError:
        pass
    return {"width": None, "height": None}


def image_metadata_label(metadata: dict[str, Any]) -> str:
    """Format image facts for observation summaries and reports."""
    parts: list[str] = []
    file_format = str(metadata.get("file_format") or "").strip()
    if file_format:
        parts.append(file_format)
    width = metadata.get("width")
    height = metadata.get("height")
    if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
        parts.append(f"{width}x{height}")
    image_bytes = metadata.get("image_bytes")
    if isinstance(image_bytes, int) and image_bytes >= 0:
        parts.append(_format_bytes(image_bytes))
    return ", ".join(parts)


def _format_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value / (1024 * 1024):.1f} MB"
