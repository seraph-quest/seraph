"""Tests for safe audit formatting helpers."""

from src.audit.formatting import redact_for_audit, format_tool_call_summary, summarize_tool_result


def test_redact_for_audit_redacts_sensitive_keys():
    payload = {
        "key": "open-sesame",
        "headers": {"Authorization": "Bearer abc"},
        "nested": {"token_value": "xyz"},
        "query": "weather",
    }

    redacted = redact_for_audit(payload)

    assert redacted["key"] == "[redacted]"
    assert redacted["headers"] == "[redacted]"
    assert redacted["nested"]["token_value"] == "[redacted]"
    assert redacted["query"] == "weather"


def test_format_tool_call_summary_uses_redacted_arguments():
    summary = format_tool_call_summary(
        "store_secret",
        {"key": "github", "value": "super-secret-token"},
        set(),
    )

    assert "store_secret" in summary
    assert "[redacted]" in summary
    assert "super-secret-token" not in summary


def test_summarize_tool_result_avoids_raw_output():
    summary, details = summarize_tool_result("get_secret", "actual-secret-value")

    assert summary == "get_secret returned output (19 chars)"
    assert details["output_length"] == 19
    assert details["content_redacted"] is True
