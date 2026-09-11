"""Canonical conversation identity helpers."""

from .identity import (
    CONVERSATION_SCHEMA_VERSION,
    ConversationIdentity,
    ConversationIdentityError,
    build_conversation_identity,
    build_lineage,
    redact_attachment_refs,
    validate_attachment_refs,
)

__all__ = [
    "CONVERSATION_SCHEMA_VERSION",
    "ConversationIdentity",
    "ConversationIdentityError",
    "build_conversation_identity",
    "build_lineage",
    "redact_attachment_refs",
    "validate_attachment_refs",
]
