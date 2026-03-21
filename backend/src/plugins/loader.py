"""Legacy compatibility alias for the old `src.plugins.loader` import path."""

import sys

from src.native_tools import loader as _native_loader

sys.modules[__name__] = _native_loader
