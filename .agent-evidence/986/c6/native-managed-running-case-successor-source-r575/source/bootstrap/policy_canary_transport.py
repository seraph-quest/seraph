'Test-only exact async manual-canary HTTP boundary; no proof issuance.'
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import stat

DUMMY_KEY = 'seraph-managed-offline-only-not-a-provider-credential'
ENDPOINT = 'https://openrouter.ai/api/v1/chat/completions'
FIXTURE = Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/managed-execution')
CAPABILITIES = ('text', 'structured_output', 'health', 'latency_ms')
DECLARED = ('text', 'structured_output')


def expected_bodies(setup):
    if (setup.get('schema_version', 'seraph.openrouter.setup.v1') != 'seraph.openrouter.setup.v1'
            or setup.get('model_ids') != ['openai/gpt-4o-mini']
            or setup.get('allowed_upstreams') != ['openai']
            or setup.get('capabilities') != list(DECLARED)
            or setup.get('data_collection') != 'deny' or setup.get('data_retention_policy') != 'deny'
            or setup.get('cloud_egress_acknowledged') is not True
            or setup.get('egress_class') != 'cloud_allowed_full'
            or setup.get('allow_fallbacks', False) is not False
            or setup.get('require_parameters', True) is not True
            or setup.get('zero_data_retention', False) is not False):
        raise RuntimeError('unreviewed_manual_canary_setup')
    common = {'model': 'openai/gpt-4o-mini', 'max_tokens': 64,
        'provider': {'only': ['openai'], 'allow_fallbacks': False,
                     'require_parameters': True, 'data_collection': 'deny',
                     'data_retention_policy': 'deny', 'zdr': False}}
    text = {**common, 'messages': [{'role': 'user', 'content': 'Reply with CANARY_OK only.'}]}
    structured = {**common, 'messages': [{'role': 'user', 'content': 'Return exactly {"ok":true}.'}],
                  'response_format': {'type': 'json_object'}}
    return {'text': text, 'structured_output': structured, 'health': text, 'latency_ms': text}


def make_handler(setup, httpx, observed, selected_capability):
    expected = expected_bodies(setup)
    consumed = set()

    def handler(request):
        if (request.method != 'POST' or str(request.url) != ENDPOINT
                or request.headers.get('Authorization') != 'Bearer ' + DUMMY_KEY):
            raise RuntimeError('unowned_manual_canary_request')
        body = json.loads(request.content)
        capability = selected_capability()
        if capability not in expected or body != expected[capability] or capability in consumed:
            raise RuntimeError('unowned_or_repeated_manual_canary_body')
        consumed.add(capability)
        # Never record request headers/credentials or replace the broker/proof.
        observed({'capability': capability, 'request_sha256': hashlib.sha256(request.content).hexdigest(),
                  'real_provider_socket_contacts': 0})
        content = '{"ok":true}' if capability == 'structured_output' else 'CANARY_OK'
        return httpx.Response(200, json={'id': 'managed-original-manual-canary-' + capability,
            'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'cost': '0.000000'},
            'choices': [{'message': {'role': 'assistant', 'content': content}}]})
    return handler


def install(context, *, observed):
    import httpx
    path = FIXTURE / 'journey/case.json'
    if Path(context['journey_directory']) != path.parent:
        raise RuntimeError('manual_canary_original_journey_changed')
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
        raise RuntimeError('manual_canary_original_case_not_private')
    config = json.loads(path.read_text())
    from src.api import model_fabric_settings as api
    current = ContextVar('managed_original_canary_capability', default=None)
    handler = make_handler(config['model_fabric_settings']['openrouter'], httpx, observed, current.get)
    actual = httpx.AsyncClient
    original_transport = api._execute_canary_transport

    async def execute(profile, **kwargs):
        capability = kwargs.get('capability')
        if capability not in CAPABILITIES or current.get() is not None:
            raise RuntimeError('unowned_original_manual_canary_callback')
        token = current.set(capability)
        try:
            return await original_transport(profile, **kwargs)
        finally:
            current.reset(token)

    def client(*args, **kwargs):
        if args or kwargs != {'timeout': 5.0, 'follow_redirects': False}:
            raise RuntimeError('unowned_async_manual_canary_client')
        return actual(**kwargs, transport=httpx.MockTransport(handler))

    api._execute_canary_transport = execute
    httpx.AsyncClient = client
    def restore():
        if httpx.AsyncClient is not client or api._execute_canary_transport is not execute:
            raise RuntimeError('manual_canary_restore_order_changed')
        httpx.AsyncClient = actual
        api._execute_canary_transport = original_transport
    return restore
