"""Legacy compatibility alias for the old `src.plugins.registry` import path."""

import sys

from src.native_tools import registry as _native_registry

sys.modules[__name__] = _native_registry
