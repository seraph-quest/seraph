"""App blocklist for screen capture — prevents screenshots of sensitive apps."""

import json
import logging

logger = logging.getLogger("seraph_daemon")

DEFAULT_BLOCKLIST: set[str] = {
    # Password managers
    "1password",
    "bitwarden",
    "lastpass",
    "keepassxc",
    "keeper",
    "dashlane",
    "enpass",
    # macOS system credentials
    "keychain access",
    # Banking (common patterns)
    "chase",
    "bank of america",
    "wells fargo",
    "capital one",
    "schwab",
    "fidelity",
    "td ameritrade",
    "robinhood",
    # Encrypted messaging (privacy-sensitive)
    "signal",
    # Crypto wallets
    "ledger live",
    "trezor suite",
    "metamask",
}


def load_blocklist(config_path: str | None = None) -> set[str]:
    """Load blocklist from JSON file, merged with defaults.

    Config format:
        {"blocked_apps": ["TikTok"], "allowed_apps": ["Signal"]}

    - blocked_apps: added to the default blocklist
    - allowed_apps: removed from the default blocklist (overrides defaults)

    Returns the final set of blocked app name patterns (all lowercased).
    """
    result = set(DEFAULT_BLOCKLIST)

    if config_path is None:
        return result

    try:
        with open(config_path) as f:
            config = json.load(f)
    except FileNotFoundError:
        logger.warning("Blocklist config not found: %s — using defaults", config_path)
        return result
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load blocklist config: %s — using defaults", exc)
        return result

    for app in config.get("blocked_apps", []):
        result.add(app.lower().strip())

    for app in config.get("allowed_apps", []):
        result.discard(app.lower().strip())

    return result


def is_blocked(app_name: str, blocklist: set[str]) -> bool:
    """Check if an app name matches any entry in the blocklist.

    Uses case-insensitive substring matching so "1Password 7" matches "1password".
    """
    app_lower = app_name.lower()
    return any(blocked in app_lower for blocked in blocklist)
