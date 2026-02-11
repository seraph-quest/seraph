"""Screen context source — native daemon integration for screen awareness.

The Seraph native daemon (a lightweight macOS/Linux process running outside Docker)
captures screen context and posts it to the backend. This module documents the
API contract for that integration.

## Daemon API Contract

### Endpoint
    POST /api/observer/context

### Request Body
    {
        "active_window": str,       # Window title + app name
                                    # e.g. "VS Code — seraph/backend/src/agent/strategist.py"
        "screen_context": str,      # Brief description of what the user appears to be doing
                                    # e.g. "User is writing Python code in the strategist module"
    }

### Field Details

- **active_window** (required): The currently focused application and window title.
  Format: "{App Name} — {window/document title}". The backend uses this to derive
  user state (e.g., detecting IDE = deep_work, calendar app = scheduling).

- **screen_context** (optional): A human-readable summary of what's on screen.
  The daemon may use local OCR or window metadata to generate this. Should be
  1-2 sentences max. Used by the strategist for richer context.

### Frequency
The daemon should POST every 30-60 seconds while the user is active.
If the active window hasn't changed, the daemon may skip the POST to reduce
noise. The backend does NOT poll — it relies on the daemon to push updates.

### Privacy
- Screen context stays local (backend runs on localhost or user's Docker).
- The daemon should NEVER capture screenshots or send pixel data.
- Only window titles and inferred activity descriptions are transmitted.
- The user can disable screen context in settings (daemon should respect this).

### Backend Handler
The POST endpoint is handled in `src/api/router.py` and calls:
    context_manager.update_screen_context(active_window, screen_context)

The data is then included in the next context refresh and available to the
strategist agent and delivery gate for state derivation.

## Implementation Status
The daemon is deferred to a future phase. The backend endpoint and
ContextManager.update_screen_context() method are already implemented and
ready to receive data when the daemon is built.
"""
