"""Typed capability declaration for the bounded Gmail source lane.

The declaration is deliberately descriptive.  Actual provider access remains
behind ``src.integrations.gmail_read.GoogleGmailReadonlyAdapter`` and the
authenticated Mail router; registering this module must never make an
unconfigured Gmail account appear ready.
"""

from __future__ import annotations

from typing import Any


MAIL_MESSAGES_READ = "mail.messages.read"
MAIL_REPLY_PREPARE_LOCAL = "mail.reply.prepare_local"
MAIL_SOURCE_CAPABILITY_VERSION = "1"


def gmail_read_capability() -> dict[str, Any]:
    """Return the redacted source contract consumed by capability inventory."""

    return {
        "capability_id": MAIL_MESSAGES_READ,
        "capability_version": MAIL_SOURCE_CAPABILITY_VERSION,
        "provider": "gmail",
        "service": "gmail_readonly",
        "access_mode": "authenticated_read_only",
        "contracts": [MAIL_MESSAGES_READ, MAIL_REPLY_PREPARE_LOCAL],
        "permissions": {
            "provider_scope": "https://www.googleapis.com/auth/gmail.readonly",
            "methods": ["GET"],
            "resource_allowlist": ["labels", "messages.list", "messages.get"],
            "remote_model": False,
            "mailbox_mutation": False,
        },
        "limits": {
            "labels": 200,
            "selected_labels": 3,
            "messages": 10,
            "metadata_concurrency": 2,
            "window_days": 7,
        },
        "runtime_state": "configuration_required",
        "memory_status": "no_learning",
    }


__all__ = [
    "MAIL_MESSAGES_READ",
    "MAIL_REPLY_PREPARE_LOCAL",
    "MAIL_SOURCE_CAPABILITY_VERSION",
    "gmail_read_capability",
]
