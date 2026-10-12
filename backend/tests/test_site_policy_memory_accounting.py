"""Component evidence only; no issued REPORT8 fixture or Memory activation."""
import pytest

from config.settings import settings
from src.memory.header_bounds import HeaderBoundsError, HeaderReadBudget
from src.security import site_policy


def _dimensions(value):
    # Numeric forecast inputs, not a payer/certificate/Source or permission.
    utf8 = len(value.encode("utf-8"))
    letters = [char for char in value if char.isascii() and char.isalpha()]
    return (utf8, 6 * utf8 + 2, value.isascii(), value.count(","),
        value.count(":"), value.count("/"), value.count("."), value.count("-"),
        all(char.isascii() and (char.isalpha() or char.isdigit() or char in ".-") for char in value),
        len(value), any(char.isspace() for char in value), "%" in value,
        "[" in value or "]" in value, "@" in value, "?" in value, "#" in value,
        "://" in value, bool(letters), any(char not in "abcdefABCDEFxX" for char in letters))


@pytest.mark.parametrize("host,expected", [("fixture.example", 6011), ("raw.githubusercontent.com", 7632)])
def test_known_forecast_is_source_numeric_without_policy_execution(monkeypatch, host, expected):
    def forbidden(*args, **kwargs):
        raise AssertionError("forecast executed policy parser")
    monkeypatch.setattr(site_policy, "urlparse", forbidden)
    monkeypatch.setattr(site_policy.socket, "inet_aton", forbidden)
    upper = site_policy._memory_policy_known_upper(_dimensions(host),
        _dimensions("fixture.example"), _dimensions(""))
    assert type(upper) is int and upper == expected
    assert 2 * sum(site_policy._memory_policy_slots(count) for count in (2, 1, 1, 3)) == 78
    assert (site_policy._memory_policy_slots(5) + site_policy._memory_policy_slots(6)
        + 2 * (site_policy._memory_policy_slots(2) + site_policy._memory_policy_slots(1))
        + 2 * site_policy._memory_policy_slots(1)) == 103


@pytest.mark.parametrize("host,allow,block", [
    ("0x7f.1", "fixture.example", ""),
    ("1.2.3.4", "fixture.example", ""),
    ("https://fixture.example/", "fixture.example", ""),
    ("fıxture.example", "fixture.example", ""),
    ("fixture.example", "fixture.example,other.example", ""),
    ("fixture.example", "fixture.example", "blocked.example"),
])
def test_unclosed_forecast_stops_without_running_policy(monkeypatch, host, allow, block):
    def forbidden(*args, **kwargs):
        raise AssertionError("unclosed forecast executed parser")
    monkeypatch.setattr(site_policy, "urlparse", forbidden)
    monkeypatch.setattr(site_policy.socket, "inet_aton", forbidden)
    with pytest.raises(HeaderBoundsError, match="memory_report_site_policy_numeric_unavailable"):
        site_policy._memory_policy_known_upper(_dimensions(host), _dimensions(allow), _dimensions(block))


def test_unissued_payer_denies_before_normalization_and_does_not_spend(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("unissued payer reached policy allocation")
    monkeypatch.setattr(site_policy, "_normalize_hostname", forbidden)
    budget = HeaderReadBudget()
    before = budget.remaining
    with pytest.raises(HeaderBoundsError, match="memory_report_admission_numeric_owner_changed"):
        site_policy.evaluate_site_access("fixture.example", _memory_header_budget=budget)
    assert budget.remaining == before


def test_default_unicode_policy_and_legacy_internal_checks_remain(monkeypatch):
    monkeypatch.setattr(settings, "browser_site_allowlist", "fıxture.example")
    monkeypatch.setattr(settings, "browser_site_blocklist", "")
    monkeypatch.setattr(site_policy.socket, "getaddrinfo", lambda *args: pytest.fail("unexpected DNS"))
    decision = site_policy.evaluate_site_access("fıxture.example")
    assert decision.allowed and decision.hostname == "fıxture.example"
    assert decision.matched_rule == "fıxture.example"
    for host in ("2130706433", "127.1", "0x7f.1"):
        decision = site_policy.evaluate_site_access(host)
        assert not decision.allowed and decision.reason == "internal_private"
