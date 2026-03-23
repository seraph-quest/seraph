"""Dynamic native tool loader for bundled `src/tools/*.py` modules."""

import importlib
import logging
import pkgutil
from pathlib import Path

from smolagents import Tool

logger = logging.getLogger(__name__)

_discovered_tools: list | None = None


def discover_tools() -> list:
    """Scan bundled tool modules and collect all smolagents Tool instances."""
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
                logger.debug(f"Discovered native tool: {obj.name} from {module_info.name}")

    logger.info(f"Auto-discovered {len(tools)} native tools from {tools_dir}")
    _discovered_tools = tools
    return tools


def reload_tools() -> list:
    """Force a re-scan of the bundled tools directory."""
    global _discovered_tools
    _discovered_tools = None
    return discover_tools()
