'Finite transport boundary units; not auth/probe/admission acceptance.'
import httpx
import pytest
from policy_canary_transport import (CAPABILITIES, DUMMY_KEY, ENDPOINT, expected_bodies, make_handler)

SETUP = {'model_ids': ['openai/gpt-4o-mini'], 'allowed_upstreams': ['openai'],
    'capabilities': ['text', 'structured_output'], 'data_collection': 'deny',
    'data_retention_policy': 'deny', 'cloud_egress_acknowledged': True,
    'egress_class': 'cloud_allowed_full'}

@pytest.mark.parametrize('capability', CAPABILITIES)
def test_original_canary_exact_body_once(capability):
    observed = []
    handler = make_handler(SETUP, httpx, observed.append, lambda: capability)
    request = httpx.Request('POST', ENDPOINT, json=expected_bodies(SETUP)[capability],
                            headers={'Authorization': 'Bearer ' + DUMMY_KEY})
    result = handler(request)
    assert result.status_code == 200
    assert result.json()['usage']['cost'] == '0.000000'
    assert observed[0]['capability'] == capability
    assert DUMMY_KEY not in str(observed)
    with pytest.raises(RuntimeError, match='^unowned_or_repeated_manual_canary_body$'):
        handler(request)

@pytest.mark.parametrize('url', ['https://openrouter.ai/api/v1/models',
    'https://openrouter.ai/api/v1/chat/completions?x=1',
    'http://openrouter.ai/api/v1/chat/completions',
    'https://unowned.invalid/api/v1/chat/completions'])
def test_canary_rejects_literal_unowned_url(url):
    handler = make_handler(SETUP, httpx, lambda _: pytest.fail('unowned callback'), lambda: 'text')
    request = httpx.Request('POST', url, json=expected_bodies(SETUP)['text'],
                            headers={'Authorization': 'Bearer ' + DUMMY_KEY})
    with pytest.raises(RuntimeError, match='^unowned_manual_canary_request$'):
        handler(request)

def test_canary_rejects_unowned_body():
    handler = make_handler(SETUP, httpx, lambda _: pytest.fail('unowned callback'), lambda: 'text')
    request = httpx.Request('POST', ENDPOINT, json={'model': 'openai/gpt-4o-mini'},
                            headers={'Authorization': 'Bearer ' + DUMMY_KEY})
    with pytest.raises(RuntimeError, match='^unowned_or_repeated_manual_canary_body$'):
        handler(request)
