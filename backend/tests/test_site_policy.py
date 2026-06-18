from unittest.mock import patch

from src.security.site_policy import evaluate_site_access


def test_site_policy_blocks_legacy_loopback_integer_host():
    decision = evaluate_site_access("http://2130706433/")

    assert decision.allowed is False
    assert decision.reason == "internal_private"


def test_site_policy_blocks_short_loopback_host():
    decision = evaluate_site_access("http://127.1/")

    assert decision.allowed is False
    assert decision.reason == "internal_private"


def test_site_policy_blocks_private_dns_resolution_preflight():
    with patch(
        "src.security.site_policy.socket.getaddrinfo",
        return_value=[(None, None, None, None, ("10.0.0.7", 0))],
    ):
        decision = evaluate_site_access("https://public-looking.example/", resolve_dns=True)

    assert decision.allowed is False
    assert decision.reason == "internal_private"
    assert decision.resolved_addresses == ("10.0.0.7",)
