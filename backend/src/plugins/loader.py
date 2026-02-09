"""Dynamic tool loader — auto-discovers @tool functions from src/tools/*.py."""

import importlib
import logging
import pkgutil
from pathlib import Path

from smolagents import Tool

logger = logging.getLogger(__name__)

# Cache discovered tools
_discovered_tools: list | None = None


def discover_tools() -> list:
    """Scan src/tools/*.py and collect all smolagents Tool instances.

    Looks for module-level objects that are instances of smolagents.Tool
    (which includes @tool decorated functions).
    """
    global _discovered_tools
    if _discovered_tools is not None:
        return _discovered_tools

    tools_dir = Path(__file__).parent.parent / "tools"
    tools = []
    seen_names: set[str] = set()

    for module_info in pkgutil.iter_modules([str(tools_dir)]):
        if module_info.name.startswith("_"):
            continue
        if module_info.name == "mcp_manager":
            continue

        try:
            module = importlib.import_module(f"src.tools.{module_info.name}")
        except Exception:
            logger.exception(f"Failed to import tool module: {module_info.name}")
            continue

        for attr_name in dir(module):
            obj = getattr(module, attr_name)
            if isinstance(obj, Tool) and obj.name not in seen_names:
                tools.append(obj)
                seen_names.add(obj.name)
                logger.debug(f"Discovered tool: {obj.name} from {module_info.name}")

    logger.info(f"Auto-discovered {len(tools)} tools from {tools_dir}")
    _discovered_tools = tools
    return tools


def reload_tools() -> list:
    """Force re-scan of tools directory."""
    global _discovered_tools
    _discovered_tools = None
    return discover_tools()
