"""Native tool discovery and metadata for bundled Python tools.

This package is the canonical home for Seraph's repo-local tool discovery.
It covers built-in trusted Python tools shipped inside `src/tools`, not the
typed extension platform described in the extension architecture docs.
"""

from .loader import discover_tools, reload_tools
from .registry import TOOL_METADATA, get_all_metadata, get_tool_metadata

__all__ = [
    "TOOL_METADATA",
    "discover_tools",
    "get_all_metadata",
    "get_tool_metadata",
    "reload_tools",
]
