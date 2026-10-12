"""Original model/copy accounting and private payer lifetime regressions."""
import copy
import json
import sys

import pytest

from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError
from src.workspace import accounting_continuity as continuity
from tests.test_discovery_compiler_precharge import (
    genuine_live_selection, goal_programme_service, goal_discovery_service, cordis_host,
    test_genuine_nonempty_identity_compiler_occurrences as original_positive,
)


@pytest.mark.parametrize("value", ["", "printable\\quoted\"", "\ud800", "\U0001f680", 10 ** 600])
def test_paid_scalar_upper_covers_original_json(value):
    budget = HeaderReadBudget()
    before = budget.remaining
    upper = continuity._programme_value_upper(value, budget=budget)
    assert upper >= len(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode())
    assert budget.remaining < before


def test_float_counter_ignores_markers_inside_original_json_strings():
    budget = HeaderReadBudget()
    raw = '{"brief":"eeee...EEEE", "amount":1e20}'
    upper = continuity._programme_parse_upper(raw, budget=budget)
    assert upper == continuity._programme_value_upper(raw) + 64
    assert upper >= len(json.dumps(json.loads(raw), ensure_ascii=True, separators=(",", ":")).encode())


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_original_json_nonfinite_tokens_have_prepaid_float_width(token):
    budget = HeaderReadBudget()
    raw = '{"value":' + token + ',"brief":"NaN Infinity"}'
    upper = continuity._programme_parse_upper(raw, budget=budget)
    assert upper == continuity._programme_value_upper(raw) + 64
    assert type(json.loads(raw)["value"]) is float


@pytest.mark.parametrize("text", ["\u03bb" * 4096, "\ud800" * 1024])
def test_long_unicode_scan_prepays_original_encoded_buffer_and_byte_scalars(text):
    raw = '{"brief":"' + text + '","amount":1e20}'
    budget = HeaderReadBudget()
    before = budget.remaining
    upper = continuity._programme_parse_upper(raw, budget=budget)
    encoded = raw.encode("utf-8", errors="surrogatepass")
    assert before - budget.remaining >= 4 * len(encoded)
    assert upper >= len(json.dumps(json.loads(raw), ensure_ascii=True, separators=(",", ":")).encode())


def test_unissued_constructor_payer_denies_before_owner_or_debit():
    payer = object.__new__(continuity._ProgrammeConstructorPayer)
    with pytest.raises(HeaderBoundsError, match="programme_constructor_owner_unavailable"):
        payer._checked()


@pytest.mark.asyncio
async def test_original_positive_payer_rejects_copy_and_changed_certificate(genuine_live_selection, monkeypatch):
    observed = []
    prior = sys.getprofile()
    def profile(frame, action, result):
        if (action == "return" and frame.f_code is continuity._programme_constructor_payer.__code__
                and type(result) is continuity._ProgrammeConstructorPayer):
            payer = result
            budget = payer._checked()
            before = budget.remaining
            with pytest.raises(HeaderBoundsError, match="programme_constructor_owner_unavailable"):
                copy.copy(payer)._checked()
            original = payer.certificate
            payer.certificate = copy.copy(original)
            try:
                with pytest.raises(HeaderBoundsError, match="programme_constructor_owner_changed"):
                    payer._checked()
            finally:
                payer.certificate = original
            for field in ("task", "transaction", "connection", "budget"):
                genuine = getattr(payer, field)
                setattr(payer, field, object())
                try:
                    with pytest.raises(HeaderBoundsError, match="programme_constructor_owner_changed"):
                        payer._checked()
                finally:
                    setattr(payer, field, genuine)
            from src.memory import composition_headers as headers
            token = headers._CURRENT_SNAPSHOT_SCOPE.set(copy.copy(payer.scope))
            try:
                with pytest.raises(HeaderBoundsError):
                    payer._checked()
            finally:
                headers._CURRENT_SNAPSHOT_SCOPE.reset(token)
            assert payer._checked() is budget and budget.remaining == before
            observed.append(True)
        if prior is not None:
            prior(frame, action, result)
    sys.setprofile(profile)
    try:
        await original_positive(genuine_live_selection, monkeypatch, "positive")
    finally:
        sys.setprofile(prior)
    assert observed
