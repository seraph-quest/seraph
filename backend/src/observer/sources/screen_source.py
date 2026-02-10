"""Screen context source — stub for Phase 3.4 native daemon integration.

The native daemon will POST screen context to:
    POST /api/observer/context
    Body: {
        "active_window": "VS Code — seraph/backend/src/observer/manager.py",
        "screen_context": "User is editing Python code in observer module"
    }

The ContextManager stores this data via update_screen_context() and
includes it in the next context snapshot.
"""
