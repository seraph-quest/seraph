from src.security.site_policy import evaluate_site_access


def test_site_policy_blocks_legacy_loopback_integer_host():
    decision = evaluate_site_access("http://2130706433/")

    assert decision.allowed is False
    assert decision.reason == "internal_private"


def test_site_policy_blocks_short_loopback_host():
    decision = evaluate_site_access("http://127.1/")

    assert decision.allowed is False
    assert decision.reason == "internal_private"
