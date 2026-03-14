"""Tests for secret redaction in outbound text."""

from unittest.mock import patch

import pytest

from src.vault.redaction import redact_secrets_in_text
from src.vault.repository import vault_repository


@pytest.mark.asyncio
async def test_redact_secrets_in_text_replaces_known_secrets(async_db):
    await vault_repository.store("api_token", "super-secret-token")

    text = await redact_secrets_in_text("Use super-secret-token to call the API")
    assert text == "Use [redacted secret] to call the API"


@pytest.mark.asyncio
async def test_redact_secrets_in_text_ignores_short_values(async_db):
    await vault_repository.store("pin", "1234")

    text = await redact_secrets_in_text("The pin is 1234")
    assert text == "The pin is 1234"


@pytest.mark.asyncio
async def test_redact_secrets_in_text_fails_open_when_lookup_errors(async_db):
    with patch("src.vault.redaction.vault_repository.list_secret_values", side_effect=RuntimeError("boom")):
        text = await redact_secrets_in_text("leave this alone")

    assert text == "leave this alone"
