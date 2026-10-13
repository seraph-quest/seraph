'Original authenticated PUT/manual POST sequence; no stored proof fixtures.'
import copy
from policy_canary_transport import CAPABILITIES, DUMMY_KEY, expected_bodies


def configure_original_policy(http, settings_payload):
    payload = copy.deepcopy(settings_payload)
    expected_bodies(payload['openrouter'])
    payload['openrouter']['api_key'] = DUMMY_KEY
    # No read of vault, SQL credentials, fake hash or existing proof files.
    http.request('PUT', '/api/settings/model-fabric', payload)
    observations = []
    for capability in CAPABILITIES:
        result = http.request('POST', '/api/settings/model-fabric/canary', {
            'profile_id': 'openrouter', 'capability': capability,
            'timeout_seconds': 5, 'proof_ttl_seconds': 3600})
        if (result.get('profile_id') != 'openrouter' or result.get('capability') != capability
                or result.get('outcome') != 'passed' or result.get('error_code') is not None
                or result.get('receipt_persistence') != 'persisted'
                or result.get('proof_persistence') != 'persisted' or type(result.get('proof')) is not dict):
            raise AssertionError('original_manual_canary_receipts_not_persisted')
        proof = result['proof']
        if (proof.get('profile_id') != 'openrouter' or proof.get('capability') != capability
                or proof.get('outcome') != 'passed'):
            raise AssertionError('original_manual_canary_proof_identity_or_outcome_changed')
        observations.append({'capability': capability, 'outcome': result['outcome'],
            'receipt_persistence': result['receipt_persistence'],
            'proof_persistence': result['proof_persistence']})
    return observations
